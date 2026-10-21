"""ローカル完結型 Codex、Claude Code、Antigravity 利用状況ダッシュボード。

ローカル環境における各種AIアシスタントのトークン消費量、利用枠（クォータ）、
推定コスト、サービス稼働状況を集計し、ブラウザで閲覧可能なダッシュボードを提供します。
"""

import argparse
import calendar
import json
import os
import re
import shutil
import ssl
import subprocess  # nosec B404
import sys
import threading
import time
import webbrowser
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, cast
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

try:
    from datetime import UTC
except ImportError:
    UTC = timezone.utc  # type: ignore[misc,assignment]

# Codex (OpenAI) 各モデルの100万トークンあたり価格 (入力USD, キャッシュ入力USD, 出力USD)
CODEX_PRICES_PER_MILLION = {
    # バージョン非依存のモデル名称キー（最新世代デフォルト）
    "gpt-6": (2.50, 1.25, 10.0),
    "gpt-5": (1.25, 0.125, 10.0),
    "gpt-mini": (0.25, 0.025, 2.0),
    "gpt-oss": (0.0, 0.0, 0.0),
    "gpt": (1.25, 0.125, 10.0),
    "mini": (0.25, 0.025, 2.0),
    "oss": (0.0, 0.0, 0.0),
    "o3": (15.0, 7.5, 60.0),
    "o3-mini": (1.10, 0.55, 4.40),
    "o4-mini": (1.10, 0.55, 4.40),
    "o1": (15.0, 7.5, 60.0),
    "o1-mini": (1.10, 0.55, 4.40),
    # 2026年最新バージョンキー
    "gpt-6-astra": (2.50, 1.25, 10.0),
    "gpt-6-sol": (2.50, 1.25, 10.0),
    "gpt-6-luna": (2.50, 1.25, 10.0),
    "gpt-5.6-terra": (2.0, 0.2, 12.0),
    "gpt-5.5": (1.75, 0.175, 14.0),
    "gpt-5.4": (1.25, 0.125, 10.0),
    "gpt-5.4-mini": (0.25, 0.025, 2.0),
    "gpt-5-mini": (0.25, 0.025, 2.0),
    # 旧世代バージョン（互換性保持）
    "gpt-4.5": (75.0, 37.5, 150.0),
    "gpt-4o": (2.50, 1.25, 10.0),
    "gpt-4o-mini": (0.15, 0.075, 0.60),
    "gpt-4": (2.50, 1.25, 10.0),
}
# 後方互換エイリアス
PRICES_PER_MILLION = CODEX_PRICES_PER_MILLION

# Claude Code (Anthropic) 各モデルの100万トークンあたり価格
# (入力USD, キャッシュ書込min USD, キャッシュ書込max USD, キャッシュ読込USD, 出力USD)
CLAUDE_PRICES_PER_MILLION = {
    # バージョン非依存のモデル名称キー（最新世代 5 / 5.5 価格を適用）
    "fable": (10.0, 12.50, 20.0, 1.0, 50.0),
    "opus": (4.0, 5.0, 8.0, 0.4, 20.0),
    "sonnet": (2.0, 2.5, 4.0, 0.2, 10.0),
    "haiku": (0.10, 0.125, 0.20, 0.01, 0.50),
    # 2026年最新バージョンキー
    "claude-fable-5-1": (10.0, 12.50, 20.0, 1.0, 50.0),
    "claude-fable-5": (10.0, 12.50, 20.0, 1.0, 50.0),
    "claude-opus-5-5": (4.0, 5.0, 8.0, 0.4, 20.0),
    "claude-opus-5": (4.0, 5.0, 8.0, 0.4, 20.0),
    "claude-sonnet-5-5": (2.0, 2.5, 4.0, 0.2, 10.0),
    "claude-sonnet-5": (2.0, 2.5, 4.0, 0.2, 10.0),
    "claude-haiku-5-5": (0.10, 0.125, 0.20, 0.01, 0.50),
    "claude-haiku-5": (0.10, 0.125, 0.20, 0.01, 0.50),
    # 旧世代バージョン（互換性保持）
    "claude-opus-4.5": (15.0, 18.75, 30.0, 1.5, 75.0),
    "claude-opus-4": (15.0, 18.75, 30.0, 1.5, 75.0),
    "claude-opus-3.7": (15.0, 18.75, 30.0, 1.5, 75.0),
    "claude-opus-3-7": (15.0, 18.75, 30.0, 1.5, 75.0),
    "claude-sonnet-4.6": (3.0, 3.75, 6.0, 0.3, 15.0),
    "claude-sonnet-4.5": (3.0, 3.75, 6.0, 0.3, 15.0),
    "claude-sonnet-4": (3.0, 3.75, 6.0, 0.3, 15.0),
    "claude-3.7-sonnet": (3.0, 3.75, 6.0, 0.3, 15.0),
    "claude-3-7-sonnet": (3.0, 3.75, 6.0, 0.3, 15.0),
    "claude-3.5-sonnet": (3.0, 3.75, 6.0, 0.3, 15.0),
    "claude-3-5-sonnet": (3.0, 3.75, 6.0, 0.3, 15.0),
    "claude-haiku-4.5": (1.0, 1.25, 2.0, 0.1, 5.0),
    "claude-3.5-haiku": (0.80, 1.0, 1.6, 0.08, 4.0),
    "claude-3-5-haiku": (0.80, 1.0, 1.6, 0.08, 4.0),
}

# Antigravity (Gemini / Claude / GPT / OSS) 各モデルの100万トークンあたり価格 (入力USD, 出力USD)
AGY_PRICES_PER_MILLION = {
    # バージョン非依存のモデル名称キー（最新デフォルト）
    "gpt-oss": (0.0, 0.0),
    "oss": (0.0, 0.0),
    "argon": (1.25, 5.00),
    "flash": (0.15, 0.60),
    "pro": (1.25, 5.00),
    "gemini": (0.15, 0.60),
    "sonnet": (2.0, 10.0),
    "opus": (4.0, 20.0),
    "fable": (10.0, 50.0),
    "haiku": (0.10, 0.50),
    # Antigravity IDE UIセレクタ表示名
    "gemini 3.8 flash": (0.15, 0.60),
    "gemini 4 argon": (1.25, 5.00),
    "gemini 4": (1.25, 5.00),
    "claude opus 5.5": (4.0, 20.0),
    "claude sonnet 5.5": (2.0, 10.0),
    "gpt-oss 120b": (0.0, 0.0),
    # 旧世代キー
    "gemini-2.5-pro": (1.25, 5.00),
    "gemini-2.5-flash": (0.15, 0.60),
    "gemini-2.0-pro": (1.25, 5.00),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-1.5-pro": (1.25, 5.00),
    "gemini-1.5-flash": (0.075, 0.30),
}
# 後方互換エイリアス
GEMINI_PRICES_PER_MILLION = AGY_PRICES_PER_MILLION


def classify_agy_model(model_name: str | None) -> str:
    """モデル名に基づいてモデル系列（gemini, claude_gpt, argon）を判定します。

    Args:
        model_name (str | None): 判定対象のモデル名。None の場合はデフォルトとして扱われます。

    Returns:
        str: モデル系列名（"gemini", "claude_gpt", または "argon"）。
    """
    name = (model_name or "").lower()
    if "argon" in name:
        return "argon"
    if any(k in name for k in ("claude", "gpt", "sonnet", "opus", "oss")):
        return "claude_gpt"
    return "gemini"


AGY_PLAN_QUOTAS: dict[str, dict[str, dict[str, int]]] = {
    "Google AI Free": {
        "gemini": {"5h": 150, "7d": 500},
        "claude_gpt": {"5h": 0, "7d": 0},
    },
    "Google AI Plus": {
        "gemini": {"5h": 1100, "7d": 3800},
        "claude_gpt": {"5h": 25, "7d": 110},
    },
    "Google AI Pro": {
        "gemini": {"5h": 1200, "7d": 4100},
        "claude_gpt": {"5h": 30, "7d": 145},
    },
    "Google AI Ultra 5x": {
        "gemini": {"5h": 5500, "7d": 19000},
        "claude_gpt": {"5h": 125, "7d": 550},
    },
    "Google AI Ultra 20x": {
        "gemini": {"5h": 22000, "7d": 76000},
        "claude_gpt": {"5h": 500, "7d": 2200},
    },
}
STATUS_URL = "https://status.openai.com/api/v2/summary.json"
INCIDENTS_URL = "https://status.openai.com/api/v2/incidents.json"
CLAUDE_STATUS_URL = "https://status.claude.com/api/v2/summary.json"
CLAUDE_INCIDENTS_URL = "https://status.claude.com/api/v2/incidents.json"
GOOGLE_STATUS_URL = "https://status.cloud.google.com/incidents.json"
STATUS_CACHE: tuple[float, dict[str, Any]] | None = None
CLAUDE_STATUS_CACHE: tuple[float, dict[str, Any]] | None = None
GOOGLE_STATUS_CACHE: tuple[float, dict[str, Any]] | None = None


def summarize_session(path: Path) -> dict[str, Any] | None:
    """単一の Codex セッション JSONL ファイルから最新の使用量と安全なタスクメタデータを抽出します。

    Args:
        path (Path): 解析対象の JSONL セッションファイルのパス。

    Returns:
        dict[str, Any] | None: 抽出されたセッション情報の辞書。有効なレコードが存在しない場合は None。
            辞書にはセッションID、モデル、利用制限、コンテキストトークン、トークン使用量、
            推定コスト、タスク情報などが含まれます。
    """
    summary: dict[str, Any] = {
        "session_id": None,
        "auxiliary": False,
        "observed_at": None,
        "model": None,
        "effort": None,
        "limits": {"primary": None, "secondary": None},
        "context": {"used_tokens": 0, "window_tokens": 0, "used_percent": None},
        "usage": {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        },
        "task": {"cwd": None, "prompt": None},
    }
    saw_record = False
    estimated_cost_usd = 0.0
    cost_available = True

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        saw_record = True
        summary["observed_at"] = record.get("timestamp") or summary["observed_at"]
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue

        if record.get("type") == "session_meta":
            summary["session_id"] = (
                payload.get("session_id") or payload.get("id") or summary["session_id"]
            )
            summary["task"]["cwd"] = payload.get("cwd") or summary["task"]["cwd"]
            summary["auxiliary"] = payload.get("thread_source") == "guardian_review"
        elif record.get("type") == "turn_context":
            summary["model"] = payload.get("model") or summary["model"]
            summary["effort"] = payload.get("effort") or summary["effort"]
            summary["task"]["cwd"] = payload.get("cwd") or summary["task"]["cwd"]
        elif record.get("type") == "response_item" and summary["task"]["prompt"] is None:
            prompt = first_user_prompt(payload)
            if prompt:
                cast(dict[str, Any], summary["task"])["prompt"] = prompt
        elif record.get("type") == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            raw_usage = info.get("last_token_usage")
            usage: dict[str, Any] = dict(raw_usage) if isinstance(raw_usage, dict) else {}
            summary["usage"] = normalize_usage(usage)
            window = number(info.get("model_context_window"))
            summary["context"] = {
                "used_tokens": summary["usage"]["input_tokens"],
                "window_tokens": window,
                "used_percent": percent(summary["usage"]["input_tokens"], window),
            }
            limits = (
                payload.get("rate_limits") if isinstance(payload.get("rate_limits"), dict) else {}
            )
            summary["limits"] = {
                "primary": normalize_limit(limits.get("primary")),
                "secondary": normalize_limit(limits.get("secondary")),
            }
            cost = usage_cost(summary["model"], summary["usage"])
            if cost is None:
                cost_available = False
            else:
                estimated_cost_usd += cost

    if not saw_record:
        return None
    summary["estimated_cost_usd"] = round(estimated_cost_usd, 8) if cost_available else None
    return summary


def empty_claude_summary() -> dict[str, Any]:
    """Claude Code のローカルセッションが存在しない場合の初期データ構造を返します。

    Returns:
        dict[str, Any]: 空の利用量・タスク構造を持つ辞書。
    """
    return {
        "observed_at": None,
        "model": None,
        "usage": {
            "input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        },
        "limits": {"primary": None, "secondary": None},
        "task": {"cwd": None, "prompt": None},
    }


def summarize_claude_session(path: Path) -> dict[str, Any] | None:
    """単一の Claude Code セッション JSONL ファイルからアシスタントのトークン記録を集計します。

    Args:
        path (Path): Claude Code の JSONL セッションファイルのパス。

    Returns:
        dict[str, Any] | None: 集計されたセッション情報の辞書。有効な利用記録やタスクがない場合は None。
    """
    summary = empty_claude_summary()
    saw_record = False
    saw_usage = False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        saw_record = True
        if record.get("type") == "user" and summary["task"]["prompt"] is None:
            prompt = first_claude_user_prompt(record)
            if prompt:
                summary["task"]["prompt"] = prompt
                summary["task"]["cwd"] = record.get("cwd")
        message = record.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(usage, dict):
            continue
        saw_usage = True
        summary["observed_at"] = record.get("timestamp") or summary["observed_at"]
        summary["model"] = message.get("model") or summary["model"]
        normalized = {
            key: number(usage.get(key))
            for key in (
                "input_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
                "output_tokens",
            )
        }
        for key, value in normalized.items():
            summary["usage"][key] += value
        summary["usage"]["total_tokens"] += sum(normalized.values())
    return summary if saw_record and (saw_usage or summary["task"]["prompt"]) else None


def empty_agy_summary() -> dict[str, Any]:
    """Antigravity のローカルセッションが存在しない場合の初期データ構造を返します。

    Returns:
        dict[str, Any]: デフォルトのモデル（Gemini 2.5 Flash）や空のステップ・トークン数を含む辞書。
    """
    return {
        "observed_at": None,
        "model": "Gemini 2.5 Flash",
        "model_family": "gemini",
        "usage": {
            "steps": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "gemini": {
                "steps": 0,
                "steps_5h": 0,
                "steps_7d": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            },
            "claude_gpt": {
                "steps": 0,
                "steps_5h": 0,
                "steps_7d": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            },
            "argon": {
                "steps": 0,
                "steps_5h": 0,
                "steps_7d": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            },
        },
        "limits": {
            "gemini": {"primary": None, "secondary": None},
            "claude_gpt": {"primary": None, "secondary": None},
            "argon": {"primary": None, "secondary": None},
            "rate_limit": None,
        },
        "task": {"cwd": None, "prompt": None},
        "estimated_cost_usd": None,
    }


AGY_LIVE_CACHE: tuple[float, dict[str, Any] | None] | None = None


def _detect_language_server_process() -> tuple[str, str] | None:
    """Antigravity language_server のプロセスID (PID) と CSRF トークンを検出します。

    Linux、macOS、Windows の各プラットフォームに対応しています。

    Returns:
        tuple[str, str] | None: (pid, csrf_token) のタプル。検出できない場合は None。
    """
    # 1. Windows: PowerShell を使用してプロセスのコマンドラインから抽出
    if sys.platform == "win32":
        powershell_bin = shutil.which("powershell") or shutil.which("pwsh")
        if powershell_bin:
            try:
                flags = 0x08000000 if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
                ps_script = (
                    "Get-CimInstance Win32_Process | "
                    "Where-Object { $_.CommandLine -like '*csrf_token*' } | "
                    "ForEach-Object { \"$($_.ProcessId) $($_.CommandLine)\" }"
                )
                out = subprocess.check_output(  # nosec B603
                    [powershell_bin, "-NoProfile", "-NonInteractive", "-Command", ps_script],
                    creationflags=flags,
                    stderr=subprocess.DEVNULL,
                ).decode("utf-8", "ignore")
                for line in out.splitlines():
                    if "csrf_token" in line:
                        parts = line.strip().split(None, 1)
                        if len(parts) >= 2:
                            pid = parts[0]
                            m = re.search(r"--csrf_token\s+([a-zA-Z0-9-]+)", parts[1])
                            if pid and m:
                                return pid, m.group(1)
            except Exception:
                pass
        return None

    # 2. macOS: ps コマンドを使用してプロセスコマンドラインを抽出
    if sys.platform == "darwin":
        ps_bin = shutil.which("ps")
        if ps_bin:
            try:
                out = subprocess.check_output(  # nosec B603
                    [ps_bin, "-ax", "-o", "pid,command"],
                    stderr=subprocess.DEVNULL,
                ).decode("utf-8", "ignore")
                for line in out.splitlines():
                    if "language_server" in line and "csrf_token" in line:
                        parts = line.strip().split(None, 1)
                        if len(parts) >= 2:
                            pid = parts[0]
                            m = re.search(r"--csrf_token\s+([a-zA-Z0-9-]+)", parts[1])
                            if pid and m:
                                return pid, m.group(1)
            except Exception:
                pass
        return None

    # 3. Linux / その他 UNIX
    # 3a. pgrep によるプロセス検索
    pgrep_bin = shutil.which("pgrep")
    if pgrep_bin:
        try:
            out = subprocess.check_output(  # nosec B603
                [pgrep_bin, "-af", "language_server"],
                stderr=subprocess.DEVNULL,
            ).decode("utf-8", "ignore")
            for line in out.splitlines():
                if "csrf_token" in line:
                    parts = line.split()
                    pid = parts[0] if parts else None
                    m = re.search(r"--csrf_token\s+([a-zA-Z0-9-]+)", line)
                    if pid and m:
                        return pid, m.group(1)
        except Exception:
            pass

    # 3b. /proc ファイルシステム走査によるフォールバック（pgrep 不在時）
    proc_path = Path("/proc")
    if proc_path.is_dir():
        try:
            for entry in proc_path.iterdir():
                if entry.is_dir() and entry.name.isdigit():
                    cmdline_file = entry / "cmdline"
                    if cmdline_file.is_file():
                        try:
                            content = cmdline_file.read_bytes().decode("utf-8", "ignore")
                            if "language_server" in content and "csrf_token" in content:
                                m = re.search(r"--csrf_token[\x00\s]+([a-zA-Z0-9-]+)", content)
                                if m:
                                    return entry.name, m.group(1)
                        except OSError:
                            continue
        except Exception:
            pass

    return None


def _detect_listening_ports(pid: str) -> list[int]:
    """指定されたプロセスID (PID) が 127.0.0.1 でリッスンしている TCP ポート一覧を取得します。

    Linux、macOS、Windows の各プラットフォームに対応しています。

    Args:
        pid (str): 対象プロセスの PID。

    Returns:
        list[int]: 検出されたローカルポート番号のリスト。
    """
    ports: list[int] = []

    # 1. Windows: netstat -ano -p tcp
    if sys.platform == "win32":
        netstat_bin = shutil.which("netstat")
        if netstat_bin:
            try:
                flags = 0x08000000 if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
                out = subprocess.check_output(  # nosec B603
                    [netstat_bin, "-ano", "-p", "tcp"],
                    creationflags=flags,
                    stderr=subprocess.DEVNULL,
                ).decode("utf-8", "ignore")
                for line in out.splitlines():
                    if "LISTENING" in line and "127.0.0.1:" in line:
                        parts = line.strip().split()
                        if len(parts) >= 5 and parts[-1] == str(pid):
                            m = re.search(r"127\.0\.0\.1:(\d+)", parts[1])
                            if m:
                                ports.append(int(m.group(1)))
            except Exception:
                pass
        return ports

    # 2. macOS: lsof -nP -iTCP -sTCP:LISTEN -a -p <PID>
    if sys.platform == "darwin":
        lsof_bin = shutil.which("lsof")
        if lsof_bin:
            try:
                out = subprocess.check_output(  # nosec B603
                    [lsof_bin, "-nP", "-iTCP", "-sTCP:LISTEN", "-a", "-p", str(pid)],
                    stderr=subprocess.DEVNULL,
                ).decode("utf-8", "ignore")
                for line in out.splitlines():
                    m = re.search(r"127\.0\.0\.1:(\d+)", line)
                    if m:
                        ports.append(int(m.group(1)))
            except Exception:
                pass
        return ports

    # 3. Linux: ss -tulpn を優先し、lsof にフォールバック
    ss_bin = shutil.which("ss")
    if ss_bin:
        try:
            ss_out = subprocess.check_output(  # nosec B603
                [ss_bin, "-tulpn"],
                stderr=subprocess.DEVNULL,
            ).decode("utf-8", "ignore")
            for line in ss_out.splitlines():
                if f"pid={pid}," in line and "127.0.0.1:" in line:
                    m = re.search(r"127\.0\.0\.1:(\d+)", line)
                    if m:
                        ports.append(int(m.group(1)))
        except Exception:
            pass

    if not ports:
        lsof_bin = shutil.which("lsof")
        if lsof_bin:
            try:
                out = subprocess.check_output(  # nosec B603
                    [lsof_bin, "-nP", "-iTCP", "-sTCP:LISTEN", "-a", "-p", str(pid)],
                    stderr=subprocess.DEVNULL,
                ).decode("utf-8", "ignore")
                for line in out.splitlines():
                    m = re.search(r"127\.0\.0\.1:(\d+)", line)
                    if m:
                        ports.append(int(m.group(1)))
            except Exception:
                pass

    return ports


def detect_antigravity_live_status(timeout_sec: float = 1.5) -> dict[str, Any] | None:
    """実行中の Antigravity language_server プロセスに接続し、公式のリアルタイムクォータ情報を取得します。

    Linux、macOS、Windows の各プラットフォームに対応しています。

    Args:
        timeout_sec (float, optional): 各リクエストのタイムアウト秒数。デフォルトは 1.5。

    Returns:
        dict[str, Any] | None: 取得したプラン名や Gemini / Claude-GPT の各枠（5h / 7d）の情報辞書。
            language_server が検出できない場合や通信に失敗した場合は None。
    """
    proc_info = _detect_language_server_process()
    if not proc_info:
        return None
    pid, token = proc_info

    ports = _detect_listening_ports(pid)
    if not ports:
        return None

    body = json.dumps(
        {"metadata": {"ideName": "antigravity", "extensionName": "antigravity", "locale": "ja"}}
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
        "X-Codeium-Csrf-Token": token,
    }

    def _parse_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
        frac = bucket.get("remainingFraction")
        reset_str = bucket.get("resetTime")
        reset_ts = None
        if reset_str:
            try:
                reset_ts = int(datetime.fromisoformat(reset_str.replace("Z", "+00:00")).timestamp())
            except (ValueError, TypeError):
                pass
        rem_pct = round(frac * 100) if frac is not None else 0
        used_pct = max(0, min(100, 100 - rem_pct))
        window = bucket.get("window")
        win_min = 300 if window == "5h" else 10080
        return {
            "label": window or ("5h" if win_min == 300 else "7d"),
            "used_percent": used_pct,
            "remaining_percent": rem_pct,
            "window_minutes": win_min,
            "resets_at": reset_ts,
            "is_reset": (reset_ts is not None and reset_ts <= time.time()),
            "is_live": True,
            "is_exhausted": frac is None or rem_pct == 0,
        }

    for port in ports:
        for proto in ("http", "https"):
            # 1. First attempt: RetrieveUserQuotaSummary (official real-time quota for 5h and weekly)
            summary_url = f"{proto}://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
            req = Request(summary_url, data=body, headers=headers)
            try:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                with urlopen(req, context=ctx, timeout=timeout_sec) as res:  # nosec B310
                    data = json.loads(res.read().decode("utf-8", "ignore"))
                groups = data.get("response", {}).get("groups", [])
                if groups:
                    gem_res: dict[str, Any] = {"primary": None, "secondary": None}
                    cld_res: dict[str, Any] = {"primary": None, "secondary": None}
                    arg_res: dict[str, Any] = {"primary": None, "secondary": None}
                    for g in groups:
                        disp = (g.get("displayName") or "").lower()
                        buckets = g.get("buckets", [])
                        target = None
                        if "argon" in disp:
                            target = arg_res
                        elif "gemini" in disp:
                            target = gem_res
                        elif any(x in disp for x in ("claude", "gpt", "3p")):
                            target = cld_res
                        if target is not None:
                            for b in buckets:
                                win = b.get("window")
                                parsed = _parse_bucket(b)
                                if win == "5h":
                                    target["primary"] = parsed
                                elif win == "weekly":
                                    target["secondary"] = parsed
                    if (
                        gem_res["primary"]
                        or gem_res["secondary"]
                        or cld_res["primary"]
                        or cld_res["secondary"]
                        or arg_res["primary"]
                        or arg_res["secondary"]
                    ):
                        return {
                            "source": "live_api",
                            "plan": "Google AI Pro",
                            "gemini": gem_res,
                            "claude_gpt": cld_res,
                            "argon": arg_res,
                        }
            except Exception:  # nosec B110
                pass

            # 2. Fallback: GetUserStatus
            status_url = f"{proto}://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/GetUserStatus"
            req = Request(status_url, data=body, headers=headers)
            try:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                with urlopen(req, context=ctx, timeout=timeout_sec) as res:  # nosec B310
                    data = json.loads(res.read().decode("utf-8", "ignore"))

                user_status = data.get("userStatus", {})
                plan_name = user_status.get("userTier", {}).get("name") or "Google AI Pro"
                configs = user_status.get("cascadeModelConfigData", {}).get(
                    "clientModelConfigs", []
                )

                gem_res = {"primary": None, "secondary": None}
                cld_res = {"primary": None, "secondary": None}
                arg_res = {"primary": None, "secondary": None}
                now_ts = time.time()
                for c in configs:
                    lbl = (c.get("label") or "").lower()
                    q = c.get("quotaInfo")
                    if not q:
                        continue
                    parsed = _parse_bucket(q)
                    target = None
                    if "argon" in lbl:
                        target = arg_res
                    elif any(x in lbl for x in ("claude", "gpt", "opus", "sonnet")):
                        target = cld_res
                    elif "gemini" in lbl:
                        target = gem_res
                    if target is not None:
                        diff_sec = (parsed["resets_at"] or 0) - now_ts
                        if diff_sec > 24 * 3600:
                            if not target["secondary"]:
                                target["secondary"] = parsed
                        else:
                            if not target["primary"]:
                                target["primary"] = parsed

                if (
                    gem_res["primary"]
                    or gem_res["secondary"]
                    or cld_res["primary"]
                    or cld_res["secondary"]
                    or arg_res["primary"]
                    or arg_res["secondary"]
                ):
                    return {
                        "source": "live_api",
                        "plan": plan_name,
                        "gemini": gem_res,
                        "claude_gpt": cld_res,
                        "argon": arg_res,
                    }
            except Exception:  # nosec B110
                pass
    return None


def get_cached_agy_live_status(max_age_sec: float = 5.0) -> dict[str, Any] | None:
    """キャッシュされた Antigravity language_server のリアルタイムステータスを返します。

    Args:
        max_age_sec (float, optional): キャッシュの有効期限（秒）。デフォルトは 5.0。

    Returns:
        dict[str, Any] | None: Antigravity のステータス情報辞書、または取得不可時は None。
    """
    global AGY_LIVE_CACHE
    now = time.time()
    if AGY_LIVE_CACHE is not None and (now - AGY_LIVE_CACHE[0]) < max_age_sec:
        return AGY_LIVE_CACHE[1]
    status = detect_antigravity_live_status()
    AGY_LIVE_CACHE = (now, status)
    return status


def agy_usage_cost(model: Any, usage: dict[str, int]) -> float | None:
    """Antigravity セッションのトークン消費量から API 相当の推定費用（USD）を算出します。

    Args:
        model (Any): 使用されたモデル名。
        usage (dict[str, int]): トークン使用量の辞書（"input_tokens", "output_tokens"）。

    Returns:
        float | None: 推定費用（USD、小数点以下4桁）。トークンが 0 の場合は None。
    """
    model_str = str(model or "").lower()
    in_tok = usage.get("input_tokens", 0)
    out_tok = usage.get("output_tokens", 0)
    if in_tok == 0 and out_tok == 0:
        return None
    price = None
    for key, val in sorted(AGY_PRICES_PER_MILLION.items(), key=lambda x: -len(x[0])):
        if key in model_str:
            price = val
            break
    if price is None:
        if "oss" in model_str:
            price = (0.0, 0.0)
        elif any(k in model_str for k in ("argon", "pro")):
            price = (1.25, 5.00)
        elif "opus" in model_str:
            price = (4.0, 20.0)
        elif "sonnet" in model_str:
            price = (2.0, 10.0)
        elif "fable" in model_str:
            price = (10.0, 50.0)
        elif "haiku" in model_str:
            price = (0.10, 0.50)
        else:
            price = (0.15, 0.60)
    in_price, out_price = price
    cost = (in_tok * in_price + out_tok * out_price) / 1_000_000
    return round(cost, 4)


def summarize_agy_session(brain_dir: Path) -> dict[str, Any] | None:
    """Antigravity の単一セッション（brain ディレクトリ）のログから利用情報を抽出します。

    Args:
        brain_dir (Path): brain 会話ディレクトリのパス
            （例: ~/.gemini/antigravity/brain/<uuid>）。

    Returns:
        dict[str, Any] | None: セッションサマリー辞書。有効なレコードが存在しない場合は None。
    """
    transcript = brain_dir / ".system_generated" / "logs" / "transcript_full.jsonl"
    if not transcript.is_file():
        transcript = brain_dir / ".system_generated" / "logs" / "transcript.jsonl"
    if not transcript.is_file():
        return None
    summary = empty_agy_summary()
    saw_record = False
    step_count = 0
    input_chars = 0
    output_chars = 0
    current_model = "Gemini 2.5 Flash"
    detected_model = None

    now_ts = datetime.now(UTC).timestamp()
    gem_5h = gem_7d = gem_total = 0
    cld_5h = cld_7d = cld_total = 0
    arg_5h = arg_7d = arg_total = 0

    try:
        lines = transcript.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        saw_record = True
        created = record.get("created_at")
        created_ts = None
        if created:
            summary["observed_at"] = created
            try:
                created_ts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError):
                created_ts = None

        rec_type = record.get("type")
        content = str(record.get("content", ""))

        if rec_type == "USER_INPUT":
            input_chars += len(content)
            m = re.findall(
                r"setting `Model Selection` from .*? to (.*?)(?:\.\s*No need|\.\n|\n|$)",
                content,
            )
            if m:
                detected_model = m[-1].strip()
                current_model = detected_model
            if summary["task"]["prompt"] is None:
                prompt = _extract_agy_user_prompt(content)
                if prompt:
                    summary["task"]["prompt"] = prompt
        elif rec_type == "PLANNER_RESPONSE":
            step_count += 1
            fam = classify_agy_model(current_model)
            diff = (now_ts - created_ts) if created_ts else 0.0
            if fam == "argon":
                arg_total += 1
                if diff <= 5 * 3600:
                    arg_5h += 1
                if diff <= 7 * 86400:
                    arg_7d += 1
            elif fam == "gemini":
                gem_total += 1
                if diff <= 5 * 3600:
                    gem_5h += 1
                if diff <= 7 * 86400:
                    gem_7d += 1
            else:
                cld_total += 1
                if diff <= 5 * 3600:
                    cld_5h += 1
                if diff <= 7 * 86400:
                    cld_7d += 1

            output_chars += len(content)
            thinking = str(record.get("thinking", ""))
            output_chars += len(thinking)
        elif rec_type == "GENERIC":
            input_chars += len(content)

    if not saw_record:
        return None
    summary["model"] = detected_model or current_model
    summary["model_family"] = classify_agy_model(summary["model"])
    summary["usage"]["steps"] = step_count
    summary["usage"]["steps_5h"] = gem_5h + cld_5h + arg_5h
    summary["usage"]["steps_7d"] = gem_7d + cld_7d + arg_7d
    summary["usage"]["gemini"] = {
        "steps": gem_total,
        "steps_5h": gem_5h,
        "steps_7d": gem_7d,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
    summary["usage"]["claude_gpt"] = {
        "steps": cld_total,
        "steps_5h": cld_5h,
        "steps_7d": cld_7d,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
    summary["usage"]["argon"] = {
        "steps": arg_total,
        "steps_5h": arg_5h,
        "steps_7d": arg_7d,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
    in_tok = int(input_chars / 3.8)
    out_tok = int(output_chars / 3.8)
    summary["usage"]["input_tokens"] = in_tok
    summary["usage"]["output_tokens"] = out_tok
    summary["usage"]["total_tokens"] = in_tok + out_tok
    summary["estimated_cost_usd"] = agy_usage_cost(summary["model"], summary["usage"])
    return summary


def _extract_agy_user_prompt(content: str) -> str | None:
    """Antigravity の USER_INPUT レコードから本来のユーザーリクエスト行を抽出します。

    Args:
        content (str): トランスクリプトレコード内の未加工コンテンツ文字列。

    Returns:
        str | None: 抽出されたユーザーリクエストの先頭行（最大160文字）、または見つからない場合は None。
    """
    in_request = False
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if stripped == "<USER_REQUEST>":
            in_request = True
            continue
        if stripped == "</USER_REQUEST>":
            break
        if in_request and stripped:
            return stripped[:160]
    return None


def claude_usage_path(state_home: Path) -> Path:
    """手動キャプチャされた Claude の利用量データ（JSON）のローカル保存先パスを返します。

    Args:
        state_home (Path): 状態保存用ベースディレクトリ（XDG_STATE_HOME 等）。

    Returns:
        Path: claude-usage.json のファイルパス。
    """
    return state_home / "ai-vitals" / "claude-usage.json"


def agy_usage_path(state_home: Path) -> Path:
    """手動キャプチャされた Antigravity の利用量データ（JSON）のローカル保存先パスを返します。

    Args:
        state_home (Path): 状態保存用ベースディレクトリ（XDG_STATE_HOME 等）。

    Returns:
        Path: agy-usage.json のファイルパス。
    """
    return state_home / "ai-vitals" / "agy-usage.json"


def monthly_settings_path(state_home: Path) -> Path:
    """月間予算・上限設定データ（JSON）のローカル保存先パスを返します。

    Args:
        state_home (Path): 状態保存用ベースディレクトリ（XDG_STATE_HOME 等）。

    Returns:
        Path: monthly-settings.json のファイルパス。
    """
    return state_home / "ai-vitals" / "monthly-settings.json"


def load_claude_status_cache(claude_home: Path) -> dict[str, Any] | None:
    """Claude のステータスキャッシュファイルが存在する場合、そこから利用枠制限を読み込みます。

    Args:
        claude_home (Path): Claude のホームディレクトリパス（~/.claude）。

    Returns:
        dict[str, Any] | None: 読み込まれた primary（5h）および secondary（7d）の制限情報辞書。
            ファイルが存在しないか解析に失敗した場合は None。
    """
    cache_path = claude_home / "vscode-claude-status-cache.json"
    if not cache_path.is_file():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    usage = data.get("usageData")
    if not isinstance(usage, dict):
        return None

    def _to_percent(val: Any) -> int | None:
        if val is None:
            return None
        try:
            num = float(val)
            if 0 < num <= 1.0:
                return round(num * 100)
            return max(0, min(100, round(num)))
        except (ValueError, TypeError):
            return None

    pri = None
    u5 = _to_percent(usage.get("utilization5h"))
    r5 = usage.get("reset5hAt")
    if u5 is not None or r5 is not None:
        pri = normalize_limit(
            {
                "used_percent": u5 if u5 is not None else 0,
                "window_minutes": 300,
                "resets_at": int(r5) if r5 else None,
            }
        )

    sec = None
    u7 = _to_percent(usage.get("utilization7d"))
    r7 = usage.get("reset7dAt")
    if u7 is not None or r7 is not None:
        sec = normalize_limit(
            {
                "used_percent": u7 if u7 is not None else 0,
                "window_minutes": 10080,
                "resets_at": int(r7) if r7 else None,
            }
        )

    if pri is None and sec is None:
        return None

    return {
        "primary": pri,
        "secondary": sec,
        "source": "status_cache",
    }


def load_claude_usage(state_home: Path, claude_home: Path | None = None) -> dict[str, Any]:
    """Claude のステータスキャッシュまたは手動保存ファイルから利用量スナップショットを読み込みます。

    Args:
        state_home (Path): 状態保存用ベースディレクトリ。
        claude_home (Path | None, optional): Claude のホームディレクトリパス。デフォルトは None。

    Returns:
        dict[str, Any]: primary、secondary、source（"status_cache" または "manual"）を含む辞書。
    """
    if claude_home is not None:
        cached = load_claude_status_cache(claude_home)
        if cached is not None:
            return cached

    empty = {"primary": None, "secondary": None, "source": None}
    try:
        value = json.loads(claude_usage_path(state_home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Fallback to legacy codex-pulse directory
        legacy = state_home / "codex-pulse" / "claude-usage.json"
        try:
            value = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return empty
    if not isinstance(value, dict):
        return empty
    return {
        "primary": normalize_limit(value.get("primary")),
        "secondary": normalize_limit(value.get("secondary")),
        "source": "manual",
    }


def load_agy_usage(state_home: Path) -> dict[str, Any]:
    """手動設定等により保存された Antigravity の利用量スナップショットを読み込みます。

    Args:
        state_home (Path): 状態保存用ベースディレクトリ。

    Returns:
        dict[str, Any]: Gemini、Claude/GPT、Argon の各枠情報辞書。
    """
    empty = {
        "gemini": {"primary": None, "secondary": None},
        "claude_gpt": {"primary": None, "secondary": None},
        "argon": {"primary": None, "secondary": None},
        "rate_limit": None,
    }
    try:
        value = json.loads(agy_usage_path(state_home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(value, dict):
        return empty

    gemini_raw = value.get("gemini")
    claude_raw = value.get("claude_gpt") or value.get("claude")
    argon_raw = value.get("argon")
    if isinstance(gemini_raw, dict) or isinstance(claude_raw, dict) or isinstance(argon_raw, dict):
        gem_pri = normalize_limit((gemini_raw or {}).get("primary"))
        gem_sec = normalize_limit((gemini_raw or {}).get("secondary"))
        cld_pri = normalize_limit((claude_raw or {}).get("primary"))
        cld_sec = normalize_limit((claude_raw or {}).get("secondary"))
        arg_pri = normalize_limit((argon_raw or {}).get("primary"))
        arg_sec = normalize_limit((argon_raw or {}).get("secondary"))
        return {
            "gemini": {"primary": gem_pri, "secondary": gem_sec},
            "claude_gpt": {"primary": cld_pri, "secondary": cld_sec},
            "argon": {"primary": arg_pri, "secondary": arg_sec},
            "rate_limit": gem_sec or normalize_limit(value.get("rate_limit")),
        }

    legacy_limit = normalize_limit(value.get("rate_limit"))
    return {
        "gemini": {"primary": None, "secondary": legacy_limit},
        "claude_gpt": {"primary": None, "secondary": None},
        "argon": {"primary": None, "secondary": None},
        "rate_limit": legacy_limit,
    }


def save_claude_usage(
    state_home: Path,
    *,
    primary_used_percent: int,
    primary_resets_at: int,
    secondary_used_percent: int,
    secondary_resets_at: int,
) -> None:
    """手動で確認された Claude の利用率およびリセット日時をローカルに保存します。

    認証情報やトークン文字列は保持せず、利用率の数値とタイムスタンプのみを記録します。

    Args:
        state_home (Path): 状態保存用ベースディレクトリ。
        primary_used_percent (int): 5時間枠の使用率パーセント。
        primary_resets_at (int): 5時間枠のリセット予定時刻（UNIXタイムスタンプ）。
        secondary_used_percent (int): 7日間枠の使用率パーセント。
        secondary_resets_at (int): 7日間枠のリセット予定時刻（UNIXタイムスタンプ）。
    """
    path = claude_usage_path(state_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "primary": {
                    "used_percent": primary_used_percent,
                    "window_minutes": 300,
                    "resets_at": primary_resets_at,
                },
                "secondary": {
                    "used_percent": secondary_used_percent,
                    "window_minutes": 10080,
                    "resets_at": secondary_resets_at,
                },
            }
        ),
        encoding="utf-8",
    )


def save_agy_usage(
    state_home: Path,
    *,
    gemini_5h_used: int | None = None,
    gemini_5h_resets_at: int | None = None,
    gemini_7d_used: int | None = None,
    gemini_7d_resets_at: int | None = None,
    claude_5h_used: int | None = None,
    claude_5h_resets_at: int | None = None,
    claude_7d_used: int | None = None,
    claude_7d_resets_at: int | None = None,
    argon_7d_used: int | None = None,
    argon_7d_resets_at: int | None = None,
    used_percent: int | None = None,
    resets_at: int | None = None,
    window_minutes: int = 10080,
) -> None:
    """手動で確認された Antigravity の利用状況をローカルに保存します。

    認証情報やトークン文字列は保持せず、利用率の数値とタイムスタンプのみを記録します。

    Args:
        state_home (Path): 状態保存用ベースディレクトリ。
        gemini_5h_used (int | None, optional): Gemini 5時間枠の使用率パーセント。
        gemini_5h_resets_at (int | None, optional): Gemini 5時間枠のリセット時刻（UNIXタイムスタンプ）。
        gemini_7d_used (int | None, optional): Gemini 7日間枠の使用率パーセント。
        gemini_7d_resets_at (int | None, optional): Gemini 7日間枠のリセット時刻（UNIXタイムスタンプ）。
        claude_5h_used (int | None, optional): Claude/GPT 5時間枠の使用率パーセント。
        claude_5h_resets_at (int | None, optional): Claude/GPT 5時間枠のリセット時刻（UNIXタイムスタンプ）。
        claude_7d_used (int | None, optional): Claude/GPT 7日間枠の使用率パーセント。
        claude_7d_resets_at (int | None, optional): Claude/GPT 7日間枠のリセット時刻（UNIXタイムスタンプ）。
        argon_7d_used (int | None, optional): Gemini 4 Argon 7日間枠の使用率パーセント。
        argon_7d_resets_at (int | None, optional): Gemini 4 Argon 7日間枠のリセット時刻（UNIXタイムスタンプ）。
        used_percent (int | None, optional): レガシー互換用の使用率パーセント。
        resets_at (int | None, optional): レガシー互換用のリセット時刻（UNIXタイムスタンプ）。
        window_minutes (int, optional): レガシー互換用のウィンドウ期間（分）。デフォルトは 10080（7日）。
    """
    existing = load_agy_usage(state_home)
    gem_pri = dict(existing["gemini"]["primary"]) if existing["gemini"]["primary"] else {}
    gem_sec = dict(existing["gemini"]["secondary"]) if existing["gemini"]["secondary"] else {}
    cld_pri = dict(existing["claude_gpt"]["primary"]) if existing["claude_gpt"]["primary"] else {}
    cld_sec = (
        dict(existing["claude_gpt"]["secondary"]) if existing["claude_gpt"]["secondary"] else {}
    )
    arg_pri = dict(existing["argon"]["primary"]) if existing.get("argon", {}).get("primary") else {}
    arg_sec = (
        dict(existing["argon"]["secondary"]) if existing.get("argon", {}).get("secondary") else {}
    )

    now = int(time.time())
    if used_percent is not None:
        if window_minutes == 300:
            gemini_5h_used = used_percent
            gemini_5h_resets_at = resets_at or gemini_5h_resets_at
        else:
            gemini_7d_used = used_percent
            gemini_7d_resets_at = resets_at or gemini_7d_resets_at

    def _fresh_resets_at(specified: int | None, existing_at: Any, default_seconds: int) -> int:
        if specified:
            return specified
        if isinstance(existing_at, (int, float)) and existing_at > now:
            return int(existing_at)
        return now + default_seconds

    if gemini_5h_used is not None:
        gem_pri = {
            "used_percent": gemini_5h_used,
            "window_minutes": 300,
            "resets_at": _fresh_resets_at(gemini_5h_resets_at, gem_pri.get("resets_at"), 5 * 3600),
        }
    if gemini_7d_used is not None:
        gem_sec = {
            "used_percent": gemini_7d_used,
            "window_minutes": 10080,
            "resets_at": _fresh_resets_at(gemini_7d_resets_at, gem_sec.get("resets_at"), 7 * 86400),
        }
    if claude_5h_used is not None:
        cld_pri = {
            "used_percent": claude_5h_used,
            "window_minutes": 300,
            "resets_at": _fresh_resets_at(claude_5h_resets_at, cld_pri.get("resets_at"), 5 * 3600),
        }
    if claude_7d_used is not None:
        cld_sec = {
            "used_percent": claude_7d_used,
            "window_minutes": 10080,
            "resets_at": _fresh_resets_at(claude_7d_resets_at, cld_sec.get("resets_at"), 7 * 86400),
        }
    if argon_7d_used is not None:
        arg_sec = {
            "used_percent": argon_7d_used,
            "window_minutes": 10080,
            "resets_at": _fresh_resets_at(argon_7d_resets_at, arg_sec.get("resets_at"), 7 * 86400),
        }

    data = {
        "gemini": {
            "primary": gem_pri or None,
            "secondary": gem_sec or None,
        },
        "claude_gpt": {
            "primary": cld_pri or None,
            "secondary": cld_sec or None,
        },
        "argon": {
            "primary": arg_pri or None,
            "secondary": arg_sec or None,
        },
        "rate_limit": gem_sec or None,
    }
    path = agy_usage_path(state_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def detect_claude_billing_day(claude_home: Path | None = None) -> int | None:
    """Claude Code の設定 (~/.claude.json) からサブスクリプション作成日（締め日）を自動検出します。

    Args:
        claude_home (Path | None, optional): Claude のホームディレクトリ。

    Returns:
        int | None: 検出された日付（1〜31）、未検出時は None。
    """
    path = (claude_home or default_claude_home()) / ".." / ".claude.json"
    if not path.is_file():
        alt_path = Path.home() / ".claude.json"
        if not alt_path.is_file():
            return None
        path = alt_path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        sub = data.get("oauthAccount", {}).get("subscriptionCreatedAt")
        if sub and isinstance(sub, str):
            dt = datetime.fromisoformat(sub.replace("Z", "+00:00"))
            return dt.day
    except Exception:
        pass
    return None


def default_monthly_settings(claude_home: Path | None = None) -> dict[str, Any]:
    """デフォルトの月間設定辞書を返します。

    Args:
        claude_home (Path | None, optional): Claude のホームディレクトリ。

    Returns:
        dict[str, Any]: 締め日（1）、各ツールの予算・トークン上限を含む辞書。
    """
    return {
        "billing_day": 1,
        "codex": {"budget_usd": 0.0, "token_limit": 0},
        "claude": {"budget_usd": 0.0, "token_limit": 0},
        "agy": {"budget_usd": 0.0, "token_limit": 0},
        "total_budget_usd": 0.0,
        "total_token_limit": 0,
    }


def load_monthly_settings(state_home: Path) -> dict[str, Any]:
    """保存された月間予算・上限設定を読み込みます。

    Args:
        state_home (Path): 状態保存用ベースディレクトリ。

    Returns:
        dict[str, Any]: 締め日および各ツールの月間設定辞書。
    """
    path = monthly_settings_path(state_home)
    defaults = default_monthly_settings()
    if not path.is_file():
        return defaults
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defaults
    if not isinstance(data, dict):
        return defaults

    result = dict(defaults)
    try:
        bday = int(data.get("billing_day", 1))
        result["billing_day"] = max(1, min(31, bday))
    except (ValueError, TypeError):
        result["billing_day"] = 1

    for tool in ("codex", "claude", "agy"):
        tool_conf = data.get(tool) if isinstance(data.get(tool), dict) else {}
        b_usd = float(tool_conf.get("budget_usd") or 0.0) if tool_conf else 0.0
        t_lim = int(tool_conf.get("token_limit") or 0) if tool_conf else 0
        result[tool] = {"budget_usd": max(0.0, b_usd), "token_limit": max(0, t_lim)}

    tot_b = float(data.get("total_budget_usd") or 0.0)
    tot_t = int(data.get("total_token_limit") or 0)
    result["total_budget_usd"] = max(0.0, tot_b)
    result["total_token_limit"] = max(0, tot_t)
    return result


def save_monthly_settings(state_home: Path, settings: dict[str, Any]) -> None:
    """月間予算・上限設定をローカルに保存します。

    Args:
        state_home (Path): 状態保存用ベースディレクトリ。
        settings (dict[str, Any]): 保存する設定辞書。
    """
    path = monthly_settings_path(state_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    cur = load_monthly_settings(state_home)

    if "billing_day" in settings and settings["billing_day"] is not None:
        try:
            cur["billing_day"] = max(1, min(31, int(settings["billing_day"])))
        except (ValueError, TypeError):
            pass

    for tool in ("codex", "claude", "agy"):
        if tool in settings and isinstance(settings[tool], dict):
            t_conf = settings[tool]
            if "budget_usd" in t_conf and t_conf["budget_usd"] is not None:
                try:
                    cur[tool]["budget_usd"] = max(0.0, float(t_conf["budget_usd"]))
                except (ValueError, TypeError):
                    pass
            if "token_limit" in t_conf and t_conf["token_limit"] is not None:
                try:
                    cur[tool]["token_limit"] = max(0, int(t_conf["token_limit"]))
                except (ValueError, TypeError):
                    pass

    if "total_budget_usd" in settings and settings["total_budget_usd"] is not None:
        try:
            cur["total_budget_usd"] = max(0.0, float(settings["total_budget_usd"]))
        except (ValueError, TypeError):
            pass
    if "total_token_limit" in settings and settings["total_token_limit"] is not None:
        try:
            cur["total_token_limit"] = max(0, int(settings["total_token_limit"]))
        except (ValueError, TypeError):
            pass

    path.write_text(json.dumps(cur, indent=2), encoding="utf-8")


def get_monthly_window(
    billing_day: int = 1, now: datetime | None = None
) -> tuple[datetime, datetime]:
    """請求締め日に基づく当月の集計開始日時と終了日時（UTC）を計算します。

    Args:
        billing_day (int, optional): 締め日（開始日、1〜31）。デフォルトは 1。
        now (datetime | None, optional): 基準日時。None の場合は現在日時（UTC）。

    Returns:
        tuple[datetime, datetime]: (当月開始日時, 次月開始日時) の UTC datetime タプル。
    """
    if now is None:
        now = datetime.now(UTC)
    billing_day = max(1, min(31, int(billing_day)))

    max_days_current = calendar.monthrange(now.year, now.month)[1]
    day_in_cur = min(billing_day, max_days_current)
    candidate_cur = datetime(now.year, now.month, day_in_cur, 0, 0, 0, tzinfo=UTC)

    if now >= candidate_cur:
        start_dt = candidate_cur
        next_year = now.year + (1 if now.month == 12 else 0)
        next_month = 1 if now.month == 12 else now.month + 1
        max_days_next = calendar.monthrange(next_year, next_month)[1]
        day_in_next = min(billing_day, max_days_next)
        end_dt = datetime(next_year, next_month, day_in_next, 0, 0, 0, tzinfo=UTC)
    else:
        prev_year = now.year - (1 if now.month == 1 else 0)
        prev_month = 12 if now.month == 1 else now.month - 1
        max_days_prev = calendar.monthrange(prev_year, prev_month)[1]
        day_in_prev = min(billing_day, max_days_prev)
        start_dt = datetime(prev_year, prev_month, day_in_prev, 0, 0, 0, tzinfo=UTC)
        end_dt = candidate_cur

    return start_dt, end_dt


def calculate_burn_rate(
    used_percent: float | None, time_progress_percent: float
) -> str | None:
    """時間の経過割合と使用率の差から消費ペース（burn rate）を判定します。

    Args:
        used_percent (float | None): 上限または予算の消費率（%）。None の場合は判定不可。
        time_progress_percent (float): 月間期間内の時間経過率（%）。

    Returns:
        str | None: "warn"（ハイペース）、"safe"（安全ペース）、"normal"（適正ペース）、または None。
    """
    if used_percent is None:
        return None
    diff = used_percent - time_progress_percent
    if diff > 15:
        return "warn"
    if diff < -10:
        return "safe"
    return "normal"


def _parse_iso_ts(ts_str: Any) -> float | None:
    """ISO 形式の日時文字列を UNIX タイムスタンプに変換します。"""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(str(ts_str).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


# セッション走査の高速化キャッシュ: Path -> (mtime, size, parsed_summary)
SESSION_SUMMARY_CACHE: dict[Path, tuple[float, int, dict[str, Any] | None]] = {}


def cached_summarize_session(path: Path) -> dict[str, Any] | None:
    """ファイル更新時刻を検証しつつキャッシュを利用して Codex セッションを抽出します。"""
    try:
        st = path.stat()
        mtime, size = st.st_mtime, st.st_size
    except OSError:
        return None
    cached = SESSION_SUMMARY_CACHE.get(path)
    if cached is not None and cached[0] == mtime and cached[1] == size:
        return cached[2]
    res = summarize_session(path)
    SESSION_SUMMARY_CACHE[path] = (mtime, size, res)
    return res


def cached_summarize_claude_session(path: Path) -> dict[str, Any] | None:
    """ファイル更新時刻を検証しつつキャッシュを利用して Claude セッションを抽出します。"""
    try:
        st = path.stat()
        mtime, size = st.st_mtime, st.st_size
    except OSError:
        return None
    cached = SESSION_SUMMARY_CACHE.get(path)
    if cached is not None and cached[0] == mtime and cached[1] == size:
        return cached[2]
    res = summarize_claude_session(path)
    SESSION_SUMMARY_CACHE[path] = (mtime, size, res)
    return res


def cached_summarize_agy_session(brain_dir: Path) -> dict[str, Any] | None:
    """ログ更新時刻を検証しつつキャッシュを利用して Antigravity セッションを抽出します。"""
    target = brain_dir / ".system_generated" / "logs" / "transcript_full.jsonl"
    if not target.is_file():
        target = brain_dir / ".system_generated" / "logs" / "transcript.jsonl"
    if not target.is_file():
        return None
    try:
        st = target.stat()
        mtime, size = st.st_mtime, st.st_size
    except OSError:
        return None
    cached = SESSION_SUMMARY_CACHE.get(brain_dir)
    if cached is not None and cached[0] == mtime and cached[1] == size:
        return cached[2]
    res = summarize_agy_session(brain_dir)
    SESSION_SUMMARY_CACHE[brain_dir] = (mtime, size, res)
    return res


def collect_monthly_usage(
    codex_home: Path,
    state_home: Path,
    claude_home: Path | None = None,
    agy_home: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """請求締め日に基づく当月の各AIツールおよび全体の利用量・予算進捗を集計します。

    Args:
        codex_home (Path): Codex のホームディレクトリ。
        state_home (Path): 状態保存用ディレクトリ。
        claude_home (Path | None, optional): Claude のホームディレクトリ。
        agy_home (Path | None, optional): Antigravity のホームディレクトリ。
        now (datetime | None, optional): 基準日時（テスト用）。デフォルトは現在（UTC）。

    Returns:
        dict[str, Any]: 集計期間、全体および各ツール（Codex, Claude, Antigravity）の
            月間タスク数、トークン数、推定コスト、予算/上限進捗率、消費ペースを含む辞書。
    """
    if now is None:
        now = datetime.now(UTC)
    settings = load_monthly_settings(state_home)
    billing_day = settings.get("billing_day", 1)
    detected_billing_day = detect_claude_billing_day(claude_home)
    start_dt, end_dt = get_monthly_window(billing_day, now)
    start_ts = start_dt.timestamp()
    end_ts = end_dt.timestamp()

    days_total = max(1.0, (end_dt - start_dt).total_seconds() / 86400.0)
    days_elapsed = max(0.0, min(days_total, (now - start_dt).total_seconds() / 86400.0))
    time_progress_percent = round((days_elapsed / days_total) * 100.0, 1)

    # 1. Codex
    codex_sessions_dir = codex_home / "sessions"
    codex_tasks = 0
    codex_in = codex_out = codex_tok = 0
    codex_cost = 0.0

    if codex_sessions_dir.is_dir():
        for path in codex_sessions_dir.glob("**/*.jsonl"):
            try:
                if path.stat().st_mtime < start_ts:
                    continue
            except OSError:
                continue
            s = cached_summarize_session(path)
            if not s or s.get("auxiliary"):
                continue
            obs = _parse_iso_ts(s.get("observed_at"))
            if obs is None:
                try:
                    obs = path.stat().st_mtime
                except OSError:
                    continue
            if start_ts <= obs < end_ts:
                usage = s.get("usage", {})
                codex_in += usage.get("input_tokens", 0)
                codex_out += usage.get("output_tokens", 0)
                codex_tok += usage.get("total_tokens", 0)
                if s.get("estimated_cost_usd") is not None:
                    codex_cost += s["estimated_cost_usd"]
                if s.get("task", {}).get("prompt"):
                    codex_tasks += 1

    # 2. Claude Code
    claude_root = claude_home or default_claude_home()
    claude_projects_dir = claude_root / "projects"
    claude_tasks = 0
    claude_in = claude_out = claude_tok = 0
    claude_cost = 0.0

    if claude_projects_dir.is_dir():
        for path in claude_projects_dir.glob("**/*.jsonl"):
            try:
                if path.stat().st_mtime < start_ts:
                    continue
            except OSError:
                continue
            s = cached_summarize_claude_session(path)
            if not s:
                continue
            obs = _parse_iso_ts(s.get("observed_at"))
            if obs is None:
                try:
                    obs = path.stat().st_mtime
                except OSError:
                    continue
            if start_ts <= obs < end_ts:
                usage = s.get("usage", {})
                claude_in += usage.get("input_tokens", 0)
                claude_out += usage.get("output_tokens", 0)
                claude_tok += usage.get("total_tokens", 0)
                c_cost = claude_usage_cost(s.get("model"), usage)
                if c_cost:
                    claude_cost += (c_cost["minimum_usd"] + c_cost["maximum_usd"]) / 2.0
                if s.get("task", {}).get("prompt"):
                    claude_tasks += 1

    # 3. Antigravity
    agy_root = agy_home or default_agy_home()
    agy_brain = agy_root / "brain"
    agy_tasks = 0
    agy_in = agy_out = agy_tok = 0
    agy_steps = agy_gem_steps = agy_cld_steps = agy_arg_steps = 0
    agy_cost = 0.0

    if agy_brain.is_dir():
        for d in agy_brain.iterdir():
            if not d.is_dir() or d.name.startswith("."):
                continue
            try:
                if d.stat().st_mtime < start_ts:
                    continue
            except OSError:
                continue
            s = cached_summarize_agy_session(d)
            if not s:
                continue
            obs = _parse_iso_ts(s.get("observed_at"))
            if obs is None:
                try:
                    obs = d.stat().st_mtime
                except OSError:
                    continue
            if start_ts <= obs < end_ts:
                usage = s.get("usage", {})
                agy_in += usage.get("input_tokens", 0)
                agy_out += usage.get("output_tokens", 0)
                agy_tok += usage.get("total_tokens", 0)
                agy_steps += usage.get("steps", 0)
                agy_gem_steps += usage.get("gemini", {}).get("steps", 0)
                agy_cld_steps += usage.get("claude_gpt", {}).get("steps", 0)
                agy_arg_steps += usage.get("argon", {}).get("steps", 0)
                if s.get("estimated_cost_usd") is not None:
                    agy_cost += s["estimated_cost_usd"]
                if s.get("task", {}).get("prompt"):
                    agy_tasks += 1

    def _calc_tool(tasks: int, in_tok: int, out_tok: int, total_tok: int, cost: float, tool_key: str) -> dict[str, Any]:
        conf = settings.get(tool_key, {})
        budget = float(conf.get("budget_usd") or 0.0)
        tok_limit = int(conf.get("token_limit") or 0)
        cost_pct = round((cost / budget) * 100.0, 1) if budget > 0 else None
        tok_pct = round((total_tok / tok_limit) * 100.0, 1) if tok_limit > 0 else None
        burn = calculate_burn_rate(cost_pct if cost_pct is not None else tok_pct, time_progress_percent)
        return {
            "tasks": tasks,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "total_tokens": total_tok,
            "estimated_cost_usd": round(cost, 4),
            "budget_usd": budget,
            "token_limit": tok_limit,
            "used_cost_percent": cost_pct,
            "used_tokens_percent": tok_pct,
            "burn_rate": burn,
        }

    codex_data = _calc_tool(codex_tasks, codex_in, codex_out, codex_tok, codex_cost, "codex")
    claude_data = _calc_tool(claude_tasks, claude_in, claude_out, claude_tok, claude_cost, "claude")
    agy_data = _calc_tool(agy_tasks, agy_in, agy_out, agy_tok, agy_cost, "agy")
    agy_data["steps"] = agy_steps
    agy_data["gemini_steps"] = agy_gem_steps
    agy_data["claude_gpt_steps"] = agy_cld_steps
    agy_data["argon_steps"] = agy_arg_steps

    total_tasks = codex_tasks + claude_tasks + agy_tasks
    total_tokens = codex_tok + claude_tok + agy_tok
    total_cost = codex_cost + claude_cost + agy_cost

    total_budget = float(settings.get("total_budget_usd") or 0.0)
    if total_budget <= 0:
        total_budget = codex_data["budget_usd"] + claude_data["budget_usd"] + agy_data["budget_usd"]

    total_tok_limit = int(settings.get("total_token_limit") or 0)
    if total_tok_limit <= 0:
        total_tok_limit = codex_data["token_limit"] + claude_data["token_limit"] + agy_data["token_limit"]

    tot_cost_pct = round((total_cost / total_budget) * 100.0, 1) if total_budget > 0 else None
    tot_tok_pct = round((total_tokens / total_tok_limit) * 100.0, 1) if total_tok_limit > 0 else None
    tot_burn = calculate_burn_rate(tot_cost_pct if tot_cost_pct is not None else tot_tok_pct, time_progress_percent)

    total_data = {
        "tasks": total_tasks,
        "total_tokens": total_tokens,
        "estimated_cost_usd": round(total_cost, 4),
        "budget_usd": total_budget,
        "token_limit": total_tok_limit,
        "used_cost_percent": tot_cost_pct,
        "used_tokens_percent": tot_tok_pct,
        "burn_rate": tot_burn,
    }

    return {
        "start_at": start_dt.isoformat(),
        "end_at": end_dt.isoformat(),
        "end_ts": end_ts,
        "billing_day": billing_day,
        "detected_billing_day": detected_billing_day,
        "days_total": round(days_total, 1),
        "days_elapsed": round(days_elapsed, 1),
        "time_progress_percent": time_progress_percent,
        "settings": settings,
        "total": total_data,
        "codex": codex_data,
        "claude": claude_data,
        "agy": agy_data,
    }


def format_session_snapshot(session: dict[str, Any], provider: str = "Codex") -> str:
    """セッションのサマリー情報を整形し、履歴パネル表示用のテキストブロックを生成します。

    Args:
        session (dict[str, Any]): 整形対象のセッション情報辞書。
        provider (str, optional): プロバイダ名（"Codex", "Claude", "Antigravity" 等）。
            デフォルトは "Codex"。

    Returns:
        str: アスキーアート枠線付きの整形済みスナップショット文字列。
    """
    observed = session.get("observed_at") or "日時不明"
    try:
        dt = datetime.fromisoformat(str(observed).replace("Z", "+00:00"))
        time_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, TypeError):
        time_str = str(observed)

    model = session.get("model") or "モデル不明"
    effort = session.get("effort")
    model_disp = f"{model} ({effort})" if effort else str(model)

    cost_usd = session.get("estimated_cost_usd")
    cost_str = ""
    if isinstance(cost_usd, (int, float)):
        cost_str = f" · ${cost_usd:.4f}"
    elif isinstance(cost_usd, dict):
        min_usd = cost_usd.get("minimum_usd", 0)
        max_usd = cost_usd.get("maximum_usd", 0)
        cost_str = f" · ${min_usd:.4f}–${max_usd:.4f}"

    title_line = f"[{time_str}] {provider} · {model_disp}{cost_str}"

    lines = [title_line]
    lines.append(f"┌ {provider} Session Snapshot " + "─" * 42 + "┐")
    lines.append(f"│ Model      {model_disp:<52}│")

    limits = session.get("limits") or {}
    pri = limits.get("primary")
    sec = limits.get("secondary") or limits.get("rate_limit")
    pri_str = (
        f"5h: {pri['used_percent']}%" if pri and pri.get("used_percent") is not None else "5h: —"
    )
    sec_str = (
        f"7d: {sec['used_percent']}%" if sec and sec.get("used_percent") is not None else "7d: —"
    )
    lim_val = f"{pri_str:<18} |  {sec_str:<28}"
    lines.append(f"│ Limits     {lim_val:<52}│")

    context = session.get("context")
    if context and (context.get("used_tokens") or context.get("window_tokens")):
        used_tok = context.get("used_tokens", 0)
        win_tok = context.get("window_tokens", 0)
        pct = context.get("used_percent")
        pct_str = f"{pct}%" if pct is not None else "—"
        ctx_val = f"{used_tok:,} / {win_tok:,} tokens ({pct_str})"
        lines.append(f"│ Context    {ctx_val:<52}│")

    usage = session.get("usage") or {}
    in_tok = usage.get("input_tokens", 0)
    out_tok = usage.get("output_tokens", 0)
    cached_tok = usage.get("cached_input_tokens") or (
        usage.get("cache_creation_input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
    )
    io_val = f"in {in_tok:,} (cache {cached_tok:,}) out {out_tok:,}"
    lines.append(f"│ Tokens     {io_val:<52}│")

    if cost_usd is not None:
        cost_val = cost_str.replace(" · ", "API≈")
        lines.append(f"│ Cost       {cost_val:<52}│")

    task = session.get("task") or {}
    prompt = task.get("prompt")
    if prompt:
        prompt_short = prompt.replace("\n", " ").strip()[:50]
        lines.append(f"│ Task       {prompt_short:<52}│")

    cwd = task.get("cwd")
    if cwd:
        cwd_short = str(cwd)[:50]
        lines.append(f"│ CWD        {cwd_short:<52}│")

    lines.append("└" + "─" * 65 + "┘")
    return "\n".join(lines)


def collect_snapshot(
    codex_home: Path,
    state_home: Path,
    claude_home: Path | None = None,
    agy_home: Path | None = None,
) -> dict[str, Any]:
    """Codex、Claude Code、Antigravity の全セッション・履歴・利用枠・稼働状況を集約したスナップショットを生成します。

    Args:
        codex_home (Path): Codex のホームディレクトリパス。
        state_home (Path): 状態保存用ベースディレクトリパス。
        claude_home (Path | None, optional): Claude のホームディレクトリパス。デフォルトは None。
        agy_home (Path | None, optional): Antigravity のホームディレクトリパス。デフォルトは None。

    Returns:
        dict[str, Any]: ダッシュボード表示用の完全なスナップショット辞書。
    """
    session_files = sorted(
        (path for path in (codex_home / "sessions").glob("**/*.jsonl") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    sessions = [summary for path in session_files[:8] if (summary := summarize_session(path))]

    # ログファイルの探索（Linuxのcum-watchログ、Vitals自体のログ、Codexホーム直下）
    watch_log_candidates = [
        state_home / "codex-usage-monitor" / "watch.log",
        state_home / "codex-claude-vitals" / "watch.log",
        codex_home / "watch.log",
    ]
    history = []
    for candidate in watch_log_candidates:
        if candidate.is_file():
            try:
                history = [
                    block for block in candidate.read_text(encoding="utf-8").split("\n\n") if block
                ]
                if history:
                    break
            except OSError:
                pass

    active_sessions = [session for session in sessions if not session["auxiliary"]]
    claude_root = claude_home or default_claude_home()
    claude_files = sorted(
        (path for path in (claude_root / "projects").glob("**/*.jsonl") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    claude_sessions = [
        summary for path in claude_files[:8] if (summary := summarize_claude_session(path))
    ]
    for cs in claude_sessions:
        cs["estimated_cost_usd"] = claude_usage_cost(cs["model"], cs["usage"])

    claude = next(
        (summary for summary in claude_sessions if summary["usage"]["total_tokens"]),
        empty_claude_summary(),
    )
    claude["limits"] = load_claude_usage(state_home, claude_home=claude_root)
    claude["estimated_cost_usd"] = claude_usage_cost(claude["model"], claude["usage"])
    # Antigravity sessions
    agy_root = agy_home or default_agy_home()
    agy_brain = agy_root / "brain"
    agy_sessions: list[dict[str, Any]] = []
    if agy_brain.is_dir():
        brain_dirs = sorted(
            (d for d in agy_brain.iterdir() if d.is_dir() and not d.name.startswith(".")),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        agy_sessions = [summary for d in brain_dirs[:8] if (summary := summarize_agy_session(d))]
    agy = next(
        (s for s in agy_sessions if s["usage"]["steps"]),
        empty_agy_summary(),
    )
    agy["limits"] = load_agy_usage(state_home)
    live_status = get_cached_agy_live_status()
    if live_status:
        agy["live_status"] = live_status
        if live_status.get("plan"):
            agy["plan"] = live_status["plan"]
        gem_live = live_status.get("gemini") or {}
        if "primary" in gem_live or "secondary" in gem_live:
            if gem_live.get("primary"):
                agy["limits"]["gemini"]["primary"] = gem_live["primary"]
            if gem_live.get("secondary"):
                agy["limits"]["gemini"]["secondary"] = gem_live["secondary"]
        elif "used_percent" in gem_live:
            agy["limits"]["gemini"]["primary"] = {
                "label": "5h",
                "used_percent": gem_live["used_percent"],
                "window_minutes": 300,
                "resets_at": gem_live.get("resets_at"),
                "is_reset": (
                    gem_live.get("resets_at") is not None and gem_live["resets_at"] <= time.time()
                ),
                "is_live": True,
            }

        cld_live = live_status.get("claude_gpt") or {}
        if "primary" in cld_live or "secondary" in cld_live:
            if cld_live.get("primary"):
                agy["limits"]["claude_gpt"]["primary"] = cld_live["primary"]
            if cld_live.get("secondary"):
                agy["limits"]["claude_gpt"]["secondary"] = cld_live["secondary"]
        elif "used_percent" in cld_live:
            agy["limits"]["claude_gpt"]["primary"] = {
                "label": "5h",
                "used_percent": cld_live["used_percent"],
                "window_minutes": 300,
                "resets_at": cld_live.get("resets_at"),
                "is_reset": (
                    cld_live.get("resets_at") is not None and cld_live["resets_at"] <= time.time()
                ),
                "is_live": True,
            }

        arg_live = live_status.get("argon") or {}
        if "primary" in arg_live or "secondary" in arg_live:
            if arg_live.get("primary"):
                agy["limits"]["argon"]["primary"] = arg_live["primary"]
            if arg_live.get("secondary"):
                agy["limits"]["argon"]["secondary"] = arg_live["secondary"]
        elif "used_percent" in arg_live:
            agy["limits"]["argon"]["secondary"] = {
                "label": "weekly",
                "used_percent": arg_live["used_percent"],
                "window_minutes": 10080,
                "resets_at": arg_live.get("resets_at"),
                "is_reset": (
                    arg_live.get("resets_at") is not None and arg_live["resets_at"] <= time.time()
                ),
                "is_live": True,
            }
    gem_5h = sum(s["usage"].get("gemini", {}).get("steps_5h", 0) for s in agy_sessions)
    gem_7d = sum(s["usage"].get("gemini", {}).get("steps_7d", 0) for s in agy_sessions)
    gem_steps = sum(s["usage"].get("gemini", {}).get("steps", 0) for s in agy_sessions)

    cld_5h = sum(s["usage"].get("claude_gpt", {}).get("steps_5h", 0) for s in agy_sessions)
    cld_7d = sum(s["usage"].get("claude_gpt", {}).get("steps_7d", 0) for s in agy_sessions)
    cld_steps = sum(s["usage"].get("claude_gpt", {}).get("steps", 0) for s in agy_sessions)

    arg_5h = sum(s["usage"].get("argon", {}).get("steps_5h", 0) for s in agy_sessions)
    arg_7d = sum(s["usage"].get("argon", {}).get("steps_7d", 0) for s in agy_sessions)
    arg_steps = sum(s["usage"].get("argon", {}).get("steps", 0) for s in agy_sessions)

    gem_tokens = sum(
        s["usage"]["total_tokens"] for s in agy_sessions if s.get("model_family") == "gemini"
    )
    cld_tokens = sum(
        s["usage"]["total_tokens"] for s in agy_sessions if s.get("model_family") == "claude_gpt"
    )
    arg_tokens = sum(
        s["usage"]["total_tokens"] for s in agy_sessions if s.get("model_family") == "argon"
    )
    agy["usage"]["gemini"] = {
        "steps_5h": gem_5h,
        "steps_7d": gem_7d,
        "steps": gem_steps,
        "total_tokens": gem_tokens,
    }
    agy["usage"]["claude_gpt"] = {
        "steps_5h": cld_5h,
        "steps_7d": cld_7d,
        "steps": cld_steps,
        "total_tokens": cld_tokens,
    }
    agy["usage"]["argon"] = {
        "steps_5h": arg_5h,
        "steps_7d": arg_7d,
        "steps": arg_steps,
        "total_tokens": arg_tokens,
    }
    # watch.log が存在しない場合、セッション履歴から自動生成する
    if not history:
        all_snapshots: list[tuple[str, dict[str, Any], str]] = []
        for s in sessions:
            if s.get("observed_at"):
                all_snapshots.append((str(s["observed_at"]), s, "Codex"))
        for cs in claude_sessions:
            if cs.get("observed_at"):
                all_snapshots.append((str(cs["observed_at"]), cs, "Claude"))
        for ags in agy_sessions:
            if ags.get("observed_at"):
                all_snapshots.append((str(ags["observed_at"]), ags, "Antigravity"))
        all_snapshots.sort(key=lambda item: item[0])
        history = [format_session_snapshot(s, prov) for _, s, prov in all_snapshots[-20:]]
    tasks = [
        {**session, "provider": "Codex"} for session in active_sessions if session["task"]["prompt"]
    ]
    tasks.extend(
        {**session, "provider": "Claude"}
        for session in claude_sessions
        if session["task"]["prompt"]
    )
    tasks.extend(
        {**session, "provider": "Antigravity"}
        for session in agy_sessions
        if session["task"]["prompt"]
    )
    tasks.sort(key=lambda t: str(t.get("observed_at") or ""), reverse=True)
    return {
        "snapshot_at": datetime.now(UTC).isoformat(),
        "current": (active_sessions or sessions or [None])[0],
        "tasks": tasks,
        "history": history,
        "claude": claude,
        "agy": agy,
        "plan_quotas": AGY_PLAN_QUOTAS,
        "status": openai_status(),
        "statuses": {
            "openai": openai_status(),
            "claude": claude_status(),
            "google": google_status(),
        },
        "monthly": collect_monthly_usage(codex_home, state_home, claude_home, agy_home),
    }


def openai_status() -> dict[str, Any]:
    """OpenAI の公式ステータスおよび直近90日間のインシデント履歴を取得します。

    Returns:
        dict[str, Any]: 全体状態および各コンポーネント（ChatGPT, Codex）の稼働状況辞書。
    """
    global STATUS_CACHE
    STATUS_CACHE = cached_status(STATUS_CACHE, STATUS_URL, INCIDENTS_URL, ("ChatGPT", "Codex"))
    return STATUS_CACHE[1]


def claude_status() -> dict[str, Any]:
    """Claude Code の公式ステータスおよび直近90日間のインシデント履歴を取得します。

    Returns:
        dict[str, Any]: 全体状態およびコンポーネントの稼働状況辞書。
    """
    global CLAUDE_STATUS_CACHE
    CLAUDE_STATUS_CACHE = cached_status(
        CLAUDE_STATUS_CACHE, CLAUDE_STATUS_URL, CLAUDE_INCIDENTS_URL, ("Claude Code",)
    )
    return CLAUDE_STATUS_CACHE[1]


def google_status() -> dict[str, Any]:
    """Google Cloud および Gemini の公式ステータスと直近90日間のインシデント履歴を取得します。

    Returns:
        dict[str, Any]: 全体状態および各グループ（Gemini & Vertex AI, Core）の稼働状況辞書。
    """
    global GOOGLE_STATUS_CACHE
    GOOGLE_STATUS_CACHE = cached_google_status(GOOGLE_STATUS_CACHE, GOOGLE_STATUS_URL)
    return GOOGLE_STATUS_CACHE[1]


def cached_google_status(
    cache: tuple[float, dict[str, Any]] | None,
    incidents_url: str,
) -> tuple[float, dict[str, Any]]:
    """短時間ローカルキャッシュを利用して Google Cloud / Gemini の公式ステータスを取得します。

    Args:
        cache (tuple[float, dict[str, Any]] | None): (有効期限タイムスタンプ, キャッシュデータ) のタプル。
        incidents_url (str): Google Cloud インシデント一覧 API の URL。

    Returns:
        tuple[float, dict[str, Any]]: (次回有効期限タイムスタンプ, ステータス辞書) のタプル。
    """
    now = time.monotonic()
    if cache and cache[0] > now:
        return cache
    today = date.today()
    try:
        req = Request(incidents_url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=4) as response:  # nosec B310
            incidents = json.load(response)

        ai_days = ["ok"] * 90
        cloud_days = ["ok"] * 90

        if isinstance(incidents, list):
            for inc in incidents:
                if not isinstance(inc, dict):
                    continue
                dt_str = inc.get("begin") or inc.get("created")
                inc_day = incident_date(dt_str)
                offset = (today - inc_day).days if inc_day else -1

                aff = inc.get("affected_products") or []
                aff_titles = [p.get("title", "") for p in aff if isinstance(p, dict)]
                desc = (str(inc.get("external_desc", "")) + " " + " ".join(aff_titles)).lower()
                sev = "bad" if inc.get("severity") in {"high", "critical"} else "warn"

                is_ai = any(k in desc for k in ("gemini", "vertex", "agent", "dialogflow"))

                if 0 <= offset < 90:
                    if is_ai:
                        ai_days[-offset - 1] = sev
                    cloud_days[-offset - 1] = sev

        ai_normal = round(ai_days.count("ok") * 100 / len(ai_days), 1)
        cloud_normal = round(cloud_days.count("ok") * 100 / len(cloud_days), 1)

        groups = [
            {
                "name": "Gemini & Vertex AI",
                "state": "operational" if ai_days[-1] == "ok" else "degraded",
                "normal_percent": ai_normal,
                "days": ai_days,
                "components": [
                    {"name": "Vertex Gemini API", "state": "operational"},
                    {"name": "Gemini on Agent Platform", "state": "operational"},
                ],
            },
            {
                "name": "Google Cloud Core",
                "state": "operational" if cloud_days[-1] == "ok" else "degraded",
                "normal_percent": cloud_normal,
                "days": cloud_days,
                "components": [
                    {"name": "Compute & Storage", "state": "operational"},
                    {"name": "Network & IAM", "state": "operational"},
                ],
            },
        ]
        value = {
            "state": "All Systems Operational"
            if all(g["state"] == "operational" for g in groups)
            else "Active Incidents",
            "groups": groups,
        }
    except (OSError, ValueError, KeyError):
        value = {"state": "取得不可", "groups": []}
    return now + 60, value


def cached_status(
    cache: tuple[float, dict[str, Any]] | None,
    summary_url: str,
    incidents_url: str,
    names: tuple[str, ...],
) -> tuple[float, dict[str, Any]]:
    """短時間ローカルキャッシュを利用して Statuspage 互換プロバイダの公式ステータスを取得します。

    Args:
        cache (tuple[float, dict[str, Any]] | None): (有効期限タイムスタンプ, キャッシュデータ) のタプル。
        summary_url (str): サマリー API の URL。
        incidents_url (str): インシデント一覧 API の URL。
        names (tuple[str, ...]): 抽出対象とする製品・コンポーネント名のタプル（例: ("ChatGPT", "Codex")）。

    Returns:
        tuple[float, dict[str, Any]]: (次回有効期限タイムスタンプ, ステータス辞書) のタプル。
    """
    now = time.monotonic()
    if cache and cache[0] > now:
        return cache
    try:
        with urlopen(summary_url, timeout=3) as response:  # nosec B310
            summary = json.load(response)
        with urlopen(incidents_url, timeout=3) as response:  # nosec B310
            incidents = json.load(response)
        value = {
            "state": summary["status"]["description"],
            "groups": status_groups(
                summary.get("components", []), incidents.get("incidents", []), date.today(), names
            ),
        }
    except (OSError, ValueError, KeyError):
        value = {"state": "取得不可", "groups": []}
    return now + 60, value


def status_groups(
    components: Any,
    incidents: Any,
    today: date,
    names: tuple[str, ...] = ("ChatGPT", "Codex"),
) -> list[dict[str, Any]]:
    """公開インシデント情報を集計し、対象製品ごとの直近90日間の稼働バー表示用データを生成します。

    Args:
        components (Any): API から取得したコンポーネントリスト。
        incidents (Any): API から取得したインシデントリスト。
        today (date): 集計基準日（本日）。
        names (tuple[str, ...], optional): 集計対象の製品名グループ。デフォルトは ("ChatGPT", "Codex")。

    Returns:
        list[dict[str, Any]]: 各製品グループの健全性パーセント、日別ステータス配列、コンポーネント一覧を含む辞書のリスト。
    """
    component_items = components if isinstance(components, list) else []
    incident_items = incidents if isinstance(incidents, list) else []
    groups = []
    for name in names:
        group_components = [
            {"name": item.get("name", ""), "state": item.get("status", "unknown")}
            for item in component_items
            if isinstance(item, dict) and name.lower() in str(item.get("name", "")).lower()
        ]
        days = ["ok"] * 90
        for incident in incident_items:
            if not isinstance(incident, dict) or name.lower() not in incident_text(incident):
                continue
            incident_day = incident_date(incident.get("created_at"))
            offset = (today - incident_day).days if incident_day else -1
            if 0 <= offset < len(days):
                days[-offset - 1] = incident_severity(incident.get("impact"))
        group_state = next(
            (item["state"] for item in group_components if item["state"] != "operational"),
            "operational",
        )
        groups.append(
            {
                "name": name,
                "state": group_state,
                "normal_percent": round(days.count("ok") * 100 / len(days), 1),
                "days": days,
                "components": group_components,
            }
        )
    return groups


def incident_text(incident: dict[str, Any]) -> str:
    """インシデント情報から対象製品を判別するためのテキスト（タイトルと更新本文）を結合して小文字で返します。

    Args:
        incident (dict[str, Any]): インシデント情報の辞書。

    Returns:
        str: 結合された小文字のテキスト。
    """
    updates = incident.get("incident_updates")
    bodies = [
        item.get("body", "")
        for item in (updates if isinstance(updates, list) else [])
        if isinstance(item, dict)
    ]
    return " ".join([str(incident.get("name", "")), *map(str, bodies)]).lower()


def incident_date(value: Any) -> date | None:
    """ISO-8601 形式のインシデント日時文字列を UTC の date オブジェクトに変換します。

    Args:
        value (Any): 日時文字列（例: "2026-03-01T12:00:00Z"）。

    Returns:
        date | None: 変換された date オブジェクト。無効なデータの場合は None。
    """
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).date()
    except ValueError:
        return None


def incident_severity(impact: Any) -> str:
    """Statuspage の impact 値を稼働バーの色区分（"bad" または "warn"）にマッピングします。

    Args:
        impact (Any): インシデントの影響度（"major", "critical", "minor" 等）。

    Returns:
        str: "bad"（重大障害）または "warn"（軽微な障害）。
    """
    return "bad" if impact in {"major", "critical"} else "warn"


def first_user_prompt(payload: dict[str, Any]) -> str | None:
    """ユーザーメッセージのペイロードから本文を保持せず先頭行のみを抽出します。

    システムプロンプトやラッパー指示行を除外し、安全なタスク概要として先頭行（最大160文字）を返します。

    Args:
        payload (dict[str, Any]): メッセージペイロード辞書。

    Returns:
        str | None: 抽出されたプロンプト先頭行、または None。
    """
    if payload.get("type") != "message" or payload.get("role") != "user":
        return None
    content = payload.get("content")
    texts = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                texts.append(item["text"])
    for text in texts:
        lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
        if lines and injected_context_line(lines[0]):
            continue
        if lines:
            return lines[0][:160]
    return None


def first_claude_user_prompt(record: dict[str, Any]) -> str | None:
    """Claude Code のユーザーレコードから安全な先頭プロンプト行を抽出します。

    Args:
        record (dict[str, Any]): Claude Code の JSONL ユーザーレコード。

    Returns:
        str | None: 抽出されたプロンプト先頭行、または None。
    """
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    payload = {"type": "message", "role": "user", "content": content}
    return first_user_prompt(payload)


def injected_context_line(line: str) -> bool:
    """指定されたテキスト行が既知の指示ラッパー（AGENTS.md やトランスクリプト接頭辞等）かどうかを判定します。

    Args:
        line (str): 判定対象のテキスト行。

    Returns:
        bool: ラッパー行であれば True、通常のユーザー入力行であれば False。
    """
    return line.startswith(
        (
            "<",
            "# AGENTS.md",
            "You are ",
            "The following is the Codex agent history",
            ">>> TRANSCRIPT",
            "```",
        )
    )


def normalize_usage(raw: dict[str, Any]) -> dict[str, int]:
    """Codex のトークン使用量辞書のキーと数値を正規化します。

    Args:
        raw (dict[str, Any]): 生のトークン使用量辞書。

    Returns:
        dict[str, int]: 正規化されたトークン数辞書（input_tokens, cached_input_tokens, output_tokens, total_tokens）。
    """
    return {
        "input_tokens": number(raw.get("input_tokens")),
        "cached_input_tokens": number(raw.get("cached_input_tokens")),
        "output_tokens": number(raw.get("output_tokens")),
        "total_tokens": number(raw.get("total_tokens")),
    }


def normalize_limit(raw: Any) -> dict[str, Any] | None:
    """レート制限ウィンドウの情報を正規化します。

    Args:
        raw (Any): 生の制限情報辞書。

    Returns:
        dict[str, Any] | None: label、used_percent、window_minutes、resets_at、is_reset を含む辞書。
            入力が辞書でない場合は None。
    """
    if not isinstance(raw, dict):
        return None
    minutes = number(raw.get("window_minutes"))
    resets_at = number(raw.get("resets_at")) or None
    return {
        "label": limit_label(minutes),
        "used_percent": rounded(raw.get("used_percent")),
        "window_minutes": minutes,
        "resets_at": resets_at,
        "is_reset": bool(resets_at and resets_at <= time.time()),
    }


def limit_label(minutes: int) -> str:
    """制限ウィンドウの分数から簡潔な表示ラベルを生成します。

    Args:
        minutes (int): ウィンドウの分数（300 -> "5h", 10080 -> "7d" 等）。

    Returns:
        str: 表示ラベル文字列（"5h", "7d", "XXm" 等）。
    """
    if minutes == 300:
        return "5h"
    if minutes == 10080:
        return "7d"
    return f"{minutes}m" if minutes else "limit"


def number(value: Any) -> int:
    """JSON 値を非負の整数（int）に変換します。

    Args:
        value (Any): 変換対象の値。

    Returns:
        int: 0 以上の整数。変換できない場合は 0。
    """
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def rounded(value: Any) -> int:
    """数値を四捨五入して非負の整数（int）に変換します。

    Args:
        value (Any): 変換対象の数値。

    Returns:
        int: 四捨五入された非負の整数。変換できない場合は 0。
    """
    try:
        return max(0, int(float(value) + 0.5))
    except (TypeError, ValueError):
        return 0


def percent(used: int, capacity: int) -> int | None:
    """使用量と容量から四捨五入した使用率パーセントを算出します。

    Args:
        used (int): 使用量。
        capacity (int): 最大容量。

    Returns:
        int | None: 使用率（0〜100等）。容量が 0 の場合は None。
    """
    return rounded(used * 100 / capacity) if capacity else None


def default_codex_home() -> Path:
    """環境変数 CODEX_HOME またはデフォルトの ~/.codex パスを返します。

    Returns:
        Path: Codex ホームディレクトリのパス。
    """
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))


def default_state_home(platform: str | None = None) -> Path:
    """OS ごとの標準的なステート保存先ディレクトリパスを返します。

    Args:
        platform (str | None, optional): 判定対象のプラットフォーム文字列（"win32" 等）。
            None の場合は sys.platform を使用。

    Returns:
        Path: ステートディレクトリのパス（XDG_STATE_HOME, %LOCALAPPDATA%, または ~/.local/state）。
    """
    if "XDG_STATE_HOME" in os.environ:
        return Path(os.environ["XDG_STATE_HOME"])
    current_platform = platform or sys.platform
    if current_platform == "win32" and "LOCALAPPDATA" in os.environ:
        return Path(os.environ["LOCALAPPDATA"])
    return Path.home() / ".local" / "state"


def default_claude_home() -> Path:
    """環境変数 CLAUDE_HOME またはデフォルトの ~/.claude パスを返します。

    Returns:
        Path: Claude ホームディレクトリのパス。
    """
    return Path(os.environ.get("CLAUDE_HOME", Path.home() / ".claude"))


def default_agy_home() -> Path:
    """環境変数 ANTIGRAVITY_HOME またはデフォルトの Antigravity データディレクトリパスを返します。

    Returns:
        Path: Antigravity ホームディレクトリのパス（~/.gemini/antigravity）。
    """
    return Path(os.environ.get("ANTIGRAVITY_HOME", Path.home() / ".gemini" / "antigravity"))


def usage_cost(model: Any, usage: dict[str, int]) -> float | None:
    """Codex のトークン使用量から API 相当の推定費用（USD）を算出します。

    モデル名完全一致・前方一致に加え、バージョン非依存のファミリー名（gpt-6, gpt-5, oss, o3, o1, mini 等）へのフォールバックをサポートします。

    Args:
        model (Any): モデル名（gpt-5 等）。
        usage (dict[str, int]): トークン使用量辞書（input_tokens, cached_input_tokens, output_tokens）。

    Returns:
        float | None: 推定費用（USD）。価格未定義モデルの場合は None。
    """
    model_id = str(model or "").lower().strip()
    if not model_id:
        return None
    # 1. 完全一致またはハイフン区切りの前方一致（長いキーを優先して特定バージョンをマッチ）
    price = next(
        (
            value
            for key, value in sorted(CODEX_PRICES_PER_MILLION.items(), key=lambda x: -len(x[0]))
            if model_id == key or model_id.startswith(key + "-")
        ),
        None,
    )
    # 2. バージョン非依存のファミリー名・キーワードマッチング
    if price is None:
        if "oss" in model_id:
            price = CODEX_PRICES_PER_MILLION.get("gpt-oss", (0.0, 0.0, 0.0))
        elif "mini" in model_id and not any(k in model_id for k in ("o1", "o3", "o4")):
            price = CODEX_PRICES_PER_MILLION.get("mini")
        elif "o4" in model_id:
            price = CODEX_PRICES_PER_MILLION.get("o4-mini")
        elif "o3" in model_id:
            price = CODEX_PRICES_PER_MILLION.get("o3-mini" if "mini" in model_id else "o3")
        elif "o1" in model_id:
            price = CODEX_PRICES_PER_MILLION.get("o1-mini" if "mini" in model_id else "o1")
        elif "gpt-6" in model_id:
            price = CODEX_PRICES_PER_MILLION.get("gpt-6")
        elif "gpt-5" in model_id:
            price = CODEX_PRICES_PER_MILLION.get("gpt-5")
        elif "gpt-4" in model_id:
            price = CODEX_PRICES_PER_MILLION.get("gpt-4o")
        elif "gpt" in model_id:
            price = CODEX_PRICES_PER_MILLION.get("gpt")
    if price is None:
        return None
    input_price, cached_price, output_price = price
    cached = min(usage["input_tokens"], usage["cached_input_tokens"])
    uncached = usage["input_tokens"] - cached
    return (
        uncached * input_price + cached * cached_price + usage["output_tokens"] * output_price
    ) / 1_000_000


def claude_usage_cost(model: Any, usage: dict[str, int]) -> dict[str, float] | None:
    """Claude Code のトークン使用量から API 相当の推定費用範囲（USD）を算出します。

    キャッシュ作成 TTL の詳細が不明なため、最小値と最大値の範囲として返します。
    特定バージョン指定がない場合でも、モデル名称（fable, opus, sonnet, haiku）による自動マッチングを行います。

    Args:
        model (Any): モデル名（claude-sonnet-5 等）。
        usage (dict[str, int]): トークン使用量辞書。

    Returns:
        dict[str, float] | None: 最小・最大推定費用の辞書（"minimum_usd", "maximum_usd"）、
            または未定義時は None。
    """
    model_id = str(model or "").lower().strip()
    if not model_id:
        return None
    # 1. 登録キーによる前方一致（長いキーを優先して特定バージョンをマッチ）
    price = next(
        (
            value
            for key, value in sorted(CLAUDE_PRICES_PER_MILLION.items(), key=lambda x: -len(x[0]))
            if model_id.startswith(key)
        ),
        None,
    )
    # 2. バージョン非依存のファミリー名・モデル名マッチング（例: sonnet, claude-sonnet, claude-sonnet-5-5, claude-opus）
    if price is None:
        fam_match = re.search(
            r"(?:^|[\-_])(sonnet|opus|haiku|fable)(?:$|[\-_](?:\d|latest|preview))",
            model_id,
        )
        if fam_match:
            price = CLAUDE_PRICES_PER_MILLION.get(fam_match.group(1))
        elif model_id in CLAUDE_PRICES_PER_MILLION:
            price = CLAUDE_PRICES_PER_MILLION.get(model_id)
    if price is None:
        return None
    input_price, cache_write_min, cache_write_max, cache_read_price, output_price = price
    base = (
        number(usage.get("input_tokens")) * input_price
        + number(usage.get("cache_read_input_tokens")) * cache_read_price
        + number(usage.get("output_tokens")) * output_price
    ) / 1_000_000
    creation = number(usage.get("cache_creation_input_tokens")) / 1_000_000
    return {
        "minimum_usd": round(base + creation * cache_write_min, 4),
        "maximum_usd": round(base + creation * cache_write_max, 4),
    }


def create_server(
    host: str,
    port: int,
    codex_home: Path,
    state_home: Path,
    claude_home: Path | None = None,
    agy_home: Path | None = None,
) -> ThreadingHTTPServer:
    """ダッシュボードUIおよびスナップショットAPIを提供するループバックHTTPサーバーを生成します。

    Args:
        host (str): バインド先ホストアドレス（ローカル専用のため "127.0.0.1" 必須）。
        port (int): リッスンポート番号。
        codex_home (Path): Codex ホームディレクトリパス。
        state_home (Path): 状態保存用ベースディレクトリパス。
        claude_home (Path | None, optional): Claude ホームディレクトリパス。デフォルトは None。
        agy_home (Path | None, optional): Antigravity ホームディレクトリパス。デフォルトは None。

    Returns:
        ThreadingHTTPServer: 設定済みのマルチスレッドHTTPサーバーインスタンス。

    Raises:
        ValueError: host が "127.0.0.1" 以外の場合。
    """
    if host != "127.0.0.1":
        raise ValueError("AI Vitals only serves on 127.0.0.1")

    shutdown_lock = threading.Lock()
    shutdown_timer: list[threading.Timer | None] = [None]

    def cancel_delayed_shutdown() -> None:
        with shutdown_lock:
            timer = shutdown_timer[0]
            if timer is not None:
                timer.cancel()
                shutdown_timer[0] = None

    class Handler(BaseHTTPRequestHandler):
        """ダッシュボード HTML および API エンドポイントを処理する HTTP リクエストハンドラ。"""

        def do_GET(self) -> None:  # noqa: N802 - required stdlib handler name
            cancel_delayed_shutdown()
            route = urlsplit(self.path).path
            if route == "/api/snapshot":
                self.send_payload(
                    json.dumps(
                        collect_snapshot(codex_home, state_home, claude_home, agy_home)
                    ).encode("utf-8"),
                    "application/json",
                )
            elif route == "/":
                self.send_payload(dashboard_html().encode("utf-8"), "text/html; charset=utf-8")
            else:
                self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802 - required stdlib handler name
            split_url = urlsplit(self.path)
            route = split_url.path
            server_port = getattr(self.server, "server_port", port)
            origin = f"http://{host}:{server_port}"
            if route == "/api/shutdown":
                req_origin = self.headers.get("Origin") or self.headers.get("Referer", "")
                if req_origin and not req_origin.startswith(origin):
                    self.send_error(403)
                else:
                    params = parse_qs(split_url.query)
                    try:
                        delay = max(0.0, float(params.get("delay", ["0"])[0]))
                    except (ValueError, TypeError):
                        delay = 0.0
                    self.send_payload(b'{"stopping":true}', "application/json")
                    with shutdown_lock:
                        timer = shutdown_timer[0]
                        if timer is not None:
                            timer.cancel()
                            shutdown_timer[0] = None
                        if delay > 0:
                            t = threading.Timer(delay, self.server.shutdown)
                            t.daemon = True
                            shutdown_timer[0] = t
                            t.start()
                        else:
                            threading.Thread(target=self.server.shutdown, daemon=True).start()
            elif route == "/api/agy-usage":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    payload = json.loads(body.decode("utf-8"))

                    def _parse_val(used_k: str, rem_k: str) -> int | None:
                        if (
                            used_k in payload
                            and payload[used_k] is not None
                            and str(payload[used_k]).strip() != ""
                        ):
                            return max(0, min(100, int(payload[used_k])))
                        if (
                            rem_k in payload
                            and payload[rem_k] is not None
                            and str(payload[rem_k]).strip() != ""
                        ):
                            return max(0, min(100, 100 - int(payload[rem_k])))
                        return None

                    g5_used = _parse_val("gemini_5h_used", "gemini_5h_remaining")
                    g7_used = _parse_val("gemini_7d_used", "gemini_7d_remaining")
                    c5_used = _parse_val("claude_5h_used", "claude_5h_remaining")
                    c7_used = _parse_val("claude_7d_used", "claude_7d_remaining")
                    a7_used = _parse_val("argon_7d_used", "argon_7d_remaining")

                    legacy_used = (
                        int(payload["used_percent"])
                        if "used_percent" in payload and payload["used_percent"] is not None
                        else None
                    )

                    save_agy_usage(
                        state_home,
                        gemini_5h_used=g5_used,
                        gemini_5h_resets_at=int(payload["gemini_5h_resets_at"])
                        if payload.get("gemini_5h_resets_at")
                        else None,
                        gemini_7d_used=g7_used,
                        gemini_7d_resets_at=int(payload["gemini_7d_resets_at"])
                        if payload.get("gemini_7d_resets_at")
                        else None,
                        claude_5h_used=c5_used,
                        claude_5h_resets_at=int(payload["claude_5h_resets_at"])
                        if payload.get("claude_5h_resets_at")
                        else None,
                        claude_7d_used=c7_used,
                        claude_7d_resets_at=int(payload["claude_7d_resets_at"])
                        if payload.get("claude_7d_resets_at")
                        else None,
                        argon_7d_used=a7_used,
                        argon_7d_resets_at=int(payload["argon_7d_resets_at"])
                        if payload.get("argon_7d_resets_at")
                        else None,
                        used_percent=legacy_used,
                        resets_at=int(payload["resets_at"]) if payload.get("resets_at") else None,
                        window_minutes=int(payload.get("window_minutes") or 10080),
                    )
                    self.send_payload(b'{"ok":true}', "application/json")
                except Exception:
                    self.send_error(400)
            elif route == "/api/monthly-settings":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    payload = json.loads(body.decode("utf-8"))
                    if not isinstance(payload, dict):
                        self.send_error(400)
                    else:
                        save_monthly_settings(state_home, payload)
                        self.send_payload(b'{"ok":true}', "application/json")
                except Exception:
                    self.send_error(400)
            else:
                self.send_error(404)

        def send_payload(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    return ThreadingHTTPServer((host, port), Handler)


def open_dashboard(url: str, browser: Callable[[str], Any] = webbrowser.open) -> None:
    """システム標準または指定されたブラウザでダッシュボード URL を開きます。

    Args:
        url (str): 開く対象のダッシュボード URL。
        browser (Callable[[str], Any], optional): ブラウザ起動用関数。デフォルトは webbrowser.open。
    """
    browser(url)


def detached_process_options(platform: str | None = None) -> dict[str, Any]:
    """Linux および Windows でプロセスをバックグラウンドへ完全にデタッチして起動するための引数辞書を生成します。

    Args:
        platform (str | None, optional): 判定対象のプラットフォーム文字列。None の場合は os.name を使用。

    Returns:
        dict[str, Any]: subprocess.Popen に渡すキーワード引数辞書（stdin/stdout/stderr, creationflags または start_new_session）。
    """
    options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if (platform or os.name) == "nt":
        options["creationflags"] = (
            0x00000008 | 0x00000200
        )  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    return options


def background_command(argv: list[str]) -> list[str]:
    """フォアグラウンド専用オプションを除去し、バックグラウンド子プロセス用の実行コマンドラインを構築します。

    Args:
        argv (list[str]): 元のコマンドライン引数リスト。

    Returns:
        list[str]: バックグラウンド実行用の引数リスト（python 実行バイナリおよびスクリプトパスを含む）。
    """
    child_args = [arg for arg in argv if arg not in {"-b", "--background", "--no-open"}]
    return [sys.executable, str(Path(__file__).resolve()), *child_args, "--no-open"]


def legacy_dashboard_html() -> str:
    """情報密度の高いレガシーなスタンドアロン型ダッシュボード HTML を返します。

    Returns:
        str: レガシーダッシュボード用の HTML/CSS/JS 文字列。
    """
    return """<!doctype html>
<html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Codex Claude Vitals</title>
<style>
:root{color-scheme:dark;--bg:#0b1220;--panel:#121d30;--line:#253651;--text:#e7eefb;--muted:#91a1bb;--mint:#72dfb5;--blue:#7cb7ff;--warn:#ffce6a;--bad:#ff7a7a}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px ui-sans-serif,system-ui,sans-serif}main{max-width:1280px;margin:auto;padding:20px}header{display:flex;justify-content:space-between;gap:12px;align-items:end;margin-bottom:16px}h1{font-size:20px;margin:0}h1 span{color:var(--mint)}.muted{color:var(--muted);font-size:12px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}.card,.panel{border:1px solid var(--line);border-radius:12px;background:var(--panel);padding:14px}.label{color:var(--muted);font-size:12px}.value{font-size:22px;font-weight:700;margin:7px 0}.bar{height:6px;background:#24314a;border-radius:999px;overflow:hidden;margin-top:8px}.bar i{display:block;height:100%;background:var(--mint)}.grid{display:grid;grid-template-columns:1.15fr .85fr;gap:10px;margin-top:10px}.panel h2{font-size:14px;margin:0 0 10px}.task{padding:10px 0;border-top:1px solid var(--line)}.task:first-of-type{border-top:0}.task-head{display:grid;grid-template-columns:70px minmax(0,1fr) 155px;gap:8px;align-items:baseline}.task-head b{min-width:0}.task-index,.task-time{color:var(--muted);font-size:11px;white-space:nowrap}.task-time{text-align:right}.task b{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.task span{display:block;color:var(--muted);margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}details{border-top:1px solid var(--line);padding:8px 0}pre{white-space:pre-wrap;word-break:break-word;color:var(--muted);font-size:11px;margin:8px 0 0}#history{max-height:180px;overflow-y:auto}.status-group{border-top:1px solid var(--line);padding:12px 0 4px}.status-head{display:flex;justify-content:space-between;gap:8px;align-items:baseline}.status-name{font-weight:700}.status-bars{display:flex;gap:2px;margin:8px 0}.status-day{height:13px;flex:1;border-radius:1px;background:var(--mint)}.status-day.warn{background:var(--warn)}.status-day.bad{background:var(--bad)}.status-group details{font-size:12px;color:var(--muted)}@media(max-width:700px){.cards{grid-template-columns:repeat(2,1fr)}.grid{grid-template-columns:1fr}.task-head{grid-template-columns:62px minmax(0,1fr)}.task-time{grid-column:2;text-align:left}}
</style>
<main><header><div><h1><span>CODEX · CLAUDE</span> VITALS</h1><div class="muted">ローカル専用 · API相当の推定値</div></div><div id="updated" class="muted">読み込み中</div></header>
<section class="cards"><article class="card"><div class="label">5h LIMIT</div><div class="value" id="primary">—</div><div class="muted" id="primary-reset">解除: —</div><div class="bar"><i id="primary-bar"></i></div></article><article class="card"><div class="label">7d LIMIT</div><div class="value" id="secondary">—</div><div class="muted" id="secondary-reset">解除: —</div><div class="bar"><i id="secondary-bar"></i></div></article><article class="card"><div class="label">CONTEXT</div><div class="value" id="context">—</div><div class="muted" id="context-detail">—</div><div class="bar"><i id="context-bar"></i></div></article><article class="card"><div class="label">API EQUIVALENT</div><div class="value" id="cost">—</div><div class="muted" id="model">—</div></article></section>
<section class="panel" style="margin-top:10px"><h2>Claude Code</h2><section class="cards"><article class="card"><div class="label">SESSION TOKENS</div><div class="value" id="claude-total">—</div><div class="muted" id="claude-model">—</div></article><article class="card"><div class="label">INPUT / OUTPUT</div><div class="value" id="claude-io">—</div><div class="muted" id="claude-cache">—</div></article><article class="card"><div class="label">5h PLAN LIMIT</div><div class="value" id="claude-primary">—</div><div class="muted" id="claude-primary-reset">/usageを保存してください</div><div class="bar"><i id="claude-primary-bar"></i></div></article><article class="card"><div class="label">7d PLAN LIMIT</div><div class="value" id="claude-secondary">—</div><div class="muted" id="claude-secondary-reset">/usageを保存してください</div><div class="bar"><i id="claude-secondary-bar"></i></div></article></section></section>
<section class="panel" style="margin-top:10px"><h2>OpenAI Status</h2><div id="status" class="muted">取得中</div><div id="status-groups"></div></section>
<section class="grid"><article class="panel"><h2>Active tasks</h2><div id="tasks" class="muted">データなし</div></article><article class="panel"><h2>Recent snapshots</h2><div id="history" class="muted">履歴なし</div></article></section></main>
<script>
const $=id=>document.getElementById(id);const text=(id,value)=>{const el=$(id);if(el)el.textContent=value??'—';};const tokens=value=>value>=1000?(value/1000).toFixed(1)+'k':String(value??0);const resetAt=value=>value?new Date(value*1000).toLocaleString('ja-JP',{timeZone:'Asia/Tokyo'}):'—';const indicatorColor=value=>value>=80?'#ff7a7a':value>=60?'#ffce6a':'#72dfb5';
function paintBar(name,value){const bar=$(name+'-bar');const color=indicatorColor(value??0);bar.style.width=(value??0)+'%';bar.style.background=color;bar.style.boxShadow='0 0 12px '+color}
function limit(name,data){const used=data?.used_percent;text(name,used==null?'—':`${used}%`);text(name+'-reset','解除: '+resetAt(data?.resets_at));paintBar(name,used)}
function renderStatus(status){text('status',status?.state||'取得不可');const groups=$('status-groups');groups.replaceChildren(...(status?.groups||[]).map(group=>{const row=document.createElement('section');row.className='status-group';const head=document.createElement('div');head.className='status-head';const name=document.createElement('span');name.className='status-name';name.textContent=`● ${group.name}`;const meta=document.createElement('span');meta.className='muted';meta.textContent=`正常日割合 ${group.normal_percent}%`;head.append(name,meta);const bars=document.createElement('div');bars.className='status-bars';bars.setAttribute('aria-label',`${group.name} の90日インシデント履歴`);(group.days||[]).forEach((state,index)=>{const day=document.createElement('i');day.className='status-day '+state;day.title=`${89-index}日前: ${state==='ok'?'問題なし':state==='warn'?'軽微な障害':'重大な障害'}`;bars.append(day)});const details=document.createElement('details');const summary=document.createElement('summary');summary.textContent=`構成要素 ${group.components?.length||0} 件（${group.state}）`;details.append(summary,...(group.components||[]).map(component=>{const item=document.createElement('div');item.textContent=`${component.name}: ${component.state}`;return item}));row.append(head,bars,details);return row}));if(!groups.childNodes.length)groups.textContent='グループ情報を取得できませんでした'}
function render(data){const current=data.current||{};limit('primary',current.limits?.primary);limit('secondary',current.limits?.secondary);const context=current.context||{};const used=context.used_percent;text('context',used==null?'—':used+'%');text('context-detail',`${tokens(context.used_tokens)} / ${tokens(context.window_tokens)} tokens`);paintBar('context',used);text('cost',current.estimated_cost_usd==null?'見積不可':'$'+current.estimated_cost_usd.toFixed(4));text('model',[current.model,current.effort].filter(Boolean).join(' · '));const claude=data.claude||{};const claudeUsage=claude.usage||{};text('claude-total',tokens(claudeUsage.total_tokens));text('claude-model',[claude.model,claude.observed_at&&new Date(claude.observed_at).toLocaleString('ja-JP')].filter(Boolean).join(' · ')||'セッション待機中');text('claude-io',`${tokens(claudeUsage.input_tokens)} / ${tokens(claudeUsage.output_tokens)}`);text('claude-cache',`cache ${tokens((claudeUsage.cache_creation_input_tokens||0)+(claudeUsage.cache_read_input_tokens||0))}`);limit('claude-primary',claude.limits?.primary);limit('claude-secondary',claude.limits?.secondary);renderStatus(data.status);text('updated',current.observed_at?'更新 '+new Date(current.observed_at).toLocaleString('ja-JP'):'セッション待機中');const tasks=$('tasks');tasks.replaceChildren(...(data.tasks||[]).map((task,index)=>{const row=document.createElement('div');row.className='task';const head=document.createElement('div');head.className='task-head';const order=document.createElement('span');order.className='task-index';order.textContent=`#${index+1} · ${index}件前`;const title=document.createElement('b');title.textContent=task.task?.prompt||'プロンプト未取得';const observed=document.createElement('time');observed.className='task-time';observed.textContent=task.observed_at?new Date(task.observed_at).toLocaleString('ja-JP',{timeZone:'Asia/Tokyo'}):'日時不明';head.append(order,title,observed);const meta=document.createElement('span');meta.textContent=[task.task?.cwd,task.model].filter(Boolean).join(' · ');row.append(head,meta);return row}));if(!tasks.childNodes.length)tasks.textContent='データなし';const history=$('history');history.replaceChildren(...(data.history||[]).reverse().map(item=>{const detail=document.createElement('details');const title=document.createElement('summary');title.textContent=item.split('\\n')[0]||'snapshot';const pre=document.createElement('pre');pre.textContent=item;detail.append(title,pre);return detail}));if(!history.childNodes.length)history.textContent='履歴なし'}
async function loadSnapshot(){try{render(await (await fetch('/api/snapshot',{cache:'no-store'})).json())}catch(error){text('updated','取得エラー: '+error.message)}}loadSnapshot();setInterval(loadSnapshot,15000);
</script></html>"""


def dashboard_html() -> str:
    """テーマ切り替え・ドラッグ並び替え対応のカスタマイズ可能なダッシュボード HTML を返します。

    Returns:
        str: ダッシュボード用の完全な HTML/CSS/JS 文字列。
    """
    return """<!doctype html>
<html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Vitals</title>
<style>
:root{color-scheme:dark;--bg:#0b1220;--panel:#121d30;--card:#101a2b;--badge:#16243a;--bar-bg:#24314a;--line:#253651;--text:#e7eefb;--muted:#91a1bb;--mint:#72dfb5;--blue:#7cb7ff;--warn:#ffce6a;--bad:#ff7a7a}
html[data-theme="dracula"]{color-scheme:dark;--bg:#1e1f29;--panel:#282a36;--card:#21222c;--badge:#343746;--bar-bg:#44475a;--line:#44475a;--text:#f8f8f2;--muted:#8b9bb4;--mint:#50fa7b;--blue:#bd93f9;--warn:#ffb86c;--bad:#ff5555}
html[data-theme="cyberpunk"]{color-scheme:dark;--bg:#08090f;--panel:#121324;--card:#181a2e;--badge:#22253f;--bar-bg:#2d3154;--line:#ff00554d;--text:#00ffcc;--muted:#8b9ab5;--mint:#00ffcc;--blue:#ff007f;--warn:#ffe600;--bad:#ff0055}
html[data-theme="synthwave"]{color-scheme:dark;--bg:#1f1629;--panel:#261f38;--card:#2c2242;--badge:#3a2e56;--bar-bg:#47366b;--line:#5a3f85;--text:#fdfdfd;--muted:#9a8fc0;--mint:#72f1b8;--blue:#fe4450;--warn:#fede5d;--bad:#ff7edb}
html[data-theme="monokai"]{color-scheme:dark;--bg:#1e1c1f;--panel:#272428;--card:#322e33;--badge:#403b41;--bar-bg:#4d474e;--line:#4e474f;--text:#fcfcfa;--muted:#938f94;--mint:#a9dc76;--blue:#78dce8;--warn:#ffd866;--bad:#ff6188}
html[data-theme="nord"]{color-scheme:dark;--bg:#242933;--panel:#2e3440;--card:#3b4252;--badge:#434c5e;--bar-bg:#4c566a;--line:#4c566a;--text:#eceff4;--muted:#94a1b8;--mint:#88c0d0;--blue:#81a1c1;--warn:#ebcb8b;--bad:#bf616a}
html[data-theme="github-light"]{color-scheme:light;--bg:#ffffff;--panel:#f6f8fa;--card:#ffffff;--badge:#eaeef2;--bar-bg:#d0d7de;--line:#d0d7de;--text:#1f2328;--muted:#656d76;--mint:#1a7f37;--blue:#0969da;--warn:#9a6700;--bad:#cf222e}
html[data-theme="solarized-light"]{color-scheme:light;--bg:#fdf6e3;--panel:#f5efdc;--card:#fdf6e3;--badge:#eee8d5;--bar-bg:#e0d9c4;--line:#d3d0c8;--text:#073642;--muted:#586e75;--mint:#859900;--blue:#268bd2;--warn:#b58900;--bad:#dc322f}
html[data-theme="one-light"]{color-scheme:light;--bg:#fafafa;--panel:#f0f0f0;--card:#ffffff;--badge:#e5e5e6;--bar-bg:#e0e0e0;--line:#d4d4d4;--text:#383a42;--muted:#6f7179;--mint:#50a14f;--blue:#4078f2;--warn:#c18401;--bad:#e45649}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}main{max-width:1280px;margin:auto;padding:0 20px 32px}header{position:sticky;top:0;z-index:50;display:flex;justify-content:space-between;gap:12px;align-items:center;padding:14px 0;margin-bottom:18px;background:color-mix(in srgb,var(--bg) 82%,transparent);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border-bottom:1px solid color-mix(in srgb,var(--line) 45%,transparent)}h1{font-size:18px;font-weight:700;letter-spacing:-0.02em;margin:0;display:flex;align-items:center;gap:4px}h1 span{color:var(--mint);background:linear-gradient(135deg,var(--mint),var(--blue));-webkit-background-clip:text;-webkit-text-fill-color:transparent}button,summary,select{cursor:pointer;font-family:inherit}.muted{color:var(--muted);font-size:12px}summary::-webkit-details-marker{display:none}details>summary{list-style:none}.btn-small,.layout-button,#shutdown{display:inline-flex;align-items:center;justify-content:center;gap:5px;height:30px;padding:0 12px;font-size:11px;font-weight:600;color:var(--text);background:color-mix(in srgb,var(--badge) 80%,transparent);border:1px solid color-mix(in srgb,var(--line) 85%,transparent);border-radius:7px;cursor:pointer;white-space:nowrap;transition:all .18s cubic-bezier(0.16,1,0.3,1);text-decoration:none;box-sizing:border-box;margin:0;backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px)}.btn-small:hover,.layout-button:hover{background:var(--panel);border-color:color-mix(in srgb,var(--mint) 60%,var(--line));color:var(--mint);transform:translateY(-1px);box-shadow:0 2px 8px #0002}#shutdown{color:#ffd7d7;background:#351923cc;border-color:var(--bad)}#shutdown:hover{background:#572332;border-color:var(--bad);color:#fff;box-shadow:0 2px 10px rgba(255,122,122,0.25);transform:translateY(-1px)}#shutdown:focus-visible{outline:2px solid #fff;outline-offset:2px}#shutdown:disabled{cursor:default;opacity:.55;transform:none}#notify-toggle.active{border-color:var(--mint);color:var(--mint);background:color-mix(in srgb,var(--mint) 12%,var(--badge))}#theme-select{display:inline-flex;align-items:center;height:28px;padding:0 8px;font-size:11px;font-weight:600;background:var(--badge);border:1px solid var(--line);color:var(--text);border-radius:6px;cursor:pointer;transition:all .15s ease;margin:0}#theme-select:hover{border-color:var(--mint)}#updated{margin:0 0 0 4px;font-variant-numeric:tabular-nums}.settings{position:absolute;right:0;z-index:100;min-width:256px;max-height:85vh;overflow-y:auto;background:color-mix(in srgb,var(--panel) 88%,transparent);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);border:1px solid color-mix(in srgb,var(--line) 90%,rgba(255,255,255,0.08));border-radius:10px;padding:14px;box-shadow:0 16px 36px rgba(0,0,0,0.35),0 0 0 1px rgba(255,255,255,0.04);animation:popoverIn .15s ease-out}@keyframes popoverIn{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:translateY(0)}}.settings label{display:block;padding:4px 0;font-size:12px}.settings select{width:100%;height:30px;padding:0 8px;font-size:11px;font-weight:500;background:var(--badge);border:1px solid var(--line);color:var(--text);border-radius:6px;cursor:pointer;margin-top:3px;transition:border-color .15s}.settings select:hover{border-color:var(--mint)}.settings select:focus{outline:none;border-color:var(--mint);box-shadow:0 0 0 2px color-mix(in srgb,var(--mint) 25%,transparent)}.settings::-webkit-scrollbar{width:5px}.settings::-webkit-scrollbar-thumb{background:var(--line);border-radius:3px}.settings button{margin-top:8px}.layout-item{display:flex;align-items:center;justify-content:space-between;gap:6px;padding:3px 0;border-radius:4px}.layout-item label{display:flex;align-items:center;gap:6px;margin:0;padding:0;cursor:pointer;font-size:12px;user-select:none;flex:1}.item-btns{display:inline-flex;gap:2px}.btn-order{background:var(--badge);border:1px solid var(--line);color:var(--text);border-radius:4px;padding:1px 6px;font-size:11px;font-weight:700;cursor:pointer;line-height:1.2;transition:all .1s}.btn-order:hover:not(:disabled){background:var(--panel);border-color:var(--mint);color:var(--mint)}.btn-order:disabled{opacity:.2;cursor:default}.blocks{display:grid;gap:12px}.block{border:1px solid var(--line);border-radius:14px;background:var(--panel);padding:16px;box-shadow:0 1px 3px rgba(0,0,0,0.08),0 4px 12px rgba(0,0,0,0.03);transition:border-color .15s ease}.block:hover{border-color:color-mix(in srgb,var(--line) 75%,var(--mint))}.block[hidden]{display:none}.block.dragging{opacity:.4;transform:scale(0.995)}.block-title{display:flex;align-items:center;gap:8px;margin:0;font-size:14px;font-weight:600;letter-spacing:-0.01em}.handle{color:var(--muted);cursor:grab;user-select:none;transition:color .15s}.handle:hover{color:var(--mint)}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}.card{border:1px solid var(--line);border-radius:10px;padding:13px;background:var(--card);transition:transform .18s cubic-bezier(0.16,1,0.3,1),box-shadow .18s cubic-bezier(0.16,1,0.3,1),border-color .18s ease}.card:hover{transform:translateY(-1px);border-color:color-mix(in srgb,var(--mint) 35%,var(--line));box-shadow:0 6px 18px rgba(0,0,0,0.12)}.label{color:var(--muted);font-size:11px;font-weight:600;letter-spacing:0.04em;text-transform:uppercase}.value{font-size:24px;font-weight:700;margin:6px 0;letter-spacing:-0.03em;font-feature-settings:"tnum";font-variant-numeric:tabular-nums}.bar{height:5px;background:var(--bar-bg);border-radius:999px;overflow:hidden;margin-top:8px}.bar i{display:block;height:100%;background:var(--mint);transition:width .4s cubic-bezier(0.16,1,0.3,1),background-color .2s ease}.plan-badge{display:inline-flex;align-items:center;gap:6px;cursor:pointer;padding:3px 9px;border-radius:6px;background:var(--badge);border:1px solid var(--line);font-size:12px;color:var(--text);transition:all .15s;font-variant-numeric:tabular-nums}.plan-badge:hover{background:var(--panel);border-color:var(--mint);transform:translateY(-0.5px)}.plan-badge i{font-style:normal;font-size:11px;color:var(--mint);text-decoration:none;font-weight:600}.service-badge{display:inline-flex;align-items:center;gap:6px;font-size:11px;color:var(--muted);background:var(--badge);border:1px solid var(--line);border-radius:6px;padding:3px 9px;font-variant-numeric:tabular-nums}.task{padding:10px 0;border-top:1px solid var(--line)}.task:first-of-type{border-top:0}.task-head{display:grid;grid-template-columns:70px minmax(0,1fr) 155px;gap:8px;align-items:baseline}.task-head b{min-width:0}.task-index,.task-time{color:var(--muted);font-size:11px;white-space:nowrap;font-variant-numeric:tabular-nums}.task-time{text-align:right}.task b{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.task span{display:block;color:var(--muted);margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.provider{display:inline-block;color:#fff!important;background:var(--mint);border-radius:999px;padding:1px 6px;font-size:10px;font-weight:600}.provider.claude{background:var(--warn);color:#1f2328!important}.provider.agy{background:var(--blue);color:#fff!important}.status-details summary{cursor:pointer;list-style:none}.status-details summary::-webkit-details-marker{display:none}details{border-top:1px solid var(--line);padding:8px 0}pre{white-space:pre-wrap;word-break:break-word;color:var(--muted);font-size:11px;margin:8px 0 0}#tasks{max-height:480px;overflow-y:auto;padding-right:4px}#tasks::-webkit-scrollbar{width:6px}#tasks::-webkit-scrollbar-track{background:transparent}#tasks::-webkit-scrollbar-thumb{background:var(--line);border-radius:3px}#tasks::-webkit-scrollbar-thumb:hover{background:var(--muted)}#history{max-height:180px;overflow-y:auto}.status-groups{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px;margin-top:10px;border-top:1px solid var(--line);padding-top:8px}.status-group{padding:4px 0}.status-head{display:flex;justify-content:space-between;gap:8px;align-items:baseline}.status-name{font-weight:700}.status-bars{display:flex;gap:2px;margin:8px 0}.status-day{height:13px;flex:1;border-radius:1px;background:var(--mint);cursor:pointer;transition:transform .12s cubic-bezier(0.16,1,0.3,1)}.status-day:hover{transform:scaleY(1.35);opacity:.9}.status-day.warn{background:var(--warn)}.status-day.bad{background:var(--bad)}.status-group details{font-size:12px;color:var(--muted);border:0;padding:2px 0}.burn-badge{display:inline-block;font-size:10px;padding:1px 6px;border-radius:4px;font-weight:700;margin-left:5px;vertical-align:middle;font-variant-numeric:tabular-nums}.burn-badge.warn{background:#ff555522;color:var(--bad);border:1px solid var(--bad)}.burn-badge.safe{background:#50fa7b22;color:var(--mint);border:1px solid var(--mint)}.burn-badge.normal{background:#bd93f922;color:var(--blue);border:1px solid var(--blue)}.bloat-alert{display:inline-block;margin-top:6px;font-size:11px;color:var(--warn);background:#ffe60022;border:1px solid var(--warn);border-radius:4px;padding:2px 6px;line-height:1.3}dialog::backdrop{background:rgba(0,0,0,0.55);backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px)}@media(max-width:700px){main{padding:0 12px 24px}.cards{grid-template-columns:repeat(2,1fr)}.task-head{grid-template-columns:62px minmax(0,1fr)}.task-time{grid-column:2;text-align:left}}
</style>
<main><header><div><h1><span>AI</span> VITALS</h1></div><div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap"><div id="updated" class="muted">読み込み中</div><button id="shutdown" type="button">⏹ 停止</button><details id="layout-settings"><summary class="layout-button" title="設定・メニューを開きます">☰ メニュー</summary><div class="settings"><div style="font-weight:700;font-size:11px;color:var(--mint);margin-bottom:6px">セクション表示・並び順</div><div id="layout-item-list"><div class="layout-item" data-item="overview"><label><input type="checkbox" data-toggle="overview"> Today's Overview</label><div class="item-btns"><button type="button" class="btn-order" data-dir="up" title="上へ移動">↑</button><button type="button" class="btn-order" data-dir="down" title="下へ移動">↓</button></div></div><div class="layout-item" data-item="codex"><label><input type="checkbox" data-toggle="codex"> Codex</label><div class="item-btns"><button type="button" class="btn-order" data-dir="up" title="上へ移動">↑</button><button type="button" class="btn-order" data-dir="down" title="下へ移動">↓</button></div></div><div class="layout-item" data-item="claude"><label><input type="checkbox" data-toggle="claude"> Claude</label><div class="item-btns"><button type="button" class="btn-order" data-dir="up" title="上へ移動">↑</button><button type="button" class="btn-order" data-dir="down" title="下へ移動">↓</button></div></div><div class="layout-item" data-item="agy"><label><input type="checkbox" data-toggle="agy"> Antigravity</label><div class="item-btns"><button type="button" class="btn-order" data-dir="up" title="上へ移動">↑</button><button type="button" class="btn-order" data-dir="down" title="下へ移動">↓</button></div></div><div class="layout-item" data-item="tasks"><label><input type="checkbox" data-toggle="tasks"> Active tasks</label><div class="item-btns"><button type="button" class="btn-order" data-dir="up" title="上へ移動">↑</button><button type="button" class="btn-order" data-dir="down" title="下へ移動">↓</button></div></div><div class="layout-item" data-item="history"><label><input type="checkbox" data-toggle="history"> History</label><div class="item-btns"><button type="button" class="btn-order" data-dir="up" title="上へ移動">↑</button><button type="button" class="btn-order" data-dir="down" title="下へ移動">↓</button></div></div></div><button id="layout-reset" type="button" style="width:100%;margin-top:6px">初期配置に戻す</button><div style="border-top:1px solid var(--line);margin:10px 0 6px"></div><div style="font-weight:700;font-size:11px;color:var(--mint);margin-bottom:6px">環境設定</div><label>カラーテーマ<select id="theme-select"><option value="default">🎨 Default Dark</option><option value="dracula">🧛 Dracula</option><option value="cyberpunk">⚡ Cyberpunk 2077</option><option value="synthwave">🌆 Synthwave '84</option><option value="monokai">🐍 Monokai Pro</option><option value="nord">❄️ Nord</option><option value="github-light">☀️ GitHub Light (ライト)</option><option value="solarized-light">🌅 Solarized Light (ライト)</option><option value="one-light">💡 One Light (ライト)</option></select></label><div style="margin-top:6px"><button id="notify-toggle" type="button" class="btn-small" style="width:100%" title="クォータ上限到達やリセット解除をデスクトップ通知します">🔔 通知: オフ</button></div><div style="border-top:1px solid var(--line);margin:10px 0 6px"></div><div style="font-weight:700;font-size:11px;color:var(--mint);margin-bottom:6px">契約プラン設定（表示用）</div><label>Codex plan（表示用）<select id="codex-plan-select"><option>未設定</option><option>ChatGPT Free</option><option>ChatGPT Go</option><option>ChatGPT Plus</option><option>ChatGPT Pro 5x</option><option>ChatGPT Pro 20x</option><option>ChatGPT Business</option><option>ChatGPT Enterprise / Edu</option><option>API 従量課金</option></select></label><label>Claude plan（表示用）<select id="claude-plan-select"><option>未設定</option><option>Claude Free</option><option>Claude Pro</option><option>Claude Max 5x</option><option>Claude Max 20x</option><option>Claude Team</option><option>Claude Enterprise</option><option>API 従量課金</option></select></label><label>Antigravity plan（表示用）<select id="agy-plan-select"><option>未設定</option><option>Google AI Free</option><option>Google AI Plus</option><option>Google AI Pro</option><option>Google AI Ultra 5x</option><option>Google AI Ultra 20x</option><option>API 従量課金</option></select></label><div style="border-top:1px solid var(--line);margin:10px 0 6px"></div><div style="font-weight:700;font-size:11px;color:var(--mint);margin-bottom:6px">月間予算・上限設定</div><label>集計締め日（1〜31日）<input type="number" id="monthly-billing-day" min="1" max="31" value="1" style="width:100%;height:28px;padding:0 8px;font-size:11px;background:var(--badge);border:1px solid var(--line);color:var(--text);border-radius:6px;margin-top:2px"></label><div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:6px"><div><label style="font-size:10px;font-weight:600;color:var(--muted)">Codex 予算 ($)<input type="number" id="monthly-codex-budget" min="0" step="0.5" placeholder="0.00" style="width:100%;height:26px;padding:0 6px;font-size:11px;background:var(--badge);border:1px solid var(--line);color:var(--text);border-radius:4px;margin-top:2px"></label></div><div><label style="font-size:10px;font-weight:600;color:var(--muted)">Codex トークン枠<input type="number" id="monthly-codex-tokens" min="0" step="100000" placeholder="0" style="width:100%;height:26px;padding:0 6px;font-size:11px;background:var(--badge);border:1px solid var(--line);color:var(--text);border-radius:4px;margin-top:2px"></label></div><div><label style="font-size:10px;font-weight:600;color:var(--muted)">Claude 予算 ($)<input type="number" id="monthly-claude-budget" min="0" step="0.5" placeholder="0.00" style="width:100%;height:26px;padding:0 6px;font-size:11px;background:var(--badge);border:1px solid var(--line);color:var(--text);border-radius:4px;margin-top:2px"></label></div><div><label style="font-size:10px;font-weight:600;color:var(--muted)">Claude トークン枠<input type="number" id="monthly-claude-tokens" min="0" step="100000" placeholder="0" style="width:100%;height:26px;padding:0 6px;font-size:11px;background:var(--badge);border:1px solid var(--line);color:var(--text);border-radius:4px;margin-top:2px"></label></div><div><label style="font-size:10px;font-weight:600;color:var(--muted)">Antigravity 予算 ($)<input type="number" id="monthly-agy-budget" min="0" step="0.5" placeholder="0.00" style="width:100%;height:26px;padding:0 6px;font-size:11px;background:var(--badge);border:1px solid var(--line);color:var(--text);border-radius:4px;margin-top:2px"></label></div><div><label style="font-size:10px;font-weight:600;color:var(--muted)">Antigravity トークン枠<input type="number" id="monthly-agy-tokens" min="0" step="100000" placeholder="0" style="width:100%;height:26px;padding:0 6px;font-size:11px;background:var(--badge);border:1px solid var(--line);color:var(--text);border-radius:4px;margin-top:2px"></label></div><div><label style="font-size:10px;font-weight:600;color:var(--muted)">全体予算 ($: 空白で合算)<input type="number" id="monthly-total-budget" min="0" step="0.5" placeholder="自動合算" style="width:100%;height:26px;padding:0 6px;font-size:11px;background:var(--badge);border:1px solid var(--line);color:var(--text);border-radius:4px;margin-top:2px"></label></div><div><label style="font-size:10px;font-weight:600;color:var(--muted)">全体トークン枠 (空白で合算)<input type="number" id="monthly-total-tokens" min="0" step="100000" placeholder="自動合算" style="width:100%;height:26px;padding:0 6px;font-size:11px;background:var(--badge);border:1px solid var(--line);color:var(--text);border-radius:4px;margin-top:2px"></label></div></div><button id="save-monthly-btn" type="button" class="btn-small" style="width:100%;margin-top:8px;background:var(--mint);color:var(--bg);font-weight:700">💾 月間予算・上限を保存</button></div></details></div></header>
<div id="layout" class="blocks">
<section class="block" data-block="overview" draggable="true"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;flex-wrap:wrap;gap:8px"><h2 class="block-title"><span class="handle">⠿</span>Today's Overview<span class="muted" style="font-size:11px;font-weight:400;margin-left:6px">本日全体の利用サマリー</span></h2><div style="display:flex;align-items:center;gap:6px"><span id="monthly-period-badge" class="service-badge">当月集計: —</span><div id="overview-health-badge" class="service-badge">● システム健全性: 取得中</div></div></div><section class="cards"><article class="card"><div class="label">TODAY'S TASKS</div><div class="value" id="ov-tasks">—</div><div class="muted" id="ov-tasks-detail">本日実行されたプロンプト数</div></article><article class="card"><div class="label">ESTIMATED SPEND (TODAY)</div><div class="value" id="ov-cost">—</div><div class="muted" id="ov-cost-detail">3ツール合算 (API相当額)</div></article><article class="card"><div class="label">TOTAL TOKENS (OBSERVED)</div><div class="value" id="ov-tokens">—</div><div class="muted" id="ov-tokens-detail">観測セッションのトークン合算</div></article><article class="card"><div class="label">MOST ACTIVE TOOL</div><div class="value" id="ov-active-tool">—</div><div class="muted" id="ov-active-detail">本日最も使われたAIツール</div></article></section><div style="border-top:1px solid var(--line);margin:12px 0 10px"></div><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px"><div style="font-size:11px;font-weight:700;color:var(--muted);letter-spacing:0.04em;text-transform:uppercase">Monthly Vitals &amp; Quotas (当月累計・予算進捗)</div><span id="ov-monthly-time-progress" class="muted" style="font-size:11px">期間経過: —</span></div><section class="cards"><article class="card"><div class="label">MONTHLY TASKS</div><div class="value" id="ov-monthly-tasks">—</div><div class="muted" id="ov-monthly-tasks-detail">当月総タスク数</div></article><article class="card"><div class="label" style="display:flex;justify-content:space-between"><span>ESTIMATED SPEND (MONTH)</span><span id="ov-monthly-burn"></span></div><div class="value" id="ov-monthly-cost">—</div><div class="muted" id="ov-monthly-cost-detail">予算: — · 残り: —</div><div class="bar" style="margin-top:6px;height:5px"><i id="ov-monthly-cost-bar"></i></div></article><article class="card"><div class="label" style="display:flex;justify-content:space-between"><span>MONTHLY TOKENS</span><span id="ov-monthly-tokens-burn"></span></div><div class="value" id="ov-monthly-tokens">—</div><div class="muted" id="ov-monthly-tokens-detail">枠: — · 残り: —</div><div class="bar" style="margin-top:6px;height:5px"><i id="ov-monthly-tokens-bar"></i></div></article><article class="card"><div class="label">MONTHLY PACE</div><div class="value" id="ov-monthly-pace">—</div><div class="muted" id="ov-monthly-pace-detail">当月消化ペース</div></article></section></section>
<section class="block" data-block="codex" draggable="true"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;flex-wrap:wrap;gap:8px"><h2 class="block-title"><span class="handle">⠿</span>Codex<span id="codex-sync-badge" style="font-size:11px;font-weight:400;color:var(--mint);margin-left:6px"></span></h2><div id="openai-status-badge" class="service-badge">● OpenAI Status: 取得中</div></div><section class="cards"><article class="card"><div class="label">5h LIMIT</div><div class="value" id="primary">—</div><div class="muted" id="primary-reset">解除: —</div><div class="muted" id="primary-detail">使用: — · 残り: —</div><div class="bar"><i id="primary-bar"></i></div></article><article class="card"><div class="label">7d LIMIT<span id="secondary-burn"></span></div><div class="value" id="secondary">—</div><div class="muted" id="secondary-reset">解除: —</div><div class="muted" id="secondary-detail">使用: — · 残り: —</div><div class="bar"><i id="secondary-bar"></i></div></article><article class="card"><div class="label" style="display:flex;justify-content:space-between"><span>1m MONTHLY<span id="codex-1m-burn"></span></span><span class="muted" id="codex-1m-val">—</span></div><div style="margin-top:7px"><div style="display:flex;justify-content:space-between;font-size:12px"><b id="codex-1m-cost">—</b><span class="muted" id="codex-1m-rem">残り: —</span></div><div class="bar" style="margin-top:3px;height:6px"><i id="codex-1m-bar"></i></div></div><div class="muted" id="codex-1m-reset" style="margin-top:6px;font-size:11px">リセット: —</div><div class="muted" id="codex-1m-detail" style="font-size:11px">トークン: —</div></article><article class="card"><div class="label">TOKENS &amp; COST</div><div class="value" id="cost">—</div><div class="muted" id="context-detail">CONTEXT: —</div><div class="muted" id="context-remaining">残り: —</div><div class="bar"><i id="context-bar"></i></div><div id="context-bloat"></div><div class="muted" id="cost-note" style="margin-top:4px">—</div></article></section><div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-top:8px;flex-wrap:wrap"><div style="display:flex;align-items:center;gap:8px"><div class="plan-badge" id="codex-plan-view" title="クリックして契約プランを選択">PLAN: 未設定 <i>⚙️ 変更</i></div><div class="service-badge" id="codex-monthly-badge">当月: —</div><div class="muted" id="codex-model">—</div></div></div><div id="openai-status-groups" class="status-groups"></div></section>
<section class="block" data-block="claude" draggable="true"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;flex-wrap:wrap;gap:8px"><h2 class="block-title"><span class="handle">⠿</span>Claude Code<span id="claude-sync-badge" style="font-size:11px;font-weight:400;color:var(--mint);margin-left:6px"></span></h2><div id="claude-status-badge" class="service-badge">● Claude Status: 取得中</div></div><section class="cards"><article class="card"><div class="label">5h PLAN LIMIT</div><div class="value" id="claude-primary">—</div><div class="muted" id="claude-primary-reset">解除: —</div><div class="muted" id="claude-primary-detail">使用: — · 残り: —</div><div class="bar"><i id="claude-primary-bar"></i></div></article><article class="card"><div class="label">7d PLAN LIMIT<span id="claude-secondary-burn"></span></div><div class="value" id="claude-secondary">—</div><div class="muted" id="claude-secondary-reset">解除: —</div><div class="muted" id="claude-secondary-detail">使用: — · 残り: —</div><div class="bar"><i id="claude-secondary-bar"></i></div></article><article class="card"><div class="label" style="display:flex;justify-content:space-between"><span>1m MONTHLY<span id="claude-1m-burn"></span></span><span class="muted" id="claude-1m-val">—</span></div><div style="margin-top:7px"><div style="display:flex;justify-content:space-between;font-size:12px"><b id="claude-1m-cost">—</b><span class="muted" id="claude-1m-rem">残り: —</span></div><div class="bar" style="margin-top:3px;height:6px"><i id="claude-1m-bar"></i></div></div><div class="muted" id="claude-1m-reset" style="margin-top:6px;font-size:11px">リセット: —</div><div class="muted" id="claude-1m-detail" style="font-size:11px">トークン: —</div></article><article class="card"><div class="label">TOKENS &amp; COST</div><div class="value" id="claude-cost">—</div><div class="muted" id="claude-total">SESSION: —</div><div class="muted" id="claude-model">MODEL: —</div><div id="claude-bloat"></div><div class="muted" id="claude-cost-note" style="margin-top:4px">—</div></article></section><div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-top:8px;flex-wrap:wrap"><div style="display:flex;align-items:center;gap:8px"><div class="plan-badge" id="claude-plan-view" title="クリックして契約プランを選択">PLAN: 未設定 <i>⚙️ 変更</i></div><div class="service-badge" id="claude-monthly-badge">当月: —</div><div class="muted" id="claude-io">—</div><div class="muted" id="claude-cache">—</div></div></div><div id="claude-status-groups" class="status-groups"></div></section>
<section class="block" data-block="agy" draggable="true"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;flex-wrap:wrap;gap:8px"><h2 class="block-title"><span class="handle">⠿</span>Antigravity<span id="agy-sync-badge" style="font-size:11px;font-weight:400;color:var(--mint);margin-left:6px"></span></h2><div id="google-status-badge" class="service-badge">● Google Status: 取得中</div></div><section class="cards"><article class="card"><div class="label" style="display:flex;justify-content:space-between"><span>GEMINI MODELS</span><span class="muted" id="agy-gem-state">正常</span></div><div style="margin-top:7px"><div style="display:flex;justify-content:space-between;font-size:12px"><span>5h: <b id="agy-gem-5h-val">—</b></span><span class="muted" id="agy-gem-5h-rem">残り: —</span></div><div class="bar" style="margin-top:3px;height:6px"><i id="agy-gem-5h-bar"></i></div></div><div style="margin-top:9px"><div style="display:flex;justify-content:space-between;font-size:12px"><span>7d: <b id="agy-gem-7d-val">—</b><span id="agy-gem-burn"></span></span><span class="muted" id="agy-gem-7d-rem">残り: —</span></div><div class="bar" style="margin-top:3px;height:6px"><i id="agy-gem-7d-bar"></i></div></div><div class="muted" id="agy-gem-reset" style="margin-top:6px;font-size:11px">解除: —</div></article><article class="card"><div class="label" style="display:flex;justify-content:space-between"><span>CLAUDE &amp; GPT</span><span class="muted" id="agy-cld-state">正常</span></div><div style="margin-top:7px"><div style="display:flex;justify-content:space-between;font-size:12px"><span>5h: <b id="agy-cld-5h-val">—</b></span><span class="muted" id="agy-cld-5h-rem">残り: —</span></div><div class="bar" style="margin-top:3px;height:6px"><i id="agy-cld-5h-bar"></i></div></div><div style="margin-top:9px"><div style="display:flex;justify-content:space-between;font-size:12px"><span>7d: <b id="agy-cld-7d-val">—</b><span id="agy-cld-burn"></span></span><span class="muted" id="agy-cld-7d-rem">残り: —</span></div><div class="bar" style="margin-top:3px;height:6px"><i id="agy-cld-7d-bar"></i></div></div><div class="muted" id="agy-cld-reset" style="margin-top:6px;font-size:11px">解除: —</div></article><article class="card"><div class="label" style="display:flex;justify-content:space-between"><span>GEMINI 4 ARGON</span><span class="muted" id="agy-arg-state">正常</span></div><div style="margin-top:7px"><div style="display:flex;justify-content:space-between;font-size:12px"><span>7d: <b id="agy-arg-7d-val">—</b><span id="agy-arg-burn"></span></span><span class="muted" id="agy-arg-7d-rem">残り: —</span></div><div class="bar" style="margin-top:3px;height:6px"><i id="agy-arg-7d-bar"></i></div></div><div class="muted" id="agy-arg-reset" style="margin-top:6px;font-size:11px">解除: —</div></article><article class="card"><div class="label" style="display:flex;justify-content:space-between"><span>1m MONTHLY<span id="agy-1m-burn"></span></span><span class="muted" id="agy-1m-val">—</span></div><div style="margin-top:7px"><div style="display:flex;justify-content:space-between;font-size:12px"><b id="agy-1m-cost">—</b><span class="muted" id="agy-1m-rem">残り: —</span></div><div class="bar" style="margin-top:3px;height:6px"><i id="agy-1m-bar"></i></div></div><div class="muted" id="agy-1m-reset" style="margin-top:6px;font-size:11px">リセット: —</div><div class="muted" id="agy-1m-detail" style="font-size:11px">トークン: —</div></article><article class="card"><div class="label">TOKENS &amp; COST</div><div class="value" id="agy-cost">—</div><div class="muted" id="agy-tokens">IN / OUT: —</div><div class="muted" id="agy-steps">ACTIVITY: —</div><div id="agy-bloat"></div><div class="muted" id="agy-cost-note" style="margin-top:4px">API相当（推定費用）</div></article></section><div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-top:8px;flex-wrap:wrap"><div style="display:flex;align-items:center;gap:8px"><div class="plan-badge" id="agy-plan-view" title="クリックして契約プランを選択">PLAN: 未設定 <i>⚙️ 変更</i></div><div class="service-badge" id="agy-monthly-badge">当月: —</div><button type="button" class="btn-small" id="agy-quick-usage" title="手動で使用率%とリセット日を入力">⚙️ 使用率を設定</button></div></div><div id="google-status-groups" class="status-groups"></div></section>
<section class="block" data-block="tasks" draggable="true"><h2 class="block-title"><span class="handle">⠿</span>Active tasks</h2><div id="tasks" class="muted">データなし</div></section>
<section class="block" data-block="history" draggable="true"><h2 class="block-title"><span class="handle">⠿</span>Recent snapshots</h2><div id="history" class="muted">履歴なし</div></section>
</div><dialog id="agy-usage-dialog" style="background:color-mix(in srgb,var(--panel) 94%,transparent);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);color:var(--text);border:1px solid color-mix(in srgb,var(--line) 90%,rgba(255,255,255,0.08));border-radius:14px;padding:20px;max-width:390px;box-shadow:0 24px 48px rgba(0,0,0,0.4),0 0 0 1px rgba(255,255,255,0.04)"><form method="dialog" id="agy-usage-form"><h3 style="margin:0 0 10px;font-size:15px;color:var(--mint)">Antigravity 使用量設定</h3><p class="muted" style="margin:0 0 12px;font-size:12px">IDEの「View Usage」に表示されている<b>残り% (Remaining)</b>を入力してください。</p><div style="margin-bottom:10px;border:1px solid var(--line);border-radius:8px;padding:10px;background:var(--card)"><b style="font-size:13px;display:block;margin-bottom:6px">Gemini Models</b><label style="display:flex;justify-content:space-between;align-items:center;font-size:12px;margin-bottom:6px"><span>5-Hour Limit 残り%</span><input type="number" id="input-gem-5h" min="0" max="100" placeholder="84" style="width:70px;background:var(--badge);border:1px solid var(--line);color:var(--text);padding:4px 6px;border-radius:4px;text-align:right">%</label><label style="display:flex;justify-content:space-between;align-items:center;font-size:12px"><span>Weekly Limit 残り%</span><input type="number" id="input-gem-7d" min="0" max="100" placeholder="92" style="width:70px;background:var(--badge);border:1px solid var(--line);color:var(--text);padding:4px 6px;border-radius:4px;text-align:right">%</label></div><div style="margin-bottom:14px;border:1px solid var(--line);border-radius:8px;padding:10px;background:var(--card)"><b style="font-size:13px;display:block;margin-bottom:6px">Claude &amp; GPT models</b><label style="display:flex;justify-content:space-between;align-items:center;font-size:12px;margin-bottom:6px"><span>5-Hour Limit 残り%</span><input type="number" id="input-cld-5h" min="0" max="100" placeholder="0" style="width:70px;background:var(--badge);border:1px solid var(--line);color:var(--text);padding:4px 6px;border-radius:4px;text-align:right">%</label><label style="display:flex;justify-content:space-between;align-items:center;font-size:12px"><span>Weekly Limit 残り%</span><input type="number" id="input-cld-7d" min="0" max="100" placeholder="66" style="width:70px;background:var(--badge);border:1px solid var(--line);color:var(--text);padding:4px 6px;border-radius:4px;text-align:right">%</label></div><div style="margin-bottom:14px;border:1px solid var(--line);border-radius:8px;padding:10px;background:var(--card)"><b style="font-size:13px;display:block;margin-bottom:6px">Gemini 4 Argon</b><label style="display:flex;justify-content:space-between;align-items:center;font-size:12px"><span>Weekly Limit 残り%</span><input type="number" id="input-arg-7d" min="0" max="100" placeholder="70" style="width:70px;background:var(--badge);border:1px solid var(--line);color:var(--text);padding:4px 6px;border-radius:4px;text-align:right">%</label></div><div style="display:flex;justify-content:flex-end;gap:8px"><button type="button" class="btn-small" id="agy-dialog-cancel">キャンセル</button><button type="submit" class="btn-small" style="background:var(--mint);color:var(--bg);font-weight:700">保存して反映</button></div></form></dialog></main>
<script>
let serverStopped=false;const autoStop=()=>{if(!serverStopped){navigator.sendBeacon('/api/shutdown?delay=3')}};window.addEventListener('pagehide',autoStop);window.addEventListener('beforeunload',autoStop);
const $=id=>document.getElementById(id),layout=$('layout'),layoutKey='ai-vitals.layout.v4',defaults=['overview','codex','claude','agy','tasks','history'];const text=(id,value)=>{const el=$(id);if(el)el.textContent=value??'—';};const tokens=value=>value>=1000?(value/1000).toFixed(1)+'k':String(value??0);const resetAt=value=>value?new Date(value*1000).toLocaleString('ja-JP',{timeZone:'Asia/Tokyo'}):'—';const indicatorColor=value=>value>=80?'#ff7a7a':value>=60?'#ffce6a':'#72dfb5';
const themeKey='ai-vitals.theme';function applyTheme(theme){if(!theme||theme==='default'){document.documentElement.removeAttribute('data-theme');localStorage.removeItem(themeKey)}else{document.documentElement.setAttribute('data-theme',theme);localStorage.setItem(themeKey,theme)}const s=$('theme-select');if(s)s.value=theme||'default'}applyTheme(localStorage.getItem(themeKey)||'default');$('theme-select')?.addEventListener('change',e=>applyTheme(e.target.value));
function formatRemaining(resetsAtSec){if(!resetsAtSec)return'';const diff=Math.floor(resetsAtSec-Date.now()/1000);if(diff<=0)return'リセット完了';const d=Math.floor(diff/86400),h=Math.floor((diff%86400)/3600),m=Math.floor((diff%3600)/60),s=diff%60;if(d>0)return`あと${d}日${h}時間${m}分`;if(h>0)return`あと${h}時間${m}分${s}秒`;return`あと${m}分${s}秒`}
function updateCountdowns(){document.querySelectorAll('[data-resets-at]').forEach(el=>{const ts=parseFloat(el.dataset.resetsAt);if(!ts)return;const prefix=el.dataset.resetPrefix||'解除: ';const rem=formatRemaining(ts);el.textContent=`${prefix}${resetAt(ts)} (${rem})`})}
setInterval(updateCountdowns,1000);
function burnRateBadge(usedPercent,resetsAtSec,windowMinutes=10080){if(usedPercent==null||!resetsAtSec)return'';const nowSec=Date.now()/1000,totalSec=windowMinutes*60,remSec=Math.max(0,resetsAtSec-nowSec),elapsedSec=Math.max(0,totalSec-remSec);if(elapsedSec<1800)return'';const timePct=(elapsedSec/totalSec)*100,diff=usedPercent-timePct;if(diff>15)return'<span class="burn-badge warn" title="現在の消費ペースが速く、リセット前に上限へ達する恐れがあります">⚡ ハイペース注意</span>';if(diff<-10)return'<span class="burn-badge safe" title="週枠に対して余裕のある安定ペースです">⚡ 安全ペース</span>';return'<span class="burn-badge normal" title="時間経過に見合った適正なペースです">⚡ 適正ペース</span>'}
function renderBloat(id,isBloated,msg='⚠️ 会話肥大化（新規会話や /compact 推奨）'){const el=$(id);if(!el)return;el.innerHTML=isBloated?`<span class="bloat-alert" title="トークン消費が増大し、応答速度や精度の低下を招く可能性があります">${msg}</span>`:''}
let notifyEnabled=localStorage.getItem('ai-vitals.notify')==='1';function updateNotifyButton(){const btn=$('notify-toggle');if(!btn)return;if(notifyEnabled&&window.Notification?.permission==='granted'){btn.textContent='🔔 通知: オン';btn.classList.add('active')}else{btn.textContent='🔔 通知: オフ';btn.classList.remove('active')}}
$('notify-toggle').addEventListener('click',async()=>{if(!('Notification' in window)){alert('お使いのブラウザはデスクトップ通知に対応していません');return}if(Notification.permission==='granted'){notifyEnabled=!notifyEnabled}else{const perm=await Notification.requestPermission();notifyEnabled=(perm==='granted')}localStorage.setItem('ai-vitals.notify',notifyEnabled?'1':'0');updateNotifyButton();if(notifyEnabled&&Notification.permission==='granted'){new Notification('AI Vitals',{body:'クォータ上限到達やリセット解除の通知を有効にしました。'})}});updateNotifyButton();
let prevQuotas={};function checkNotify(key,remaining,toolName,quotaName){if(!notifyEnabled||window.Notification?.permission!=='granted')return;const prev=prevQuotas[key];if(prev!==undefined){if(remaining===0&&prev>0){new Notification(`⚠️ ${toolName}: 上限到達`,{body:`${quotaName} の利用枠を使い切りました。`})}else if(remaining>0&&prev===0){new Notification(`🎉 ${toolName}: 枠回復！`,{body:`${quotaName} の利用枠がリセットされました。開発を再開できます。`})}}prevQuotas[key]=remaining}
function savedLayout(){try{return JSON.parse(localStorage.getItem(layoutKey))||{order:defaults,hidden:[],plans:{}}}catch{return{order:defaults,hidden:[],plans:{}}}}function saveLayout(){try{localStorage.setItem(layoutKey,JSON.stringify({order:[...layout.children].map(node=>node.dataset.block),hidden:[...layout.children].filter(node=>node.hidden).map(node=>node.dataset.block),plans:{codex:$('codex-plan-select').value,claude:$('claude-plan-select').value,agy:$('agy-plan-select').value}}))}catch{}}
function renderPlans(){const codexVal=$('codex-plan-select').value;const claudeVal=$('claude-plan-select').value;const agyVal=$('agy-plan-select').value;$('codex-plan-view').innerHTML=`PLAN: <b>${codexVal}</b> <i>⚙️ 変更</i>`;$('claude-plan-view').innerHTML=`PLAN: <b>${claudeVal}</b> <i>⚙️ 変更</i>`;$('agy-plan-view').innerHTML=`PLAN: <b>${agyVal}</b> <i>⚙️ 変更</i>`}
function openPlanSetting(selectId){const d=$('layout-settings');if(d){d.open=true;const sel=$(selectId);if(sel){sel.focus();sel.scrollIntoView({behavior:'smooth',block:'center'})}}}
$('codex-plan-view').addEventListener('click',()=>openPlanSetting('codex-plan-select'));$('claude-plan-view').addEventListener('click',()=>openPlanSetting('claude-plan-select'));$('agy-plan-view').addEventListener('click',()=>openPlanSetting('agy-plan-select'));
const itemList=$('layout-item-list');function updateOrderButtons(){if(!itemList)return;const items=[...itemList.children];items.forEach((item,index)=>{const up=item.querySelector('[data-dir="up"]');const down=item.querySelector('[data-dir="down"]');if(up)up.disabled=(index===0);if(down)down.disabled=(index===items.length-1)})}function syncMenuOrder(){if(!itemList)return;[...layout.children].forEach(b=>{if(b.dataset?.block){const item=itemList.querySelector(`[data-item="${b.dataset.block}"]`);if(item)itemList.append(item)}});updateOrderButtons()}
function applyLayout(){const value=savedLayout(),order=[...(value.order||defaults),...defaults.filter(name=>!(value.order||defaults).includes(name))];order.forEach(name=>{const block=layout.querySelector(`[data-block="${name}"]`);if(block){layout.append(block);block.hidden=(value.hidden||[]).includes(name);const toggle=document.querySelector(`[data-toggle="${name}"]`);if(toggle)toggle.checked=!block.hidden}});$('codex-plan-select').value=value.plans?.codex||'未設定';$('claude-plan-select').value=value.plans?.claude||'未設定';$('agy-plan-select').value=value.plans?.agy||'未設定';renderPlans();syncMenuOrder()}
document.querySelectorAll('[data-toggle]').forEach(toggle=>toggle.addEventListener('change',()=>{layout.querySelector(`[data-block="${toggle.dataset.toggle}"]`).hidden=!toggle.checked;saveLayout()}));document.querySelectorAll('#codex-plan-select,#claude-plan-select,#agy-plan-select').forEach(select=>select.addEventListener('change',()=>{renderPlans();saveLayout()}));$('layout-reset').addEventListener('click',()=>{try{localStorage.removeItem(layoutKey)}catch{}applyLayout()});$('shutdown').addEventListener('click',async()=>{if(!confirm('AI Vitalsを停止しますか？'))return;try{await fetch('/api/shutdown',{method:'POST'});serverStopped=true;text('updated','停止しました');$('shutdown').disabled=true}catch(error){text('updated','停止エラー: '+error.message)}});let dragged=null;layout.querySelectorAll('[data-block]').forEach(block=>{block.addEventListener('dragstart',()=>{dragged=block;block.classList.add('dragging')});block.addEventListener('dragend',()=>{block.classList.remove('dragging');dragged=null;syncMenuOrder();saveLayout()});block.addEventListener('dragover',event=>event.preventDefault());block.addEventListener('drop',event=>{event.preventDefault();if(dragged&&dragged!==block){layout.insertBefore(dragged,block);syncMenuOrder()}})});itemList?.addEventListener('click',e=>{const btn=e.target.closest('.btn-order');if(!btn||btn.disabled)return;const item=btn.closest('.layout-item'),name=item?.dataset?.item,dir=btn.dataset.dir;const block=layout.querySelector(`[data-block="${name}"]`);if(!block)return;if(dir==='up'&&block.previousElementSibling){layout.insertBefore(block,block.previousElementSibling)}else if(dir==='down'&&block.nextElementSibling){layout.insertBefore(block,block.nextElementSibling.nextElementSibling)}syncMenuOrder();saveLayout()});const menuDropdown=$('layout-settings');document.addEventListener('click',e=>{if(menuDropdown&&menuDropdown.open&&!menuDropdown.contains(e.target)){menuDropdown.open=false}});document.addEventListener('keydown',e=>{if(e.key==='Escape'&&menuDropdown&&menuDropdown.open){menuDropdown.open=false}});applyLayout();
function duration(minutes){const days=Math.floor(minutes/1440),hours=Math.floor(minutes%1440/60),mins=minutes%60;return[[days,days+'日'],[hours,hours+'時間'],[mins,mins+'分']].filter(([value])=>value).map(([,label])=>label).join('')||'0分'}function paintBar(name,value){const bar=$(name+'-bar'),color=indicatorColor(value??0);if(bar){bar.style.width=(value??0)+'%';bar.style.background=color;bar.style.boxShadow='0 0 12px '+color}}
function limit(name,data){const nowSec=Date.now()/1000;const isExpired=data?.is_reset||(data?.resets_at&&data.resets_at<=nowSec);const used=isExpired?0:data?.used_percent;const windowMinutes=data?.window_minutes,usedMinutes=Math.round((windowMinutes||0)*(used||0)/100);text(name,used==null?'—':`${used}%`);const resetEl=$(name+'-reset');if(isExpired){resetEl.removeAttribute('data-resets-at');resetEl.textContent='解除: リセット済み';text(name+'-detail',windowMinutes?`リセット完了 · 全枠利用可能 (${duration(windowMinutes)}枠)`:'リセット完了')}else if(data?.resets_at){resetEl.dataset.resetsAt=data.resets_at;resetEl.dataset.resetPrefix='解除: ';resetEl.textContent='解除: '+resetAt(data.resets_at)+' ('+formatRemaining(data.resets_at)+')';text(name+'-detail',windowMinutes?`使用: ${duration(usedMinutes)} / ${duration(windowMinutes)} · 残り: ${duration(Math.max(0,windowMinutes-usedMinutes))}`:'使用: — · 残り: —')}else{resetEl.removeAttribute('data-resets-at');resetEl.textContent='解除: —';text(name+'-detail','使用: — · 残り: —')}paintBar(name,used);const burnEl=$(name+'-burn');if(burnEl){burnEl.innerHTML=burnRateBadge(used,data?.resets_at,windowMinutes)}}
function renderCost(id,noteId,value,model,range){if(value==null){text(id,'見積不可');text(noteId,model?'公式API単価が未掲載（掲載時に対応）':'モデル情報なし');return}text(id,range?`$${value.minimum_usd.toFixed(4)}–$${value.maximum_usd.toFixed(4)}`:'$'+value.toFixed(4));text(noteId,range?'API相当・cache TTL不明の試算範囲':'API相当・購読請求額ではありません')}
function renderStatus(prefix,status){const badge=$(prefix+'-status-badge');const state=status?.state||'取得不可';let color='var(--muted)',dotColor='#91a1bb';if(state.includes('正常')||state==='operational'||state==='OK'){color='var(--mint)';dotColor='#72dfb5'}else if(state.includes('障害')||state.includes('停止')){color='var(--bad)';dotColor='#ff7a7a'}else if(state!=='取得不可'){color='var(--warn)';dotColor='#ffce6a'}const nameMap={openai:'OpenAI',claude:'Claude',google:'Google'};const sName=nameMap[prefix]||prefix;let overallPercent=null;if(status?.groups&&status.groups.length>0){const sum=status.groups.reduce((acc,g)=>acc+(g.normal_percent||0),0);overallPercent=Math.round(sum/status.groups.length)}const pctStr=overallPercent!=null?` · 90日: ${overallPercent}%`:'';if(badge){badge.innerHTML=`<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:${dotColor}"></span>${sName} Status: <b style="color:${color}">${state}</b>${pctStr}`}const groups=$(prefix+'-status-groups');if(groups){groups.replaceChildren(...(status?.groups||[]).map(group=>{const row=document.createElement('section');row.className='status-group';const head=document.createElement('div');head.className='status-head';const name=document.createElement('span');name.className='status-name';name.textContent=`● ${group.name}`;const meta=document.createElement('span');meta.className='muted';meta.textContent=`正常日割合 ${group.normal_percent}%`;head.append(name,meta);const bars=document.createElement('div');bars.className='status-bars';bars.setAttribute('aria-label',`${group.name} の90日インシデント履歴`);(group.days||[]).forEach((state,index)=>{const day=document.createElement('i');day.className='status-day '+state;day.title=`${89-index}日前: ${state==='ok'?'正常':state==='warn'?'軽微な障害':'重大な障害'}`;bars.append(day)});const details=document.createElement('details'),summary=document.createElement('summary');summary.textContent=`構成要素 ${group.components?.length||0} 件（${group.state}）`;details.append(summary,...(group.components||[]).map(component=>{const item=document.createElement('div');item.textContent=`${component.name}: ${component.state}`;return item}));row.append(head,bars,details);return row}));if(!groups.childNodes.length)groups.textContent='グループ情報を取得できませんでした'}}
let latestAgyLimits=null;const agyDialog=$('agy-usage-dialog');
$('agy-quick-usage').addEventListener('click',()=>{if(latestAgyLimits){const g=latestAgyLimits.gemini||{},c=latestAgyLimits.claude_gpt||{},a=latestAgyLimits.argon||{};const g5=g.primary?.used_percent,g7=g.secondary?.used_percent;const c5=c.primary?.used_percent,c7=c.secondary?.used_percent;const a7=a.secondary?.used_percent;$('input-gem-5h').value=g5!=null?Math.max(0,100-g5):'';$('input-gem-7d').value=g7!=null?Math.max(0,100-g7):'';$('input-cld-5h').value=c5!=null?Math.max(0,100-c5):'';$('input-cld-7d').value=c7!=null?Math.max(0,100-c7):'';$('input-arg-7d').value=a7!=null?Math.max(0,100-a7):''}if(agyDialog.showModal){agyDialog.showModal()}else{agyDialog.setAttribute('open','')}});
$('agy-dialog-cancel').addEventListener('click',()=>{if(agyDialog.close){agyDialog.close()}else{agyDialog.removeAttribute('open')}});
$('agy-usage-form').addEventListener('submit',async(e)=>{e.preventDefault();const g5=$('input-gem-5h').value.trim(),g7=$('input-gem-7d').value.trim(),c5=$('input-cld-5h').value.trim(),c7=$('input-cld-7d').value.trim(),a7=$('input-arg-7d').value.trim();const payload={};if(g5!=='')payload.gemini_5h_remaining=parseInt(g5,10);if(g7!=='')payload.gemini_7d_remaining=parseInt(g7,10);if(c5!=='')payload.claude_5h_remaining=parseInt(c5,10);if(c7!=='')payload.claude_7d_remaining=parseInt(c7,10);if(a7!=='')payload.argon_7d_remaining=parseInt(a7,10);try{await fetch('/api/agy-usage',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});if(agyDialog.close){agyDialog.close()}else{agyDialog.removeAttribute('open')};loadSnapshot()}catch(err){alert('保存に失敗しました: '+err.message)}});
function renderAgyLimits(prefix,limitsData,quota,usageStats){const pri=limitsData?.primary,sec=limitsData?.secondary;const nowSec=Date.now()/1000;const isPriLive=Boolean(pri?.is_live);const isSecLive=Boolean(sec?.is_live);const priExpired=pri?.is_reset||(pri?.resets_at&&pri.resets_at<=nowSec);const secExpired=sec?.is_reset||(sec?.resets_at&&sec.resets_at<=nowSec);const hasManualPri=pri?.used_percent!=null&&!priExpired&&!isPriLive;const hasManualSec=sec?.used_percent!=null&&!secExpired&&!isSecLive;const hasPlan=Boolean(quota?.["5h"]||quota?.["7d"]);let autoPriUsed=null,autoSecUsed=null;if(quota?.["5h"]&&usageStats?.steps_5h!=null){autoPriUsed=Math.min(100,Math.round((usageStats.steps_5h/quota["5h"])*100))}if(quota?.["7d"]&&usageStats?.steps_7d!=null){autoSecUsed=Math.min(100,Math.round((usageStats.steps_7d/quota["7d"])*100))}const isPriAuto=!isPriLive&&!hasManualPri&&hasPlan&&autoPriUsed!=null;const isSecAuto=!isSecLive&&!hasManualSec&&hasPlan&&autoSecUsed!=null;let priUsed=isPriLive?pri.used_percent:(hasManualPri?pri.used_percent:(isPriAuto?autoPriUsed:(priExpired?0:pri?.used_percent)));let secUsed=isSecLive?sec.used_percent:(hasManualSec?sec.used_percent:(isSecAuto?autoSecUsed:(secExpired?0:sec?.used_percent)));let priRem=Math.max(0,100-(priUsed??0));let secRem=Math.max(0,100-(secUsed??0));const priLabel=isPriLive?' (公式同期)':(isPriAuto?' (自動)':(hasManualPri?' (手動)':''));const secLabel=isSecLive?' (公式同期)':(isSecAuto?' (自動)':(hasManualSec?' (手動)':''));text(prefix+'-5h-val',priUsed!=null?priUsed+'%':'—');text(prefix+'-5h-rem',priRem===0?'残り: 0% (上限)':(`残り: ${priRem}%`+priLabel));const r5=$(prefix+'-5h-rem');if(r5)r5.style.color=priRem===0?'var(--bad)':'var(--muted)';paintBar(prefix+'-5h',priUsed);text(prefix+'-7d-val',secUsed!=null?secUsed+'%':'—');text(prefix+'-7d-rem',secRem===0?'残り: 0% (上限)':(`残り: ${secRem}%`+secLabel));const r7=$(prefix+'-7d-rem');if(r7)r7.style.color=secRem===0?'var(--bad)':'var(--muted)';paintBar(prefix+'-7d',secUsed);const resetTexts=[];if(isPriLive&&pri?.resets_at){resetTexts.push(priExpired?'5h: リセット済み':`5h解除: ${resetAt(pri.resets_at)} (${formatRemaining(pri.resets_at)})`)}else if(isPriAuto&&quota?.["5h"]){resetTexts.push(`5h: ${usageStats?.steps_5h||0}/${quota["5h"]}st (自動)`)}else if(!isPriAuto&&pri?.resets_at){resetTexts.push(priExpired?'5h: リセット済み':`5h解除: ${resetAt(pri.resets_at)} (${formatRemaining(pri.resets_at)})`)}if(isSecLive&&sec?.resets_at){resetTexts.push(secExpired?'7d: リセット済み':`7d解除: ${resetAt(sec.resets_at)} (${formatRemaining(sec.resets_at)})`)}else if(isSecAuto&&quota?.["7d"]){resetTexts.push(`7d: ${usageStats?.steps_7d||0}/${quota["7d"]}st (自動)`)}else if(!isSecAuto&&sec?.resets_at){resetTexts.push(secExpired?'7d: リセット済み':`7d解除: ${resetAt(sec.resets_at)} (${formatRemaining(sec.resets_at)})`)}text(prefix+'-reset',resetTexts.join(' · ')||(isPriLive||isSecLive||isPriAuto||isSecAuto?'自動同期中':'解除: —'));const burnEl=$(prefix+'-burn');if(burnEl){burnEl.innerHTML=burnRateBadge(secUsed,sec?.resets_at,10080)}const stateEl=$(prefix+'-state');if(stateEl){if(priRem===0||secRem===0){stateEl.textContent='上限到達';stateEl.style.color='var(--bad)'}else if((priUsed&&priUsed>=80)||(secUsed&&secUsed>=80)){stateEl.textContent='制限警戒';stateEl.style.color='var(--warn)'}else{stateEl.textContent='正常';stateEl.style.color='var(--mint)'}}}
let monthlyPopulated=false;
function populateMonthlyInputs(settings){if(!settings||monthlyPopulated)return;const activeEl=document.activeElement;if(activeEl&&activeEl.id&&activeEl.id.startsWith('monthly-'))return;monthlyPopulated=true;if($('monthly-billing-day'))$('monthly-billing-day').value=settings.billing_day||1;if($('monthly-codex-budget'))$('monthly-codex-budget').value=settings.codex?.budget_usd||'';if($('monthly-codex-tokens'))$('monthly-codex-tokens').value=settings.codex?.token_limit||'';if($('monthly-claude-budget'))$('monthly-claude-budget').value=settings.claude?.budget_usd||'';if($('monthly-claude-tokens'))$('monthly-claude-tokens').value=settings.claude?.token_limit||'';if($('monthly-agy-budget'))$('monthly-agy-budget').value=settings.agy?.budget_usd||'';if($('monthly-agy-tokens'))$('monthly-agy-tokens').value=settings.agy?.token_limit||'';if($('monthly-total-budget'))$('monthly-total-budget').value=settings.total_budget_usd||'';if($('monthly-total-tokens'))$('monthly-total-tokens').value=settings.total_token_limit||''}
$('save-monthly-btn')?.addEventListener('click',async()=>{const payload={billing_day:parseInt($('monthly-billing-day').value,10)||1,codex:{budget_usd:parseFloat($('monthly-codex-budget').value)||0,token_limit:parseInt($('monthly-codex-tokens').value,10)||0},claude:{budget_usd:parseFloat($('monthly-claude-budget').value)||0,token_limit:parseInt($('monthly-claude-tokens').value,10)||0},agy:{budget_usd:parseFloat($('monthly-agy-budget').value)||0,token_limit:parseInt($('monthly-agy-tokens').value,10)||0}};const totB=$('monthly-total-budget').value.trim();const totT=$('monthly-total-tokens').value.trim();if(totB!=='')payload.total_budget_usd=parseFloat(totB)||0;if(totT!=='')payload.total_token_limit=parseInt(totT,10)||0;try{await fetch('/api/monthly-settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});alert('月間予算・上限設定を保存しました');loadSnapshot()}catch(err){alert('設定の保存に失敗しました: '+err.message)}});
function monthlyBurnBadge(burnRate){if(burnRate==='warn')return'<span class="burn-badge warn" title="経過時間に対して消費が非常に速く、上限に達する恐れがあります">⚡ ハイペース注意</span>';if(burnRate==='safe')return'<span class="burn-badge safe" title="月間枠に対して余裕のある安定ペースです">⚡ 安全ペース</span>';if(burnRate==='normal')return'<span class="burn-badge normal" title="月内の日数経過に見合った適正なペースです">⚡ 適正ペース</span>';return''}
function renderOverview(data){const tasks=data.tasks||[],todayStr=new Date().toLocaleDateString('ja-JP');const todayTasks=tasks.filter(t=>t.observed_at&&new Date(t.observed_at).toLocaleDateString('ja-JP')===todayStr);text('ov-tasks',`${todayTasks.length} tasks`);const counts={Codex:0,Claude:0,Antigravity:0};todayTasks.forEach(t=>{if(counts[t.provider]!=null)counts[t.provider]++;else if(t.provider==='AGY')counts.Antigravity++});text('ov-tasks-detail',`本日: Codex ${counts.Codex} · Claude ${counts.Claude} · Antigravity ${counts.Antigravity}`);const current=data.current||{},claude=data.claude||{},agy=data.agy||{};let totalCost=0;if(current.estimated_cost_usd!=null)totalCost+=current.estimated_cost_usd;if(claude.estimated_cost_usd!=null){totalCost+=(claude.estimated_cost_usd.minimum_usd+claude.estimated_cost_usd.maximum_usd)/2}if(agy.estimated_cost_usd!=null)totalCost+=agy.estimated_cost_usd;text('ov-cost',totalCost>0?`$${totalCost.toFixed(4)}`:'$0.0000');text('ov-cost-detail','3ツール合算 (API相当額)');let totalTokens=0;if(current.context?.used_tokens)totalTokens+=current.context.used_tokens;if(claude.usage?.total_tokens)totalTokens+=claude.usage.total_tokens;const agyTok=agy.usage?.total_tokens||((agy.usage?.input_tokens||0)+(agy.usage?.output_tokens||0));totalTokens+=agyTok;text('ov-tokens',tokens(totalTokens));text('ov-tokens-detail',`Codex: ${tokens(current.context?.used_tokens||0)} · Claude: ${tokens(claude.usage?.total_tokens||0)} · Antigravity: ${tokens(agyTok)}`);let mostActive='—',maxCount=0;for(const[p,c]of Object.entries(counts)){if(c>maxCount){maxCount=c;mostActive=p}}text('ov-active-tool',mostActive!=='—'?`${mostActive} (${maxCount}回)`:(tasks.length>0?(tasks[0].provider==='AGY'?'Antigravity':tasks[0].provider):'—'));text('ov-active-detail',maxCount>0?'本日最も多くタスクを実行':'セッション待機中');const statuses=[data.statuses?.openai?.state,data.statuses?.claude?.state,data.statuses?.google?.state];const hasOutage=statuses.some(s=>s&&(s.includes('障害')||s.includes('停止'))),hasDegraded=statuses.some(s=>s&&!s.includes('正常')&&!s.includes('Operational')&&s!=='OK'&&s!=='取得不可');const hBadge=$('overview-health-badge');if(hBadge){if(hasOutage){hBadge.innerHTML='<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:#ff7a7a"></span>システム健全性: <b style="color:var(--bad)">障害発生中</b>'}else if(hasDegraded){hBadge.innerHTML='<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:#ffce6a"></span>システム健全性: <b style="color:var(--warn)">一部低下中</b>'}else{hBadge.innerHTML='<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:#72dfb5"></span>システム健全性: <b style="color:var(--mint)">全システム正常</b>'}}const m=data.monthly||{};populateMonthlyInputs(m.settings);const mStart=m.start_at?new Date(m.start_at).toLocaleDateString('ja-JP',{month:'numeric',day:'numeric'}):'—';const mEnd=m.end_at?new Date(m.end_at).toLocaleDateString('ja-JP',{month:'numeric',day:'numeric'}):'—';text('monthly-period-badge',`当月集計: ${mStart}〜${mEnd}`);text('ov-monthly-time-progress',m.days_elapsed!=null?`期間経過: ${m.days_elapsed}日 / ${m.days_total}日 (${m.time_progress_percent}%)`:'期間経過: —');const mTot=m.total||{};text('ov-monthly-tasks',`${mTot.tasks||0} tasks`);text('ov-monthly-tasks-detail',`当月: Codex ${m.codex?.tasks||0} · Claude ${m.claude?.tasks||0} · Antigravity ${m.agy?.tasks||0}`);const mCost=mTot.estimated_cost_usd||0;const mBudget=mTot.budget_usd||0;text('ov-monthly-cost',`$${mCost.toFixed(4)}`+(mBudget>0?` / $${mBudget.toFixed(2)}`:''));const mCostPct=mTot.used_cost_percent;if(mBudget>0&&mCostPct!=null){const remCost=Math.max(0,mBudget-mCost);text('ov-monthly-cost-detail',`進捗: ${mCostPct}% · 残り: $${remCost.toFixed(2)}`);paintBar('ov-monthly-cost',mCostPct)}else{text('ov-monthly-cost-detail','予算未設定 (メニューから設定可能)');paintBar('ov-monthly-cost',0)}const burnCostEl=$('ov-monthly-burn');if(burnCostEl)burnCostEl.innerHTML=monthlyBurnBadge(mTot.burn_rate);const mTokens=mTot.total_tokens||0;const mLimit=mTot.token_limit||0;text('ov-monthly-tokens',tokens(mTokens)+(mLimit>0?` / ${tokens(mLimit)}`:''));const mTokPct=mTot.used_tokens_percent;if(mLimit>0&&mTokPct!=null){const remTok=Math.max(0,mLimit-mTokens);text('ov-monthly-tokens-detail',`進捗: ${mTokPct}% · 残り: ${tokens(remTok)}`);paintBar('ov-monthly-tokens',mTokPct)}else{text('ov-monthly-tokens-detail','枠未設定 (メニューから設定可能)');paintBar('ov-monthly-tokens',0)}const burnTokEl=$('ov-monthly-tokens-burn');if(burnTokEl)burnTokEl.innerHTML=monthlyBurnBadge(mTot.burn_rate);let paceText='適正ペース';if(mTot.burn_rate==='warn')paceText='⚡ ハイペース注意';else if(mTot.burn_rate==='safe')paceText='⚡ 安全ペース';else if(mBudget<=0&&mLimit<=0)paceText='予算未設定';text('ov-monthly-pace',paceText);text('ov-monthly-pace-detail',mTot.burn_rate?`月間経過 ${m.time_progress_percent}% に対する消費比較`:'月間予算を設定すると自動判定')}
function renderMonthlyCard(prefix,mTool,mRoot){const cost=mTool?.estimated_cost_usd||0,budget=mTool?.budget_usd||0,tok=mTool?.total_tokens||0,limit=mTool?.token_limit||0;const costPct=mTool?.used_cost_percent,tokPct=mTool?.used_tokens_percent;const burn=mTool?.burn_rate;const valPct=costPct!=null?costPct:(tokPct!=null?tokPct:null);text(prefix+'-1m-val',valPct!=null?`${valPct}%`:'—');const burnEl=$(prefix+'-1m-burn');if(burnEl)burnEl.innerHTML=monthlyBurnBadge(burn);text(prefix+'-1m-cost',`$${cost.toFixed(4)}`+(budget>0?` / $${budget.toFixed(2)}`:''));const remTxt=[];if(budget>0){const remB=Math.max(0,budget-cost);remTxt.push(`残り: $${remB.toFixed(2)}`)}else if(limit>0){const remT=Math.max(0,limit-tok);remTxt.push(`残り: ${tokens(remT)} tok`)}text(prefix+'-1m-rem',remTxt.length?remTxt.join(' · '):'残り: —');paintBar(prefix+'-1m',valPct);const resetEl=$(prefix+'-1m-reset');if(mRoot?.end_ts){resetEl.dataset.resetsAt=mRoot.end_ts;resetEl.dataset.resetPrefix='リセット: ';resetEl.textContent='リセット: '+resetAt(mRoot.end_ts)+' ('+formatRemaining(mRoot.end_ts)+')'}else{resetEl.removeAttribute('data-resets-at');resetEl.textContent='リセット: —'}const tokDetail=[`累計: ${tokens(tok)} tok`];if(limit>0)tokDetail.push(`枠: ${tokens(limit)} tok (${tokPct||0}%)`);if(mTool?.tasks!=null)tokDetail.push(`${mTool.tasks} tasks`);text(prefix+'-1m-detail',tokDetail.join(' · '))}
function render(data){renderOverview(data);const current=data.current||{};limit('primary',current.limits?.primary);limit('secondary',current.limits?.secondary);const context=current.context||{},used=context.used_percent,remaining=Math.max(0,(context.window_tokens||0)-(context.used_tokens||0));text('context-detail',context.window_tokens?`CONTEXT: ${tokens(context.used_tokens)} / ${tokens(context.window_tokens)} tokens (${used!=null?used+'%':'—'})`:'CONTEXT: —');text('context-remaining',context.window_tokens?`残り: ${tokens(remaining)} tokens`:'残り: —');paintBar('context',used);renderCost('cost','cost-note',current.estimated_cost_usd,current.model,false);renderBloat('context-bloat',(used!=null&&used>=70)||(context.used_tokens&&context.used_tokens>=100000));text('codex-model','SESSION: '+([current.model,current.observed_at&&new Date(current.observed_at).toLocaleString('ja-JP')].filter(Boolean).join(' · ')||'待機中'));const codexSyncBadge=$('codex-sync-badge');if(codexSyncBadge){if(current&&current.observed_at){codexSyncBadge.textContent='[ローカル同期中]';codexSyncBadge.style.color='var(--mint)'}else{codexSyncBadge.textContent=''}}if(current.limits?.primary?.used_percent!=null){checkNotify('codex-5h',Math.max(0,100-current.limits.primary.used_percent),'Codex','5h枠')}if(current.limits?.secondary?.used_percent!=null){checkNotify('codex-7d',Math.max(0,100-current.limits.secondary.used_percent),'Codex','7d枠')}const claude=data.claude||{},usage=claude.usage||{};limit('claude-primary',claude.limits?.primary);limit('claude-secondary',claude.limits?.secondary);text('claude-total',`SESSION: ${tokens(usage.total_tokens)}`);text('claude-model','MODEL: '+([claude.model,claude.observed_at&&new Date(claude.observed_at).toLocaleString('ja-JP')].filter(Boolean).join(' · ')||'待機中'));renderCost('claude-cost','claude-cost-note',claude.estimated_cost_usd,claude.model,true);renderBloat('claude-bloat',Boolean(usage.total_tokens&&usage.total_tokens>=100000));text('claude-io',`INPUT / OUTPUT: ${tokens(usage.input_tokens)} / ${tokens(usage.output_tokens)}`);text('claude-cache',`cache: ${tokens((usage.cache_creation_input_tokens||0)+(usage.cache_read_input_tokens||0))}`);const claudeSyncBadge=$('claude-sync-badge');if(claudeSyncBadge){if(claude.limits?.primary?.used_percent!=null||claude.limits?.secondary?.used_percent!=null){claudeSyncBadge.textContent=claude.limits?.source==='manual'?'[/usage同期中]':'[ローカル同期中]';claudeSyncBadge.style.color='var(--mint)'}else if(claude.observed_at){claudeSyncBadge.textContent='[ローカル同期中]';claudeSyncBadge.style.color='var(--mint)'}else{claudeSyncBadge.textContent=''}}if(claude.limits?.primary?.used_percent!=null){checkNotify('claude-5h',Math.max(0,100-claude.limits.primary.used_percent),'Claude Code','5h枠')}if(claude.limits?.secondary?.used_percent!=null){checkNotify('claude-7d',Math.max(0,100-claude.limits.secondary.used_percent),'Claude Code','7d枠')}const agy=data.agy||{},agyUsage=agy.usage||{};if(agy.plan&&($('agy-plan-select').value==='未設定'||$('agy-plan-select').value==='Google AI Plus')){const opts=[...$('agy-plan-select').options].map(o=>o.value);if(opts.includes(agy.plan)){$('agy-plan-select').value=agy.plan;renderPlans()}}const agyPlan=$('agy-plan-select').value;const planQuotas=data.plan_quotas||{},planQuotaObj=planQuotas[agyPlan]||{};latestAgyLimits=agy.limits||{};renderAgyLimits('agy-gem',agy.limits?.gemini,planQuotaObj.gemini,agyUsage.gemini);renderAgyLimits('agy-cld',agy.limits?.claude_gpt,planQuotaObj.claude_gpt,agyUsage.claude_gpt);renderAgyLimits('agy-arg',agy.limits?.argon,null,agyUsage.argon);const gemPri=agy.limits?.gemini?.primary,gemSec=agy.limits?.gemini?.secondary;if(gemPri?.used_percent!=null){checkNotify('agy-gem-5h',Math.max(0,100-gemPri.used_percent),'Antigravity (Gemini)','5h枠')}if(gemSec?.used_percent!=null){checkNotify('agy-gem-7d',Math.max(0,100-gemSec.used_percent),'Antigravity (Gemini)','7d枠')}const cldPri=agy.limits?.claude_gpt?.primary,cldSec=agy.limits?.claude_gpt?.secondary;if(cldPri?.used_percent!=null){checkNotify('agy-cld-5h',Math.max(0,100-cldPri.used_percent),'Antigravity (Claude/GPT)','5h枠')}if(cldSec?.used_percent!=null){checkNotify('agy-cld-7d',Math.max(0,100-cldSec.used_percent),'Antigravity (Claude/GPT)','7d枠')}const argSec=agy.limits?.argon?.secondary;if(argSec?.used_percent!=null){checkNotify('agy-arg-7d',Math.max(0,100-argSec.used_percent),'Antigravity (Gemini 4 Argon)','7d枠')}const agyTokTotal=agyUsage.total_tokens||(agyUsage.input_tokens||0)+(agyUsage.output_tokens||0);text('agy-tokens',`IN: ${tokens(agyUsage.input_tokens||0)} / OUT: ${tokens(agyUsage.output_tokens||0)} (${tokens(agyTokTotal)})`);text('agy-steps',`Gemini: ${agyUsage.gemini?.steps||0}st / Claude: ${agyUsage.claude_gpt?.steps||0}st / Argon: ${agyUsage.argon?.steps||0}st (${agyUsage.steps||0} total)`);renderBloat('agy-bloat',agyTokTotal>=120000);renderCost('agy-cost','agy-cost-note',agy.estimated_cost_usd,agy.model,false);const agySyncBadge=$('agy-sync-badge');if(agySyncBadge){if(agy.live_status){agySyncBadge.textContent='[IDE公式同期中]';agySyncBadge.style.color='var(--mint)'}else if(agy.limits?.gemini?.primary?.used_percent!=null||agy.limits?.claude_gpt?.primary?.used_percent!=null||agy.limits?.argon?.secondary?.used_percent!=null){agySyncBadge.textContent='[手動設定中]';agySyncBadge.style.color='var(--blue)'}else{agySyncBadge.textContent=''}}const m=data.monthly||{};const mCodex=m.codex||{};renderMonthlyCard('codex',mCodex,m);const codexBudgetTxt=mCodex.budget_usd>0?` / $${mCodex.budget_usd.toFixed(2)} (${mCodex.used_cost_percent}%)`:'';text('codex-monthly-badge',`当月: $${(mCodex.estimated_cost_usd||0).toFixed(4)}${codexBudgetTxt} · ${tokens(mCodex.total_tokens||0)} tok`);if(mCodex.used_cost_percent!=null&&mCodex.used_cost_percent>=100){checkNotify('monthly-codex-budget',0,'Codex','月間予算')}const mClaude=m.claude||{};renderMonthlyCard('claude',mClaude,m);const claudeBudgetTxt=mClaude.budget_usd>0?` / $${mClaude.budget_usd.toFixed(2)} (${mClaude.used_cost_percent}%)`:'';text('claude-monthly-badge',`当月: $${(mClaude.estimated_cost_usd||0).toFixed(4)}${claudeBudgetTxt} · ${tokens(mClaude.total_tokens||0)} tok`);if(mClaude.used_cost_percent!=null&&mClaude.used_cost_percent>=100){checkNotify('monthly-claude-budget',0,'Claude Code','月間予算')}const mAgy=m.agy||{};renderMonthlyCard('agy',mAgy,m);const agyBudgetTxt=mAgy.budget_usd>0?` / $${mAgy.budget_usd.toFixed(2)} (${mAgy.used_cost_percent}%)`:'';text('agy-monthly-badge',`当月: $${(mAgy.estimated_cost_usd||0).toFixed(4)}${agyBudgetTxt} · ${tokens(mAgy.total_tokens||0)} tok (${mAgy.steps||0} st)`);if(mAgy.used_cost_percent!=null&&mAgy.used_cost_percent>=100){checkNotify('monthly-agy-budget',0,'Antigravity','月間予算')}const mTot=m.total||{};if(mTot.used_cost_percent!=null&&mTot.used_cost_percent>=100){checkNotify('monthly-total-budget',0,'AI Vitals (全体)','月間全体予算')}renderStatus('openai',data.statuses?.openai);renderStatus('claude',data.statuses?.claude);renderStatus('google',data.statuses?.google);text('updated',data.snapshot_at?'更新 '+new Date(data.snapshot_at).toLocaleString('ja-JP'):(current.observed_at?'更新 '+new Date(current.observed_at).toLocaleString('ja-JP'):'セッション待機中'));const tasks=$('tasks');const tasksList=(data.tasks||[]).slice().sort((a,b)=>String(b.observed_at||'').localeCompare(String(a.observed_at||'')));tasks.replaceChildren(...tasksList.map((task,index)=>{const row=document.createElement('div');row.className='task';const head=document.createElement('div');head.className='task-head';const order=document.createElement('span');order.className='task-index';order.textContent=`#${index+1} · ${index}件前`;const title=document.createElement('b');title.textContent=task.task?.prompt||'プロンプト未取得';const observed=document.createElement('time');observed.className='task-time';observed.textContent=task.observed_at?new Date(task.observed_at).toLocaleString('ja-JP',{timeZone:'Asia/Tokyo'}):'日時不明';head.append(order,title,observed);const meta=document.createElement('span'),provider=document.createElement('i');provider.className='provider '+(task.provider==='Claude'?'claude':(task.provider==='Antigravity'||task.provider==='AGY')?'agy':'');provider.textContent=task.provider==='AGY'?'Antigravity':task.provider;meta.append(provider,document.createTextNode(' '+[task.task?.cwd,task.model].filter(Boolean).join(' · ')));row.append(head,meta);return row}));if(!tasks.childNodes.length)tasks.textContent='データなし';const history=$('history');history.replaceChildren(...(data.history||[]).reverse().map(item=>{const detail=document.createElement('details'),title=document.createElement('summary'),pre=document.createElement('pre');title.textContent=item.split('\\n')[0]||'snapshot';pre.textContent=item;detail.append(title,pre);return detail}));if(!history.childNodes.length)history.textContent='履歴なし'}
async function loadSnapshot(){try{render(await (await fetch('/api/snapshot',{cache:'no-store'})).json())}catch(error){text('updated','取得エラー: '+error.message)}}loadSnapshot();setInterval(loadSnapshot,15000);
</script></html>"""


def main(argv: list[str] | None = None) -> None:
    """コマンドライン引数をパースし、AI Vitals ダッシュボードサーバーまたはデータ保存処理を実行します。

    Args:
        argv (list[str] | None, optional): コマンドライン引数リスト。None の場合は sys.argv[1:] を使用。
    """
    raw_args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        description="Show local Codex, Claude Code, and Antigravity usage in a browser."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4202)
    parser.add_argument("--codex-home", type=Path, default=default_codex_home())
    parser.add_argument("--state-home", type=Path, default=default_state_home())
    parser.add_argument("--claude-home", type=Path, default=default_claude_home())
    parser.add_argument("--agy-home", type=Path, default=default_agy_home())
    parser.add_argument("--save-claude-usage", action="store_true")
    parser.add_argument("--save-agy-usage", action="store_true")
    parser.add_argument("--save-monthly-settings", action="store_true")
    parser.add_argument("--billing-day", type=int)
    parser.add_argument("--monthly-budget-codex", type=float)
    parser.add_argument("--monthly-tokens-codex", type=int)
    parser.add_argument("--monthly-budget-claude", type=float)
    parser.add_argument("--monthly-tokens-claude", type=int)
    parser.add_argument("--monthly-budget-agy", type=float)
    parser.add_argument("--monthly-tokens-agy", type=int)
    parser.add_argument("--monthly-budget-total", type=float)
    parser.add_argument("--monthly-tokens-total", type=int)
    parser.add_argument("--agy-used-percent", type=int)
    parser.add_argument("--agy-resets-at", type=int)
    parser.add_argument("--agy-window-minutes", type=int, default=10080)
    parser.add_argument("--agy-gemini-5h-used", type=int)
    parser.add_argument("--agy-gemini-5h-remaining", type=int)
    parser.add_argument("--agy-gemini-5h-resets-at", type=int)
    parser.add_argument("--agy-gemini-7d-used", type=int)
    parser.add_argument("--agy-gemini-7d-remaining", type=int)
    parser.add_argument("--agy-gemini-7d-resets-at", type=int)
    parser.add_argument("--agy-claude-5h-used", type=int)
    parser.add_argument("--agy-claude-5h-remaining", type=int)
    parser.add_argument("--agy-claude-5h-resets-at", type=int)
    parser.add_argument("--agy-claude-7d-used", type=int)
    parser.add_argument("--agy-claude-7d-remaining", type=int)
    parser.add_argument("--agy-claude-7d-resets-at", type=int)
    parser.add_argument("--agy-argon-7d-used", type=int)
    parser.add_argument("--agy-argon-7d-remaining", type=int)
    parser.add_argument("--agy-argon-7d-resets-at", type=int)
    parser.add_argument("--claude-primary-used-percent", type=int)
    parser.add_argument("--claude-primary-resets-at", type=int)
    parser.add_argument("--claude-secondary-used-percent", type=int)
    parser.add_argument("--claude-secondary-resets-at", type=int)
    parser.add_argument(
        "-b", "--background", action="store_true", help="run the server in the background"
    )
    parser.add_argument("--no-open", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(raw_args)
    if args.save_claude_usage:
        values = (
            args.claude_primary_used_percent,
            args.claude_primary_resets_at,
            args.claude_secondary_used_percent,
            args.claude_secondary_resets_at,
        )
        if any(value is None for value in values):
            parser.error("--save-claude-usage requires both usage percentages and reset timestamps")
        save_claude_usage(
            args.state_home,
            primary_used_percent=args.claude_primary_used_percent,
            primary_resets_at=args.claude_primary_resets_at,
            secondary_used_percent=args.claude_secondary_used_percent,
            secondary_resets_at=args.claude_secondary_resets_at,
        )
        print(f"Claude usage saved: {claude_usage_path(args.state_home)}")
        return
    if args.save_agy_usage:

        def _calc(used: int | None, rem: int | None) -> int | None:
            if used is not None:
                return used
            if rem is not None:
                return max(0, min(100, 100 - rem))
            return None

        g5 = _calc(args.agy_gemini_5h_used, args.agy_gemini_5h_remaining)
        g7 = _calc(args.agy_gemini_7d_used, args.agy_gemini_7d_remaining)
        c5 = _calc(args.agy_claude_5h_used, args.agy_claude_5h_remaining)
        c7 = _calc(args.agy_claude_7d_used, args.agy_claude_7d_remaining)
        a7 = _calc(args.agy_argon_7d_used, args.agy_argon_7d_remaining)

        has_any = any(v is not None for v in (g5, g7, c5, c7, a7, args.agy_used_percent))
        if not has_any:
            parser.error("--save-agy-usage requires at least one usage or remaining percentage")

        save_agy_usage(
            args.state_home,
            gemini_5h_used=g5,
            gemini_5h_resets_at=args.agy_gemini_5h_resets_at,
            gemini_7d_used=g7,
            gemini_7d_resets_at=args.agy_gemini_7d_resets_at,
            claude_5h_used=c5,
            claude_5h_resets_at=args.agy_claude_5h_resets_at,
            claude_7d_used=c7,
            claude_7d_resets_at=args.agy_claude_7d_resets_at,
            argon_7d_used=a7,
            argon_7d_resets_at=args.agy_argon_7d_resets_at,
            used_percent=args.agy_used_percent,
            resets_at=args.agy_resets_at,
            window_minutes=args.agy_window_minutes,
        )
        print(f"AGY usage saved: {agy_usage_path(args.state_home)}")
        return
    if args.save_monthly_settings:
        settings_payload: dict[str, Any] = {}
        if args.billing_day is not None:
            settings_payload["billing_day"] = args.billing_day
        codex_cfg: dict[str, Any] = {}
        if args.monthly_budget_codex is not None:
            codex_cfg["budget_usd"] = args.monthly_budget_codex
        if args.monthly_tokens_codex is not None:
            codex_cfg["token_limit"] = args.monthly_tokens_codex
        if codex_cfg:
            settings_payload["codex"] = codex_cfg

        claude_cfg: dict[str, Any] = {}
        if args.monthly_budget_claude is not None:
            claude_cfg["budget_usd"] = args.monthly_budget_claude
        if args.monthly_tokens_claude is not None:
            claude_cfg["token_limit"] = args.monthly_tokens_claude
        if claude_cfg:
            settings_payload["claude"] = claude_cfg

        agy_cfg: dict[str, Any] = {}
        if args.monthly_budget_agy is not None:
            agy_cfg["budget_usd"] = args.monthly_budget_agy
        if args.monthly_tokens_agy is not None:
            agy_cfg["token_limit"] = args.monthly_tokens_agy
        if agy_cfg:
            settings_payload["agy"] = agy_cfg

        if args.monthly_budget_total is not None:
            settings_payload["total_budget_usd"] = args.monthly_budget_total
        if args.monthly_tokens_total is not None:
            settings_payload["total_token_limit"] = args.monthly_tokens_total

        save_monthly_settings(args.state_home, settings_payload)
        print(f"Monthly settings saved: {monthly_settings_path(args.state_home)}")
        return
    if args.host != "127.0.0.1":
        parser.error("--host must be 127.0.0.1; this dashboard is local-only")
    url = f"http://{args.host}:{args.port}"
    if args.background:
        subprocess.Popen(background_command(raw_args), **detached_process_options())  # nosec B603 # noqa: S603
        open_dashboard(url)
        print(f"AI Vitals started in background: {url}")
        return
    server = create_server(
        args.host, args.port, args.codex_home, args.state_home, args.claude_home, args.agy_home
    )
    print(f"AI Vitals: http://{args.host}:{server.server_port}")
    if not args.no_open:
        open_dashboard(f"http://{args.host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
