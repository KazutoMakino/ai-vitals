"""Local, dependency-free Codex, Claude Code, and Antigravity usage dashboard."""

from __future__ import annotations

import argparse
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

PRICES_PER_MILLION = {
    "gpt-5.6-terra": (2.0, 0.2, 12.0),
    "gpt-5.5": (1.75, 0.175, 14.0),
    "gpt-5.4": (1.25, 0.125, 10.0),
    "gpt-5.4-mini": (0.25, 0.025, 2.0),
    "gpt-5": (1.25, 0.125, 10.0),
}
CLAUDE_PRICES_PER_MILLION = {
    "claude-sonnet-5": (2.0, 2.5, 4.0, 0.2, 10.0),
    "claude-sonnet-4.6": (3.0, 3.75, 6.0, 0.3, 15.0),
    "claude-sonnet-4.5": (3.0, 3.75, 6.0, 0.3, 15.0),
    "claude-haiku-4.5": (1.0, 1.25, 2.0, 0.1, 5.0),
}
GEMINI_PRICES_PER_MILLION = {
    "gemini": (0.15, 0.60),
    "flash": (0.15, 0.60),
    "pro": (1.25, 5.00),
    "opus": (15.0, 75.0),
    "sonnet": (3.0, 15.0),
}


def classify_agy_model(model_name: str | None) -> str:
    """Return 'gemini' or 'claude_gpt' based on model name."""
    name = (model_name or "").lower()
    if any(k in name for k in ("claude", "gpt", "sonnet", "opus")):
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
    """Extract the latest usage and safe task metadata from one JSONL file."""
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
    """Return a stable empty shape when Claude Code has no local sessions."""
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
    """Sum Claude Code assistant-token records from a single local JSONL session."""
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
    """Return a stable empty shape when Antigravity has no local sessions."""
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
        },
        "limits": {
            "gemini": {"primary": None, "secondary": None},
            "claude_gpt": {"primary": None, "secondary": None},
            "rate_limit": None,
        },
        "task": {"cwd": None, "prompt": None},
        "estimated_cost_usd": None,
    }


AGY_LIVE_CACHE: tuple[float, dict[str, Any] | None] | None = None


def detect_antigravity_live_status(timeout_sec: float = 1.5) -> dict[str, Any] | None:
    """Connect to running Antigravity language_server to fetch official real-time quota status."""
    pgrep_bin = shutil.which("pgrep")
    if not pgrep_bin:
        return None
    try:
        out = subprocess.check_output(  # nosec B603
            [pgrep_bin, "-af", "language_server"]
        ).decode("utf-8", "ignore")
    except Exception:
        return None
    token = None
    pid = None
    for line in out.splitlines():
        if "csrf_token" in line:
            parts = line.split()
            if parts:
                pid = parts[0]
            m = re.search(r"--csrf_token\s+([a-zA-Z0-9-]+)", line)
            if m:
                token = m.group(1)
            break
    if not token or not pid:
        return None

    ss_bin = shutil.which("ss")
    if not ss_bin:
        return None
    try:
        ss_out = subprocess.check_output(  # nosec B603
            [ss_bin, "-tulpn"]
        ).decode("utf-8", "ignore")
    except Exception:
        return None
    ports: list[int] = []
    for line in ss_out.splitlines():
        if f"pid={pid}," in line and "127.0.0.1:" in line:
            m = re.search(r"127\.0\.0\.1:(\d+)", line)
            if m:
                ports.append(int(m.group(1)))
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
                    for g in groups:
                        disp = (g.get("displayName") or "").lower()
                        buckets = g.get("buckets", [])
                        target = None
                        if "gemini" in disp:
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
                    ):
                        return {
                            "source": "live_api",
                            "plan": "Google AI Pro",
                            "gemini": gem_res,
                            "claude_gpt": cld_res,
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
                now_ts = time.time()
                for c in configs:
                    lbl = (c.get("label") or "").lower()
                    q = c.get("quotaInfo")
                    if not q:
                        continue
                    parsed = _parse_bucket(q)
                    target = None
                    if any(x in lbl for x in ("claude", "gpt", "opus", "sonnet")):
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
                ):
                    return {
                        "source": "live_api",
                        "plan": plan_name,
                        "gemini": gem_res,
                        "claude_gpt": cld_res,
                    }
            except Exception:  # nosec B110
                pass
    return None


def get_cached_agy_live_status(max_age_sec: float = 5.0) -> dict[str, Any] | None:
    """Return cached live status from Antigravity language_server if recent."""
    global AGY_LIVE_CACHE
    now = time.time()
    if AGY_LIVE_CACHE is not None and (now - AGY_LIVE_CACHE[0]) < max_age_sec:
        return AGY_LIVE_CACHE[1]
    status = detect_antigravity_live_status()
    AGY_LIVE_CACHE = (now, status)
    return status


def agy_usage_cost(model: Any, usage: dict[str, int]) -> float | None:
    """Estimate API-equivalent cost for an Antigravity session."""
    model_str = str(model or "").lower()
    in_tok = usage.get("input_tokens", 0)
    out_tok = usage.get("output_tokens", 0)
    if in_tok == 0 and out_tok == 0:
        return None
    price = (0.15, 0.60)
    for key, val in GEMINI_PRICES_PER_MILLION.items():
        if key in model_str:
            price = val
            break
    in_price, out_price = price
    cost = (in_tok * in_price + out_tok * out_price) / 1_000_000
    return round(cost, 4)


def summarize_agy_session(brain_dir: Path) -> dict[str, Any] | None:
    """Extract session info from one Antigravity brain transcript.

    Args:
        brain_dir: Path to one brain conversation directory
            (e.g. ~/.gemini/antigravity/brain/<uuid>).

    Returns:
        A summary dict, or None if no usable records are found.
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
            if fam == "gemini":
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
    summary["usage"]["steps_5h"] = gem_5h + cld_5h
    summary["usage"]["steps_7d"] = gem_7d + cld_7d
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
    in_tok = int(input_chars / 3.8)
    out_tok = int(output_chars / 3.8)
    summary["usage"]["input_tokens"] = in_tok
    summary["usage"]["output_tokens"] = out_tok
    summary["usage"]["total_tokens"] = in_tok + out_tok
    summary["estimated_cost_usd"] = agy_usage_cost(summary["model"], summary["usage"])
    return summary


def _extract_agy_user_prompt(content: str) -> str | None:
    """Extract the genuine user request from an AGY USER_INPUT record.

    Args:
        content: Raw content string from the transcript record.

    Returns:
        First meaningful line of the user's request, or None.
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
    """Return the local-only location for manually captured /usage values."""
    return state_home / "ai-vitals" / "claude-usage.json"


def agy_usage_path(state_home: Path) -> Path:
    """Return the local-only location for manually captured AGY usage values."""
    return state_home / "ai-vitals" / "agy-usage.json"


def load_claude_usage(state_home: Path) -> dict[str, Any]:
    """Read the optional, credential-free snapshot captured from Claude /usage."""
    empty = {"primary": None, "secondary": None}
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
    }


def load_agy_usage(state_home: Path) -> dict[str, Any]:
    """Read the optional, credential-free snapshot captured from AGY /usage."""
    empty = {
        "gemini": {"primary": None, "secondary": None},
        "claude_gpt": {"primary": None, "secondary": None},
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
    if isinstance(gemini_raw, dict) or isinstance(claude_raw, dict):
        gem_pri = normalize_limit((gemini_raw or {}).get("primary"))
        gem_sec = normalize_limit((gemini_raw or {}).get("secondary"))
        cld_pri = normalize_limit((claude_raw or {}).get("primary"))
        cld_sec = normalize_limit((claude_raw or {}).get("secondary"))
        return {
            "gemini": {"primary": gem_pri, "secondary": gem_sec},
            "claude_gpt": {"primary": cld_pri, "secondary": cld_sec},
            "rate_limit": gem_sec or normalize_limit(value.get("rate_limit")),
        }

    legacy_limit = normalize_limit(value.get("rate_limit"))
    return {
        "gemini": {"primary": None, "secondary": legacy_limit},
        "claude_gpt": {"primary": None, "secondary": None},
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
    """Store manually observed /usage values without retaining auth material."""
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
    used_percent: int | None = None,
    resets_at: int | None = None,
    window_minutes: int = 10080,
) -> None:
    """Store manually observed AGY usage values without retaining auth material."""
    existing = load_agy_usage(state_home)
    gem_pri = dict(existing["gemini"]["primary"]) if existing["gemini"]["primary"] else {}
    gem_sec = dict(existing["gemini"]["secondary"]) if existing["gemini"]["secondary"] else {}
    cld_pri = dict(existing["claude_gpt"]["primary"]) if existing["claude_gpt"]["primary"] else {}
    cld_sec = (
        dict(existing["claude_gpt"]["secondary"]) if existing["claude_gpt"]["secondary"] else {}
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

    data = {
        "gemini": {
            "primary": gem_pri or None,
            "secondary": gem_sec or None,
        },
        "claude_gpt": {
            "primary": cld_pri or None,
            "secondary": cld_sec or None,
        },
        "rate_limit": gem_sec or None,
    }
    path = agy_usage_path(state_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def format_session_snapshot(session: dict[str, Any], provider: str = "Codex") -> str:
    """Format a session summary into a readable snapshot block for the history panel."""
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
    """Return the latest session, recent tasks, and watch-log history."""
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
    claude["limits"] = load_claude_usage(state_home)
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
    gem_5h = sum(s["usage"].get("gemini", {}).get("steps_5h", 0) for s in agy_sessions)
    gem_7d = sum(s["usage"].get("gemini", {}).get("steps_7d", 0) for s in agy_sessions)
    gem_steps = sum(s["usage"].get("gemini", {}).get("steps", 0) for s in agy_sessions)

    cld_5h = sum(s["usage"].get("claude_gpt", {}).get("steps_5h", 0) for s in agy_sessions)
    cld_7d = sum(s["usage"].get("claude_gpt", {}).get("steps_7d", 0) for s in agy_sessions)
    cld_steps = sum(s["usage"].get("claude_gpt", {}).get("steps", 0) for s in agy_sessions)

    gem_tokens = sum(
        s["usage"]["total_tokens"] for s in agy_sessions if s.get("model_family") == "gemini"
    )
    cld_tokens = sum(
        s["usage"]["total_tokens"] for s in agy_sessions if s.get("model_family") == "claude_gpt"
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
    }


def openai_status() -> dict[str, Any]:
    """Read official current status and the visible 90-day incident history."""
    global STATUS_CACHE
    STATUS_CACHE = cached_status(STATUS_CACHE, STATUS_URL, INCIDENTS_URL, ("ChatGPT", "Codex"))
    return STATUS_CACHE[1]


def claude_status() -> dict[str, Any]:
    """Read the official Claude Code status and incident history."""
    global CLAUDE_STATUS_CACHE
    CLAUDE_STATUS_CACHE = cached_status(
        CLAUDE_STATUS_CACHE, CLAUDE_STATUS_URL, CLAUDE_INCIDENTS_URL, ("Claude Code",)
    )
    return CLAUDE_STATUS_CACHE[1]


def google_status() -> dict[str, Any]:
    """Read public Google Cloud / Gemini status and 90-day incident history."""
    global GOOGLE_STATUS_CACHE
    GOOGLE_STATUS_CACHE = cached_google_status(GOOGLE_STATUS_CACHE, GOOGLE_STATUS_URL)
    return GOOGLE_STATUS_CACHE[1]


def cached_google_status(
    cache: tuple[float, dict[str, Any]] | None,
    incidents_url: str,
) -> tuple[float, dict[str, Any]]:
    """Fetch official Google Cloud status with a short local cache."""
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
    """Fetch one official status provider with a short local cache."""
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
    """Summarize public ChatGPT and Codex incidents into 90 calendar-day bars."""
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
    """Combine the public incident text used to identify its product."""
    updates = incident.get("incident_updates")
    bodies = [
        item.get("body", "")
        for item in (updates if isinstance(updates, list) else [])
        if isinstance(item, dict)
    ]
    return " ".join([str(incident.get("name", "")), *map(str, bodies)]).lower()


def incident_date(value: Any) -> date | None:
    """Parse an ISO-8601 incident timestamp without trusting malformed data."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).date()
    except ValueError:
        return None


def incident_severity(impact: Any) -> str:
    """Map the status API impact to the compact bar palette."""
    return "bad" if impact in {"major", "critical"} else "warn"


def first_user_prompt(payload: dict[str, Any]) -> str | None:
    """Return the first line of a user message without retaining its body."""
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
    """Extract the first safe line from one Claude Code user record."""
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    payload = {"type": "message", "role": "user", "content": content}
    return first_user_prompt(payload)


def injected_context_line(line: str) -> bool:
    """Exclude known instruction wrappers from the task title."""
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
    """Normalize Codex token usage fields."""
    return {
        "input_tokens": number(raw.get("input_tokens")),
        "cached_input_tokens": number(raw.get("cached_input_tokens")),
        "output_tokens": number(raw.get("output_tokens")),
        "total_tokens": number(raw.get("total_tokens")),
    }


def normalize_limit(raw: Any) -> dict[str, Any] | None:
    """Normalize one Codex rate-limit window."""
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
    """Give a compact display label for a limit window."""
    if minutes == 300:
        return "5h"
    if minutes == 10080:
        return "7d"
    return f"{minutes}m" if minutes else "limit"


def number(value: Any) -> int:
    """Convert a JSON number to a non-negative integer."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def rounded(value: Any) -> int:
    """Round a JSON number like JavaScript's Math.round for non-negative values."""
    try:
        return max(0, int(float(value) + 0.5))
    except (TypeError, ValueError):
        return 0


def percent(used: int, capacity: int) -> int | None:
    """Return rounded usage percent when a context capacity exists."""
    return rounded(used * 100 / capacity) if capacity else None


def default_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))


def default_state_home(platform: str | None = None) -> Path:
    if "XDG_STATE_HOME" in os.environ:
        return Path(os.environ["XDG_STATE_HOME"])
    current_platform = platform or sys.platform
    if current_platform == "win32" and "LOCALAPPDATA" in os.environ:
        return Path(os.environ["LOCALAPPDATA"])
    return Path.home() / ".local" / "state"


def default_claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_HOME", Path.home() / ".claude"))


def default_agy_home() -> Path:
    """Return the default Antigravity data directory."""
    return Path(os.environ.get("ANTIGRAVITY_HOME", Path.home() / ".gemini" / "antigravity"))


def usage_cost(model: Any, usage: dict[str, int]) -> float | None:
    """Estimate API-equivalent cost for one token-count event."""
    model_id = str(model or "").lower()
    price = next(
        (
            value
            for key, value in PRICES_PER_MILLION.items()
            if model_id == key or model_id.startswith(key + "-")
        ),
        None,
    )
    if price is None:
        return None
    input_price, cached_price, output_price = price
    cached = min(usage["input_tokens"], usage["cached_input_tokens"])
    uncached = usage["input_tokens"] - cached
    return (
        uncached * input_price + cached * cached_price + usage["output_tokens"] * output_price
    ) / 1_000_000


def claude_usage_cost(model: Any, usage: dict[str, int]) -> dict[str, float] | None:
    """Return an API-equivalent cost range when cache-creation TTL is unavailable."""
    model_id = str(model or "").lower()
    price = next(
        (value for key, value in CLAUDE_PRICES_PER_MILLION.items() if model_id.startswith(key)),
        None,
    )
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
    """Serve the dashboard and current JSON snapshot on one loopback server."""
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
                        used_percent=legacy_used,
                        resets_at=int(payload["resets_at"]) if payload.get("resets_at") else None,
                        window_minutes=int(payload.get("window_minutes") or 10080),
                    )
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
    """Open the dashboard URL through the configured system browser."""
    browser(url)


def detached_process_options(platform: str | None = None) -> dict[str, Any]:
    """Return stdlib process options that detach on Linux and Windows."""
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
    """Return the child command without foreground-only flags."""
    child_args = [arg for arg in argv if arg not in {"-b", "--background", "--no-open"}]
    return [sys.executable, str(Path(__file__).resolve()), *child_args, "--no-open"]


def legacy_dashboard_html() -> str:
    """Return the self-contained, information-dense dashboard shell."""
    return """<!doctype html>
<html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Codex Claude Vitals</title>
<style>
:root{color-scheme:dark;--bg:#0b1220;--panel:#121d30;--line:#253651;--text:#e7eefb;--muted:#91a1bb;--mint:#72dfb5;--blue:#7cb7ff;--warn:#ffce6a;--bad:#ff7a7a}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px ui-sans-serif,system-ui,sans-serif}main{max-width:1280px;margin:auto;padding:20px}header{display:flex;justify-content:space-between;gap:12px;align-items:end;margin-bottom:16px}h1{font-size:20px;margin:0}h1 span{color:var(--mint)}.muted{color:var(--muted);font-size:12px}.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.card,.panel{border:1px solid var(--line);border-radius:12px;background:var(--panel);padding:14px}.label{color:var(--muted);font-size:12px}.value{font-size:22px;font-weight:700;margin:7px 0}.bar{height:6px;background:#24314a;border-radius:999px;overflow:hidden;margin-top:8px}.bar i{display:block;height:100%;background:var(--mint)}.grid{display:grid;grid-template-columns:1.15fr .85fr;gap:10px;margin-top:10px}.panel h2{font-size:14px;margin:0 0 10px}.task{padding:10px 0;border-top:1px solid var(--line)}.task:first-of-type{border-top:0}.task-head{display:grid;grid-template-columns:70px minmax(0,1fr) 155px;gap:8px;align-items:baseline}.task-head b{min-width:0}.task-index,.task-time{color:var(--muted);font-size:11px;white-space:nowrap}.task-time{text-align:right}.task b{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.task span{display:block;color:var(--muted);margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}details{border-top:1px solid var(--line);padding:8px 0}pre{white-space:pre-wrap;word-break:break-word;color:var(--muted);font-size:11px;margin:8px 0 0}#history{max-height:180px;overflow-y:auto}.status-group{border-top:1px solid var(--line);padding:12px 0 4px}.status-head{display:flex;justify-content:space-between;gap:8px;align-items:baseline}.status-name{font-weight:700}.status-bars{display:flex;gap:2px;margin:8px 0}.status-day{height:13px;flex:1;border-radius:1px;background:var(--mint)}.status-day.warn{background:var(--warn)}.status-day.bad{background:var(--bad)}.status-group details{font-size:12px;color:var(--muted)}@media(max-width:700px){.cards{grid-template-columns:repeat(2,1fr)}.grid{grid-template-columns:1fr}.task-head{grid-template-columns:62px minmax(0,1fr)}.task-time{grid-column:2;text-align:left}}
</style>
<main><header><div><h1><span>CODEX · CLAUDE</span> VITALS</h1><div class="muted">ローカル専用 · API相当の推定値</div></div><div id="updated" class="muted">読み込み中</div></header>
<section class="cards"><article class="card"><div class="label">5h LIMIT</div><div class="value" id="primary">—</div><div class="muted" id="primary-reset">解除: —</div><div class="bar"><i id="primary-bar"></i></div></article><article class="card"><div class="label">7d LIMIT</div><div class="value" id="secondary">—</div><div class="muted" id="secondary-reset">解除: —</div><div class="bar"><i id="secondary-bar"></i></div></article><article class="card"><div class="label">CONTEXT</div><div class="value" id="context">—</div><div class="muted" id="context-detail">—</div><div class="bar"><i id="context-bar"></i></div></article><article class="card"><div class="label">API EQUIVALENT</div><div class="value" id="cost">—</div><div class="muted" id="model">—</div></article></section>
<section class="panel" style="margin-top:10px"><h2>Claude Code</h2><section class="cards"><article class="card"><div class="label">SESSION TOKENS</div><div class="value" id="claude-total">—</div><div class="muted" id="claude-model">—</div></article><article class="card"><div class="label">INPUT / OUTPUT</div><div class="value" id="claude-io">—</div><div class="muted" id="claude-cache">—</div></article><article class="card"><div class="label">5h PLAN LIMIT</div><div class="value" id="claude-primary">—</div><div class="muted" id="claude-primary-reset">/usageを保存してください</div><div class="bar"><i id="claude-primary-bar"></i></div></article><article class="card"><div class="label">7d PLAN LIMIT</div><div class="value" id="claude-secondary">—</div><div class="muted" id="claude-secondary-reset">/usageを保存してください</div><div class="bar"><i id="claude-secondary-bar"></i></div></article></section></section>
<section class="panel" style="margin-top:10px"><h2>OpenAI Status</h2><div id="status" class="muted">取得中</div><div id="status-groups"></div></section>
<section class="grid"><article class="panel"><h2>Active tasks</h2><div id="tasks" class="muted">データなし</div></article><article class="panel"><h2>Recent snapshots</h2><div id="history" class="muted">履歴なし</div></article></section></main>
<script>
const $=id=>document.getElementById(id);const text=(id,value)=>$(id).textContent=value??'—';const tokens=value=>value>=1000?(value/1000).toFixed(1)+'k':String(value??0);const resetAt=value=>value?new Date(value*1000).toLocaleString('ja-JP',{timeZone:'Asia/Tokyo'}):'—';const indicatorColor=value=>value>=80?'#ff7a7a':value>=60?'#ffce6a':'#72dfb5';
function paintBar(name,value){const bar=$(name+'-bar');const color=indicatorColor(value??0);bar.style.width=(value??0)+'%';bar.style.background=color;bar.style.boxShadow='0 0 12px '+color}
function limit(name,data){const used=data?.used_percent;text(name,used==null?'—':`${used}%`);text(name+'-reset','解除: '+resetAt(data?.resets_at));paintBar(name,used)}
function renderStatus(status){text('status',status?.state||'取得不可');const groups=$('status-groups');groups.replaceChildren(...(status?.groups||[]).map(group=>{const row=document.createElement('section');row.className='status-group';const head=document.createElement('div');head.className='status-head';const name=document.createElement('span');name.className='status-name';name.textContent=`● ${group.name}`;const meta=document.createElement('span');meta.className='muted';meta.textContent=`正常日割合 ${group.normal_percent}%`;head.append(name,meta);const bars=document.createElement('div');bars.className='status-bars';bars.setAttribute('aria-label',`${group.name} の90日インシデント履歴`);(group.days||[]).forEach((state,index)=>{const day=document.createElement('i');day.className='status-day '+state;day.title=`${89-index}日前: ${state==='ok'?'問題なし':state==='warn'?'軽微な障害':'重大な障害'}`;bars.append(day)});const details=document.createElement('details');const summary=document.createElement('summary');summary.textContent=`構成要素 ${group.components?.length||0} 件（${group.state}）`;details.append(summary,...(group.components||[]).map(component=>{const item=document.createElement('div');item.textContent=`${component.name}: ${component.state}`;return item}));row.append(head,bars,details);return row}));if(!groups.childNodes.length)groups.textContent='グループ情報を取得できませんでした'}
function render(data){const current=data.current||{};limit('primary',current.limits?.primary);limit('secondary',current.limits?.secondary);const context=current.context||{};const used=context.used_percent;text('context',used==null?'—':used+'%');text('context-detail',`${tokens(context.used_tokens)} / ${tokens(context.window_tokens)} tokens`);paintBar('context',used);text('cost',current.estimated_cost_usd==null?'見積不可':'$'+current.estimated_cost_usd.toFixed(4));text('model',[current.model,current.effort].filter(Boolean).join(' · '));const claude=data.claude||{};const claudeUsage=claude.usage||{};text('claude-total',tokens(claudeUsage.total_tokens));text('claude-model',[claude.model,claude.observed_at&&new Date(claude.observed_at).toLocaleString('ja-JP')].filter(Boolean).join(' · ')||'セッション待機中');text('claude-io',`${tokens(claudeUsage.input_tokens)} / ${tokens(claudeUsage.output_tokens)}`);text('claude-cache',`cache ${tokens((claudeUsage.cache_creation_input_tokens||0)+(claudeUsage.cache_read_input_tokens||0))}`);limit('claude-primary',claude.limits?.primary);limit('claude-secondary',claude.limits?.secondary);renderStatus(data.status);text('updated',current.observed_at?'更新 '+new Date(current.observed_at).toLocaleString('ja-JP'):'セッション待機中');const tasks=$('tasks');tasks.replaceChildren(...(data.tasks||[]).map((task,index)=>{const row=document.createElement('div');row.className='task';const head=document.createElement('div');head.className='task-head';const order=document.createElement('span');order.className='task-index';order.textContent=`#${index+1} · ${index}件前`;const title=document.createElement('b');title.textContent=task.task?.prompt||'プロンプト未取得';const observed=document.createElement('time');observed.className='task-time';observed.textContent=task.observed_at?new Date(task.observed_at).toLocaleString('ja-JP',{timeZone:'Asia/Tokyo'}):'日時不明';head.append(order,title,observed);const meta=document.createElement('span');meta.textContent=[task.task?.cwd,task.model].filter(Boolean).join(' · ');row.append(head,meta);return row}));if(!tasks.childNodes.length)tasks.textContent='データなし';const history=$('history');history.replaceChildren(...(data.history||[]).reverse().map(item=>{const detail=document.createElement('details');const title=document.createElement('summary');title.textContent=item.split('\\n')[0]||'snapshot';const pre=document.createElement('pre');pre.textContent=item;detail.append(title,pre);return detail}));if(!history.childNodes.length)history.textContent='履歴なし'}
async function loadSnapshot(){try{render(await (await fetch('/api/snapshot',{cache:'no-store'})).json())}catch(error){text('updated','取得エラー: '+error.message)}}loadSnapshot();setInterval(loadSnapshot,15000);
</script></html>"""


def dashboard_html() -> str:
    """Return the customizable dashboard shell."""
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
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}main{max-width:1280px;margin:auto;padding:0 20px 32px}header{position:sticky;top:0;z-index:50;display:flex;justify-content:space-between;gap:12px;align-items:center;padding:14px 0;margin-bottom:18px;background:color-mix(in srgb,var(--bg) 82%,transparent);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border-bottom:1px solid color-mix(in srgb,var(--line) 45%,transparent)}h1{font-size:18px;font-weight:700;letter-spacing:-0.02em;margin:0;display:flex;align-items:center;gap:4px}h1 span{color:var(--mint);background:linear-gradient(135deg,var(--mint),var(--blue));-webkit-background-clip:text;-webkit-text-fill-color:transparent}button,summary,select{cursor:pointer;font-family:inherit}.muted{color:var(--muted);font-size:12px}summary::-webkit-details-marker{display:none}details>summary{list-style:none}.btn-small,.layout-button,#shutdown{display:inline-flex;align-items:center;justify-content:center;gap:5px;height:30px;padding:0 12px;font-size:11px;font-weight:600;color:var(--text);background:color-mix(in srgb,var(--badge) 80%,transparent);border:1px solid color-mix(in srgb,var(--line) 85%,transparent);border-radius:7px;cursor:pointer;white-space:nowrap;transition:all .18s cubic-bezier(0.16,1,0.3,1);text-decoration:none;box-sizing:border-box;margin:0;backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px)}.btn-small:hover,.layout-button:hover{background:var(--panel);border-color:color-mix(in srgb,var(--mint) 60%,var(--line));color:var(--mint);transform:translateY(-1px);box-shadow:0 2px 8px #0002}#shutdown{color:#ffd7d7;background:#351923cc;border-color:var(--bad)}#shutdown:hover{background:#572332;border-color:var(--bad);color:#fff;box-shadow:0 2px 10px rgba(255,122,122,0.25);transform:translateY(-1px)}#shutdown:focus-visible{outline:2px solid #fff;outline-offset:2px}#shutdown:disabled{cursor:default;opacity:.55;transform:none}#notify-toggle.active{border-color:var(--mint);color:var(--mint);background:color-mix(in srgb,var(--mint) 12%,var(--badge))}#theme-select{display:inline-flex;align-items:center;height:28px;padding:0 8px;font-size:11px;font-weight:600;background:var(--badge);border:1px solid var(--line);color:var(--text);border-radius:6px;cursor:pointer;transition:all .15s ease;margin:0}#theme-select:hover{border-color:var(--mint)}#updated{margin:0 0 0 4px;font-variant-numeric:tabular-nums}.settings{position:absolute;right:0;z-index:100;min-width:256px;max-height:85vh;overflow-y:auto;background:color-mix(in srgb,var(--panel) 88%,transparent);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);border:1px solid color-mix(in srgb,var(--line) 90%,rgba(255,255,255,0.08));border-radius:10px;padding:14px;box-shadow:0 16px 36px rgba(0,0,0,0.35),0 0 0 1px rgba(255,255,255,0.04);animation:popoverIn .15s ease-out}@keyframes popoverIn{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:translateY(0)}}.settings label{display:block;padding:4px 0;font-size:12px}.settings select{width:100%;height:30px;padding:0 8px;font-size:11px;font-weight:500;background:var(--badge);border:1px solid var(--line);color:var(--text);border-radius:6px;cursor:pointer;margin-top:3px;transition:border-color .15s}.settings select:hover{border-color:var(--mint)}.settings select:focus{outline:none;border-color:var(--mint);box-shadow:0 0 0 2px color-mix(in srgb,var(--mint) 25%,transparent)}.settings::-webkit-scrollbar{width:5px}.settings::-webkit-scrollbar-thumb{background:var(--line);border-radius:3px}.settings button{margin-top:8px}.layout-item{display:flex;align-items:center;justify-content:space-between;gap:6px;padding:3px 0;border-radius:4px}.layout-item label{display:flex;align-items:center;gap:6px;margin:0;padding:0;cursor:pointer;font-size:12px;user-select:none;flex:1}.item-btns{display:inline-flex;gap:2px}.btn-order{background:var(--badge);border:1px solid var(--line);color:var(--text);border-radius:4px;padding:1px 6px;font-size:11px;font-weight:700;cursor:pointer;line-height:1.2;transition:all .1s}.btn-order:hover:not(:disabled){background:var(--panel);border-color:var(--mint);color:var(--mint)}.btn-order:disabled{opacity:.2;cursor:default}.blocks{display:grid;gap:12px}.block{border:1px solid var(--line);border-radius:14px;background:var(--panel);padding:16px;box-shadow:0 1px 3px rgba(0,0,0,0.08),0 4px 12px rgba(0,0,0,0.03);transition:border-color .15s ease}.block:hover{border-color:color-mix(in srgb,var(--line) 75%,var(--mint))}.block[hidden]{display:none}.block.dragging{opacity:.4;transform:scale(0.995)}.block-title{display:flex;align-items:center;gap:8px;margin:0;font-size:14px;font-weight:600;letter-spacing:-0.01em}.handle{color:var(--muted);cursor:grab;user-select:none;transition:color .15s}.handle:hover{color:var(--mint)}.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.card{border:1px solid var(--line);border-radius:10px;padding:13px;background:var(--card);transition:transform .18s cubic-bezier(0.16,1,0.3,1),box-shadow .18s cubic-bezier(0.16,1,0.3,1),border-color .18s ease}.card:hover{transform:translateY(-1px);border-color:color-mix(in srgb,var(--mint) 35%,var(--line));box-shadow:0 6px 18px rgba(0,0,0,0.12)}.label{color:var(--muted);font-size:11px;font-weight:600;letter-spacing:0.04em;text-transform:uppercase}.value{font-size:24px;font-weight:700;margin:6px 0;letter-spacing:-0.03em;font-feature-settings:"tnum";font-variant-numeric:tabular-nums}.bar{height:5px;background:var(--bar-bg);border-radius:999px;overflow:hidden;margin-top:8px}.bar i{display:block;height:100%;background:var(--mint);transition:width .4s cubic-bezier(0.16,1,0.3,1),background-color .2s ease}.plan-badge{display:inline-flex;align-items:center;gap:6px;cursor:pointer;padding:3px 9px;border-radius:6px;background:var(--badge);border:1px solid var(--line);font-size:12px;color:var(--text);transition:all .15s;font-variant-numeric:tabular-nums}.plan-badge:hover{background:var(--panel);border-color:var(--mint);transform:translateY(-0.5px)}.plan-badge i{font-style:normal;font-size:11px;color:var(--mint);text-decoration:none;font-weight:600}.service-badge{display:inline-flex;align-items:center;gap:6px;font-size:11px;color:var(--muted);background:var(--badge);border:1px solid var(--line);border-radius:6px;padding:3px 9px;font-variant-numeric:tabular-nums}.task{padding:10px 0;border-top:1px solid var(--line)}.task:first-of-type{border-top:0}.task-head{display:grid;grid-template-columns:70px minmax(0,1fr) 155px;gap:8px;align-items:baseline}.task-head b{min-width:0}.task-index,.task-time{color:var(--muted);font-size:11px;white-space:nowrap;font-variant-numeric:tabular-nums}.task-time{text-align:right}.task b{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.task span{display:block;color:var(--muted);margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.provider{display:inline-block;color:#fff!important;background:var(--mint);border-radius:999px;padding:1px 6px;font-size:10px;font-weight:600}.provider.claude{background:var(--warn);color:#1f2328!important}.provider.agy{background:var(--blue);color:#fff!important}.status-details summary{cursor:pointer;list-style:none}.status-details summary::-webkit-details-marker{display:none}details{border-top:1px solid var(--line);padding:8px 0}pre{white-space:pre-wrap;word-break:break-word;color:var(--muted);font-size:11px;margin:8px 0 0}#tasks{max-height:480px;overflow-y:auto;padding-right:4px}#tasks::-webkit-scrollbar{width:6px}#tasks::-webkit-scrollbar-track{background:transparent}#tasks::-webkit-scrollbar-thumb{background:var(--line);border-radius:3px}#tasks::-webkit-scrollbar-thumb:hover{background:var(--muted)}#history{max-height:180px;overflow-y:auto}.status-groups{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px;margin-top:10px;border-top:1px solid var(--line);padding-top:8px}.status-group{padding:4px 0}.status-head{display:flex;justify-content:space-between;gap:8px;align-items:baseline}.status-name{font-weight:700}.status-bars{display:flex;gap:2px;margin:8px 0}.status-day{height:13px;flex:1;border-radius:1px;background:var(--mint);cursor:pointer;transition:transform .12s cubic-bezier(0.16,1,0.3,1)}.status-day:hover{transform:scaleY(1.35);opacity:.9}.status-day.warn{background:var(--warn)}.status-day.bad{background:var(--bad)}.status-group details{font-size:12px;color:var(--muted);border:0;padding:2px 0}.burn-badge{display:inline-block;font-size:10px;padding:1px 6px;border-radius:4px;font-weight:700;margin-left:5px;vertical-align:middle;font-variant-numeric:tabular-nums}.burn-badge.warn{background:#ff555522;color:var(--bad);border:1px solid var(--bad)}.burn-badge.safe{background:#50fa7b22;color:var(--mint);border:1px solid var(--mint)}.burn-badge.normal{background:#bd93f922;color:var(--blue);border:1px solid var(--blue)}.bloat-alert{display:inline-block;margin-top:6px;font-size:11px;color:var(--warn);background:#ffe60022;border:1px solid var(--warn);border-radius:4px;padding:2px 6px;line-height:1.3}dialog::backdrop{background:rgba(0,0,0,0.55);backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px)}@media(max-width:700px){main{padding:0 12px 24px}.cards{grid-template-columns:repeat(2,1fr)}.task-head{grid-template-columns:62px minmax(0,1fr)}.task-time{grid-column:2;text-align:left}}
</style>
<main><header><div><h1><span>AI</span> VITALS</h1></div><div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap"><div id="updated" class="muted">読み込み中</div><button id="shutdown" type="button">⏹ 停止</button><details id="layout-settings"><summary class="layout-button" title="設定・メニューを開きます">☰ メニュー</summary><div class="settings"><div style="font-weight:700;font-size:11px;color:var(--mint);margin-bottom:6px">セクション表示・並び順</div><div id="layout-item-list"><div class="layout-item" data-item="overview"><label><input type="checkbox" data-toggle="overview"> Today's Overview</label><div class="item-btns"><button type="button" class="btn-order" data-dir="up" title="上へ移動">↑</button><button type="button" class="btn-order" data-dir="down" title="下へ移動">↓</button></div></div><div class="layout-item" data-item="codex"><label><input type="checkbox" data-toggle="codex"> Codex</label><div class="item-btns"><button type="button" class="btn-order" data-dir="up" title="上へ移動">↑</button><button type="button" class="btn-order" data-dir="down" title="下へ移動">↓</button></div></div><div class="layout-item" data-item="claude"><label><input type="checkbox" data-toggle="claude"> Claude</label><div class="item-btns"><button type="button" class="btn-order" data-dir="up" title="上へ移動">↑</button><button type="button" class="btn-order" data-dir="down" title="下へ移動">↓</button></div></div><div class="layout-item" data-item="agy"><label><input type="checkbox" data-toggle="agy"> Antigravity</label><div class="item-btns"><button type="button" class="btn-order" data-dir="up" title="上へ移動">↑</button><button type="button" class="btn-order" data-dir="down" title="下へ移動">↓</button></div></div><div class="layout-item" data-item="tasks"><label><input type="checkbox" data-toggle="tasks"> Active tasks</label><div class="item-btns"><button type="button" class="btn-order" data-dir="up" title="上へ移動">↑</button><button type="button" class="btn-order" data-dir="down" title="下へ移動">↓</button></div></div><div class="layout-item" data-item="history"><label><input type="checkbox" data-toggle="history"> History</label><div class="item-btns"><button type="button" class="btn-order" data-dir="up" title="上へ移動">↑</button><button type="button" class="btn-order" data-dir="down" title="下へ移動">↓</button></div></div></div><button id="layout-reset" type="button" style="width:100%;margin-top:6px">初期配置に戻す</button><div style="border-top:1px solid var(--line);margin:10px 0 6px"></div><div style="font-weight:700;font-size:11px;color:var(--mint);margin-bottom:6px">環境設定</div><label>カラーテーマ<select id="theme-select"><option value="default">🎨 Default Dark</option><option value="dracula">🧛 Dracula</option><option value="cyberpunk">⚡ Cyberpunk 2077</option><option value="synthwave">🌆 Synthwave '84</option><option value="monokai">🐍 Monokai Pro</option><option value="nord">❄️ Nord</option><option value="github-light">☀️ GitHub Light (ライト)</option><option value="solarized-light">🌅 Solarized Light (ライト)</option><option value="one-light">💡 One Light (ライト)</option></select></label><div style="margin-top:6px"><button id="notify-toggle" type="button" class="btn-small" style="width:100%" title="クォータ上限到達やリセット解除をデスクトップ通知します">🔔 通知: オフ</button></div><div style="border-top:1px solid var(--line);margin:10px 0 6px"></div><div style="font-weight:700;font-size:11px;color:var(--mint);margin-bottom:6px">契約プラン設定（表示用）</div><label>Codex plan（表示用）<select id="codex-plan-select"><option>未設定</option><option>ChatGPT Free</option><option>ChatGPT Go</option><option>ChatGPT Plus</option><option>ChatGPT Pro 5x</option><option>ChatGPT Pro 20x</option><option>ChatGPT Business</option><option>ChatGPT Enterprise / Edu</option><option>API 従量課金</option></select></label><label>Claude plan（表示用）<select id="claude-plan-select"><option>未設定</option><option>Claude Free</option><option>Claude Pro</option><option>Claude Max 5x</option><option>Claude Max 20x</option><option>Claude Team</option><option>Claude Enterprise</option><option>API 従量課金</option></select></label><label>Antigravity plan（表示用）<select id="agy-plan-select"><option>未設定</option><option>Google AI Free</option><option>Google AI Plus</option><option>Google AI Pro</option><option>Google AI Ultra 5x</option><option>Google AI Ultra 20x</option><option>API 従量課金</option></select></label></div></details></div></header>
<div id="layout" class="blocks">
<section class="block" data-block="overview" draggable="true"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;flex-wrap:wrap;gap:8px"><h2 class="block-title"><span class="handle">⠿</span>Today's Overview<span class="muted" style="font-size:11px;font-weight:400;margin-left:6px">本日全体の利用サマリー</span></h2><div id="overview-health-badge" class="service-badge">● システム健全性: 取得中</div></div><section class="cards"><article class="card"><div class="label">TODAY'S TASKS</div><div class="value" id="ov-tasks">—</div><div class="muted" id="ov-tasks-detail">本日実行されたプロンプト数</div></article><article class="card"><div class="label">ESTIMATED SPEND (TODAY)</div><div class="value" id="ov-cost">—</div><div class="muted" id="ov-cost-detail">3ツール合算 (API相当額)</div></article><article class="card"><div class="label">TOTAL TOKENS (OBSERVED)</div><div class="value" id="ov-tokens">—</div><div class="muted" id="ov-tokens-detail">観測セッションのトークン合算</div></article><article class="card"><div class="label">MOST ACTIVE TOOL</div><div class="value" id="ov-active-tool">—</div><div class="muted" id="ov-active-detail">本日最も使われたAIツール</div></article></section></section>
<section class="block" data-block="codex" draggable="true"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;flex-wrap:wrap;gap:8px"><h2 class="block-title"><span class="handle">⠿</span>Codex<span id="codex-sync-badge" style="font-size:11px;font-weight:400;color:var(--mint);margin-left:6px"></span></h2><div id="openai-status-badge" class="service-badge">● OpenAI Status: 取得中</div></div><section class="cards"><article class="card"><div class="label">5h LIMIT</div><div class="value" id="primary">—</div><div class="muted" id="primary-reset">解除: —</div><div class="muted" id="primary-detail">使用: — · 残り: —</div><div class="bar"><i id="primary-bar"></i></div></article><article class="card"><div class="label">7d LIMIT<span id="secondary-burn"></span></div><div class="value" id="secondary">—</div><div class="muted" id="secondary-reset">解除: —</div><div class="muted" id="secondary-detail">使用: — · 残り: —</div><div class="bar"><i id="secondary-bar"></i></div></article><article class="card"><div class="label">TOKENS</div><div class="value" id="context">—</div><div class="muted" id="context-detail">CONTEXT: —</div><div class="muted" id="context-remaining">残り: —</div><div class="bar"><i id="context-bar"></i></div><div id="context-bloat"></div></article><article class="card"><div class="label">API EQUIVALENT</div><div class="value" id="cost">—</div><div class="muted" id="cost-note">—</div></article></section><div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-top:8px;flex-wrap:wrap"><div style="display:flex;align-items:center;gap:8px"><div class="plan-badge" id="codex-plan-view" title="クリックして契約プランを選択">PLAN: 未設定 <i>⚙️ 変更</i></div><div class="muted" id="codex-model">—</div></div></div><div id="openai-status-groups" class="status-groups"></div></section>
<section class="block" data-block="claude" draggable="true"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;flex-wrap:wrap;gap:8px"><h2 class="block-title"><span class="handle">⠿</span>Claude Code<span id="claude-sync-badge" style="font-size:11px;font-weight:400;color:var(--mint);margin-left:6px"></span></h2><div id="claude-status-badge" class="service-badge">● Claude Status: 取得中</div></div><section class="cards"><article class="card"><div class="label">5h PLAN LIMIT</div><div class="value" id="claude-primary">—</div><div class="muted" id="claude-primary-reset">解除: —</div><div class="muted" id="claude-primary-detail">使用: — · 残り: —</div><div class="bar"><i id="claude-primary-bar"></i></div></article><article class="card"><div class="label">7d PLAN LIMIT<span id="claude-secondary-burn"></span></div><div class="value" id="claude-secondary">—</div><div class="muted" id="claude-secondary-reset">解除: —</div><div class="muted" id="claude-secondary-detail">使用: — · 残り: —</div><div class="bar"><i id="claude-secondary-bar"></i></div></article><article class="card"><div class="label">TOKENS</div><div class="value" id="claude-total">—</div><div class="muted" id="claude-model">SESSION: —</div><div id="claude-bloat"></div></article><article class="card"><div class="label">API EQUIVALENT</div><div class="value" id="claude-cost">—</div><div class="muted" id="claude-cost-note">—</div></article></section><div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-top:8px;flex-wrap:wrap"><div style="display:flex;align-items:center;gap:8px"><div class="plan-badge" id="claude-plan-view" title="クリックして契約プランを選択">PLAN: 未設定 <i>⚙️ 変更</i></div><div class="muted" id="claude-io">—</div><div class="muted" id="claude-cache">—</div></div></div><div id="claude-status-groups" class="status-groups"></div></section>
<section class="block" data-block="agy" draggable="true"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;flex-wrap:wrap;gap:8px"><h2 class="block-title"><span class="handle">⠿</span>Antigravity<span id="agy-sync-badge" style="font-size:11px;font-weight:400;color:var(--mint);margin-left:6px"></span></h2><div id="google-status-badge" class="service-badge">● Google Status: 取得中</div></div><section class="cards"><article class="card"><div class="label" style="display:flex;justify-content:space-between"><span>GEMINI MODELS</span><span class="muted" id="agy-gem-state">正常</span></div><div style="margin-top:7px"><div style="display:flex;justify-content:space-between;font-size:12px"><span>5h: <b id="agy-gem-5h-val">—</b></span><span class="muted" id="agy-gem-5h-rem">残り: —</span></div><div class="bar" style="margin-top:3px;height:6px"><i id="agy-gem-5h-bar"></i></div></div><div style="margin-top:9px"><div style="display:flex;justify-content:space-between;font-size:12px"><span>7d: <b id="agy-gem-7d-val">—</b><span id="agy-gem-burn"></span></span><span class="muted" id="agy-gem-7d-rem">残り: —</span></div><div class="bar" style="margin-top:3px;height:6px"><i id="agy-gem-7d-bar"></i></div></div><div class="muted" id="agy-gem-reset" style="margin-top:6px;font-size:11px">解除: —</div></article><article class="card"><div class="label" style="display:flex;justify-content:space-between"><span>CLAUDE &amp; GPT</span><span class="muted" id="agy-cld-state">正常</span></div><div style="margin-top:7px"><div style="display:flex;justify-content:space-between;font-size:12px"><span>5h: <b id="agy-cld-5h-val">—</b></span><span class="muted" id="agy-cld-5h-rem">残り: —</span></div><div class="bar" style="margin-top:3px;height:6px"><i id="agy-cld-5h-bar"></i></div></div><div style="margin-top:9px"><div style="display:flex;justify-content:space-between;font-size:12px"><span>7d: <b id="agy-cld-7d-val">—</b><span id="agy-cld-burn"></span></span><span class="muted" id="agy-cld-7d-rem">残り: —</span></div><div class="bar" style="margin-top:3px;height:6px"><i id="agy-cld-7d-bar"></i></div></div><div class="muted" id="agy-cld-reset" style="margin-top:6px;font-size:11px">解除: —</div></article><article class="card"><div class="label">TOKENS (ESTIMATED)</div><div class="value" id="agy-tokens">—</div><div class="muted" id="agy-tokens-io">IN / OUT: —</div><div class="muted" id="agy-model">SESSION: —</div><div id="agy-bloat"></div></article><article class="card"><div class="label">API EQUIVALENT</div><div class="value" id="agy-cost">—</div><div class="muted" id="agy-steps">ACTIVITY: —</div><div class="muted" id="agy-cost-note">API相当（推定費用）</div></article></section><div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-top:8px;flex-wrap:wrap"><div style="display:flex;align-items:center;gap:8px"><div class="plan-badge" id="agy-plan-view" title="クリックして契約プランを選択">PLAN: 未設定 <i>⚙️ 変更</i></div><button type="button" class="btn-small" id="agy-quick-usage" title="手動で使用率%とリセット日を入力">⚙️ 使用率を設定</button></div></div><div id="google-status-groups" class="status-groups"></div></section>
<section class="block" data-block="tasks" draggable="true"><h2 class="block-title"><span class="handle">⠿</span>Active tasks</h2><div id="tasks" class="muted">データなし</div></section>
<section class="block" data-block="history" draggable="true"><h2 class="block-title"><span class="handle">⠿</span>Recent snapshots</h2><div id="history" class="muted">履歴なし</div></section>
</div><dialog id="agy-usage-dialog" style="background:color-mix(in srgb,var(--panel) 94%,transparent);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);color:var(--text);border:1px solid color-mix(in srgb,var(--line) 90%,rgba(255,255,255,0.08));border-radius:14px;padding:20px;max-width:390px;box-shadow:0 24px 48px rgba(0,0,0,0.4),0 0 0 1px rgba(255,255,255,0.04)"><form method="dialog" id="agy-usage-form"><h3 style="margin:0 0 10px;font-size:15px;color:var(--mint)">Antigravity 使用量設定</h3><p class="muted" style="margin:0 0 12px;font-size:12px">IDEの「View Usage」に表示されている<b>残り% (Remaining)</b>を入力してください。</p><div style="margin-bottom:10px;border:1px solid var(--line);border-radius:8px;padding:10px;background:var(--card)"><b style="font-size:13px;display:block;margin-bottom:6px">Gemini Models</b><label style="display:flex;justify-content:space-between;align-items:center;font-size:12px;margin-bottom:6px"><span>5-Hour Limit 残り%</span><input type="number" id="input-gem-5h" min="0" max="100" placeholder="84" style="width:70px;background:var(--badge);border:1px solid var(--line);color:var(--text);padding:4px 6px;border-radius:4px;text-align:right">%</label><label style="display:flex;justify-content:space-between;align-items:center;font-size:12px"><span>Weekly Limit 残り%</span><input type="number" id="input-gem-7d" min="0" max="100" placeholder="92" style="width:70px;background:var(--badge);border:1px solid var(--line);color:var(--text);padding:4px 6px;border-radius:4px;text-align:right">%</label></div><div style="margin-bottom:14px;border:1px solid var(--line);border-radius:8px;padding:10px;background:var(--card)"><b style="font-size:13px;display:block;margin-bottom:6px">Claude &amp; GPT models</b><label style="display:flex;justify-content:space-between;align-items:center;font-size:12px;margin-bottom:6px"><span>5-Hour Limit 残り%</span><input type="number" id="input-cld-5h" min="0" max="100" placeholder="0" style="width:70px;background:var(--badge);border:1px solid var(--line);color:var(--text);padding:4px 6px;border-radius:4px;text-align:right">%</label><label style="display:flex;justify-content:space-between;align-items:center;font-size:12px"><span>Weekly Limit 残り%</span><input type="number" id="input-cld-7d" min="0" max="100" placeholder="66" style="width:70px;background:var(--badge);border:1px solid var(--line);color:var(--text);padding:4px 6px;border-radius:4px;text-align:right">%</label></div><div style="display:flex;justify-content:flex-end;gap:8px"><button type="button" class="btn-small" id="agy-dialog-cancel">キャンセル</button><button type="submit" class="btn-small" style="background:var(--mint);color:var(--bg);font-weight:700">保存して反映</button></div></form></dialog></main>
<script>
let serverStopped=false;const autoStop=()=>{if(!serverStopped){navigator.sendBeacon('/api/shutdown?delay=3')}};window.addEventListener('pagehide',autoStop);window.addEventListener('beforeunload',autoStop);
const $=id=>document.getElementById(id),layout=$('layout'),layoutKey='ai-vitals.layout.v4',defaults=['overview','codex','claude','agy','tasks','history'];const text=(id,value)=>$(id).textContent=value??'—';const tokens=value=>value>=1000?(value/1000).toFixed(1)+'k':String(value??0);const resetAt=value=>value?new Date(value*1000).toLocaleString('ja-JP',{timeZone:'Asia/Tokyo'}):'—';const indicatorColor=value=>value>=80?'#ff7a7a':value>=60?'#ffce6a':'#72dfb5';
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
$('agy-quick-usage').addEventListener('click',()=>{if(latestAgyLimits){const g=latestAgyLimits.gemini||{},c=latestAgyLimits.claude_gpt||{};const g5=g.primary?.used_percent,g7=g.secondary?.used_percent;const c5=c.primary?.used_percent,c7=c.secondary?.used_percent;$('input-gem-5h').value=g5!=null?Math.max(0,100-g5):'';$('input-gem-7d').value=g7!=null?Math.max(0,100-g7):'';$('input-cld-5h').value=c5!=null?Math.max(0,100-c5):'';$('input-cld-7d').value=c7!=null?Math.max(0,100-c7):''}if(agyDialog.showModal){agyDialog.showModal()}else{agyDialog.setAttribute('open','')}});
$('agy-dialog-cancel').addEventListener('click',()=>{if(agyDialog.close){agyDialog.close()}else{agyDialog.removeAttribute('open')}});
$('agy-usage-form').addEventListener('submit',async(e)=>{e.preventDefault();const g5=$('input-gem-5h').value.trim(),g7=$('input-gem-7d').value.trim(),c5=$('input-cld-5h').value.trim(),c7=$('input-cld-7d').value.trim();const payload={};if(g5!=='')payload.gemini_5h_remaining=parseInt(g5,10);if(g7!=='')payload.gemini_7d_remaining=parseInt(g7,10);if(c5!=='')payload.claude_5h_remaining=parseInt(c5,10);if(c7!=='')payload.claude_7d_remaining=parseInt(c7,10);try{await fetch('/api/agy-usage',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});if(agyDialog.close){agyDialog.close()}else{agyDialog.removeAttribute('open')};loadSnapshot()}catch(err){alert('保存に失敗しました: '+err.message)}});
function renderAgyLimits(prefix,limitsData,quota,usageStats){const pri=limitsData?.primary,sec=limitsData?.secondary;const nowSec=Date.now()/1000;const isPriLive=Boolean(pri?.is_live);const isSecLive=Boolean(sec?.is_live);const priExpired=pri?.is_reset||(pri?.resets_at&&pri.resets_at<=nowSec);const secExpired=sec?.is_reset||(sec?.resets_at&&sec.resets_at<=nowSec);const hasManualPri=pri?.used_percent!=null&&!priExpired&&!isPriLive;const hasManualSec=sec?.used_percent!=null&&!secExpired&&!isSecLive;const hasPlan=Boolean(quota?.["5h"]||quota?.["7d"]);let autoPriUsed=null,autoSecUsed=null;if(quota?.["5h"]&&usageStats?.steps_5h!=null){autoPriUsed=Math.min(100,Math.round((usageStats.steps_5h/quota["5h"])*100))}if(quota?.["7d"]&&usageStats?.steps_7d!=null){autoSecUsed=Math.min(100,Math.round((usageStats.steps_7d/quota["7d"])*100))}const isPriAuto=!isPriLive&&!hasManualPri&&hasPlan&&autoPriUsed!=null;const isSecAuto=!isSecLive&&!hasManualSec&&hasPlan&&autoSecUsed!=null;let priUsed=isPriLive?pri.used_percent:(hasManualPri?pri.used_percent:(isPriAuto?autoPriUsed:(priExpired?0:pri?.used_percent)));let secUsed=isSecLive?sec.used_percent:(hasManualSec?sec.used_percent:(isSecAuto?autoSecUsed:(secExpired?0:sec?.used_percent)));let priRem=Math.max(0,100-(priUsed??0));let secRem=Math.max(0,100-(secUsed??0));const priLabel=isPriLive?' (公式同期)':(isPriAuto?' (自動)':(hasManualPri?' (手動)':''));const secLabel=isSecLive?' (公式同期)':(isSecAuto?' (自動)':(hasManualSec?' (手動)':''));text(prefix+'-5h-val',priUsed!=null?priUsed+'%':'—');text(prefix+'-5h-rem',priRem===0?'残り: 0% (上限)':(`残り: ${priRem}%`+priLabel));if(priRem===0){$(prefix+'-5h-rem').style.color='var(--bad)'}else{$(prefix+'-5h-rem').style.color='var(--muted)'}paintBar(prefix+'-5h',priUsed);text(prefix+'-7d-val',secUsed!=null?secUsed+'%':'—');text(prefix+'-7d-rem',secRem===0?'残り: 0% (上限)':(`残り: ${secRem}%`+secLabel));if(secRem===0){$(prefix+'-7d-rem').style.color='var(--bad)'}else{$(prefix+'-7d-rem').style.color='var(--muted)'}paintBar(prefix+'-7d',secUsed);const resetTexts=[];if(isPriLive&&pri?.resets_at){resetTexts.push(priExpired?'5h: リセット済み':`5h解除: ${resetAt(pri.resets_at)} (${formatRemaining(pri.resets_at)})`)}else if(isPriAuto&&quota?.["5h"]){resetTexts.push(`5h: ${usageStats?.steps_5h||0}/${quota["5h"]}st (自動)`)}else if(!isPriAuto&&pri?.resets_at){resetTexts.push(priExpired?'5h: リセット済み':`5h解除: ${resetAt(pri.resets_at)} (${formatRemaining(pri.resets_at)})`)}if(isSecLive&&sec?.resets_at){resetTexts.push(secExpired?'7d: リセット済み':`7d解除: ${resetAt(sec.resets_at)} (${formatRemaining(sec.resets_at)})`)}else if(isSecAuto&&quota?.["7d"]){resetTexts.push(`7d: ${usageStats?.steps_7d||0}/${quota["7d"]}st (自動)`)}else if(!isSecAuto&&sec?.resets_at){resetTexts.push(secExpired?'7d: リセット済み':`7d解除: ${resetAt(sec.resets_at)} (${formatRemaining(sec.resets_at)})`)}text(prefix+'-reset',resetTexts.join(' · ')||(isPriLive||isSecLive||isPriAuto||isSecAuto?'自動同期中':'解除: —'));const burnEl=$(prefix+'-burn');if(burnEl){burnEl.innerHTML=burnRateBadge(secUsed,sec?.resets_at,10080)}const stateEl=$(prefix+'-state');if(priRem===0||secRem===0){stateEl.textContent='上限到達';stateEl.style.color='var(--bad)'}else if((priUsed&&priUsed>=80)||(secUsed&&secUsed>=80)){stateEl.textContent='制限警戒';stateEl.style.color='var(--warn)'}else{stateEl.textContent='正常';stateEl.style.color='var(--mint)'}}
function renderOverview(data){const tasks=data.tasks||[],todayStr=new Date().toLocaleDateString('ja-JP');const todayTasks=tasks.filter(t=>t.observed_at&&new Date(t.observed_at).toLocaleDateString('ja-JP')===todayStr);text('ov-tasks',`${todayTasks.length} tasks`);const counts={Codex:0,Claude:0,Antigravity:0};todayTasks.forEach(t=>{if(counts[t.provider]!=null)counts[t.provider]++;else if(t.provider==='AGY')counts.Antigravity++});text('ov-tasks-detail',`本日: Codex ${counts.Codex} · Claude ${counts.Claude} · Antigravity ${counts.Antigravity}`);const current=data.current||{},claude=data.claude||{},agy=data.agy||{};let totalCost=0;if(current.estimated_cost_usd!=null)totalCost+=current.estimated_cost_usd;if(claude.estimated_cost_usd!=null){totalCost+=(claude.estimated_cost_usd.minimum_usd+claude.estimated_cost_usd.maximum_usd)/2}if(agy.estimated_cost_usd!=null)totalCost+=agy.estimated_cost_usd;text('ov-cost',totalCost>0?`$${totalCost.toFixed(4)}`:'$0.0000');text('ov-cost-detail','3ツール合算 (API相当額)');let totalTokens=0;if(current.context?.used_tokens)totalTokens+=current.context.used_tokens;if(claude.usage?.total_tokens)totalTokens+=claude.usage.total_tokens;const agyTok=agy.usage?.total_tokens||((agy.usage?.input_tokens||0)+(agy.usage?.output_tokens||0));totalTokens+=agyTok;text('ov-tokens',tokens(totalTokens));text('ov-tokens-detail',`Codex: ${tokens(current.context?.used_tokens||0)} · Claude: ${tokens(claude.usage?.total_tokens||0)} · Antigravity: ${tokens(agyTok)}`);let mostActive='—',maxCount=0;for(const[p,c]of Object.entries(counts)){if(c>maxCount){maxCount=c;mostActive=p}}text('ov-active-tool',mostActive!=='—'?`${mostActive} (${maxCount}回)`:(tasks.length>0?(tasks[0].provider==='AGY'?'Antigravity':tasks[0].provider):'—'));text('ov-active-detail',maxCount>0?'本日最も多くタスクを実行':'セッション待機中');const statuses=[data.statuses?.openai?.state,data.statuses?.claude?.state,data.statuses?.google?.state];const hasOutage=statuses.some(s=>s&&(s.includes('障害')||s.includes('停止'))),hasDegraded=statuses.some(s=>s&&!s.includes('正常')&&!s.includes('Operational')&&s!=='OK'&&s!=='取得不可');const hBadge=$('overview-health-badge');if(hBadge){if(hasOutage){hBadge.innerHTML='<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:#ff7a7a"></span>システム健全性: <b style="color:var(--bad)">障害発生中</b>'}else if(hasDegraded){hBadge.innerHTML='<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:#ffce6a"></span>システム健全性: <b style="color:var(--warn)">一部低下中</b>'}else{hBadge.innerHTML='<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:#72dfb5"></span>システム健全性: <b style="color:var(--mint)">全システム正常</b>'}}}
function render(data){renderOverview(data);const current=data.current||{};limit('primary',current.limits?.primary);limit('secondary',current.limits?.secondary);const context=current.context||{},used=context.used_percent,remaining=Math.max(0,(context.window_tokens||0)-(context.used_tokens||0));text('context',used==null?'—':used+'%');text('context-detail',context.window_tokens?`CONTEXT: ${tokens(context.used_tokens)} / ${tokens(context.window_tokens)} tokens`:'CONTEXT: —');text('context-remaining',context.window_tokens?`残り: ${tokens(remaining)} tokens`:'残り: —');paintBar('context',used);renderCost('cost','cost-note',current.estimated_cost_usd,current.model,false);renderBloat('context-bloat',(used!=null&&used>=70)||(context.used_tokens&&context.used_tokens>=100000));text('codex-model','SESSION: '+([current.model,current.observed_at&&new Date(current.observed_at).toLocaleString('ja-JP')].filter(Boolean).join(' · ')||'待機中'));if(current&&current.observed_at){text('codex-sync-badge','[ローカル同期中]');$('codex-sync-badge').style.color='var(--mint)'}else{text('codex-sync-badge','')}if(current.limits?.primary?.used_percent!=null){checkNotify('codex-5h',Math.max(0,100-current.limits.primary.used_percent),'Codex','5h枠')}if(current.limits?.secondary?.used_percent!=null){checkNotify('codex-7d',Math.max(0,100-current.limits.secondary.used_percent),'Codex','7d枠')}const claude=data.claude||{},usage=claude.usage||{};limit('claude-primary',claude.limits?.primary);limit('claude-secondary',claude.limits?.secondary);text('claude-total',tokens(usage.total_tokens));text('claude-model','SESSION: '+([claude.model,claude.observed_at&&new Date(claude.observed_at).toLocaleString('ja-JP')].filter(Boolean).join(' · ')||'待機中'));renderCost('claude-cost','claude-cost-note',claude.estimated_cost_usd,claude.model,true);renderBloat('claude-bloat',Boolean(usage.total_tokens&&usage.total_tokens>=100000));text('claude-io',`INPUT / OUTPUT: ${tokens(usage.input_tokens)} / ${tokens(usage.output_tokens)}`);text('claude-cache',`cache: ${tokens((usage.cache_creation_input_tokens||0)+(usage.cache_read_input_tokens||0))}`);if(claude.limits?.primary?.used_percent!=null||claude.limits?.secondary?.used_percent!=null){text('claude-sync-badge','[/usage同期中]');$('claude-sync-badge').style.color='var(--mint)'}else if(claude.observed_at){text('claude-sync-badge','[ローカル同期中]');$('claude-sync-badge').style.color='var(--mint)'}else{text('claude-sync-badge','')}if(claude.limits?.primary?.used_percent!=null){checkNotify('claude-5h',Math.max(0,100-claude.limits.primary.used_percent),'Claude Code','5h枠')}if(claude.limits?.secondary?.used_percent!=null){checkNotify('claude-7d',Math.max(0,100-claude.limits.secondary.used_percent),'Claude Code','7d枠')}const agy=data.agy||{},agyUsage=agy.usage||{};if(agy.plan&&($('agy-plan-select').value==='未設定'||$('agy-plan-select').value==='Google AI Plus')){const opts=[...$('agy-plan-select').options].map(o=>o.value);if(opts.includes(agy.plan)){$('agy-plan-select').value=agy.plan;renderPlans()}}const agyPlan=$('agy-plan-select').value;const planQuotas=data.plan_quotas||{},planQuotaObj=planQuotas[agyPlan]||{};latestAgyLimits=agy.limits||{};renderAgyLimits('agy-gem',agy.limits?.gemini,planQuotaObj.gemini,agyUsage.gemini);renderAgyLimits('agy-cld',agy.limits?.claude_gpt,planQuotaObj.claude_gpt,agyUsage.claude_gpt);const gemPri=agy.limits?.gemini?.primary,gemSec=agy.limits?.gemini?.secondary;if(gemPri?.used_percent!=null){checkNotify('agy-gem-5h',Math.max(0,100-gemPri.used_percent),'Antigravity (Gemini)','5h枠')}if(gemSec?.used_percent!=null){checkNotify('agy-gem-7d',Math.max(0,100-gemSec.used_percent),'Antigravity (Gemini)','7d枠')}const cldPri=agy.limits?.claude_gpt?.primary,cldSec=agy.limits?.claude_gpt?.secondary;if(cldPri?.used_percent!=null){checkNotify('agy-cld-5h',Math.max(0,100-cldPri.used_percent),'Antigravity (Claude/GPT)','5h枠')}if(cldSec?.used_percent!=null){checkNotify('agy-cld-7d',Math.max(0,100-cldSec.used_percent),'Antigravity (Claude/GPT)','7d枠')}const agyTokTotal=agyUsage.total_tokens||(agyUsage.input_tokens||0)+(agyUsage.output_tokens||0);text('agy-tokens',tokens(agyTokTotal));text('agy-tokens-io',`IN: ${tokens(agyUsage.input_tokens)} / OUT: ${tokens(agyUsage.output_tokens)}`);const modelFamBadge=agy.model_family==='claude_gpt'?'[Claude/GPT系]':'[Gemini系]';text('agy-model','MODEL: '+(agy.model||'Gemini 2.5 Flash')+' '+modelFamBadge);renderBloat('agy-bloat',agyTokTotal>=120000);text('agy-steps',`Gemini: ${agyUsage.gemini?.steps||0}st / Claude: ${agyUsage.claude_gpt?.steps||0}st (${agyUsage.steps||0} total)`);renderCost('agy-cost','agy-cost-note',agy.estimated_cost_usd,agy.model,false);if(agy.live_status){text('agy-sync-badge','[IDE公式同期中]');$('agy-sync-badge').style.color='var(--mint)'}else if(agy.limits?.gemini?.primary?.used_percent!=null||agy.limits?.claude_gpt?.primary?.used_percent!=null){text('agy-sync-badge','[手動設定中]');$('agy-sync-badge').style.color='var(--blue)'}else{text('agy-sync-badge','')}renderStatus('openai',data.statuses?.openai);renderStatus('claude',data.statuses?.claude);renderStatus('google',data.statuses?.google);text('updated',data.snapshot_at?'更新 '+new Date(data.snapshot_at).toLocaleString('ja-JP'):(current.observed_at?'更新 '+new Date(current.observed_at).toLocaleString('ja-JP'):'セッション待機中'));const tasks=$('tasks');const tasksList=(data.tasks||[]).slice().sort((a,b)=>String(b.observed_at||'').localeCompare(String(a.observed_at||'')));tasks.replaceChildren(...tasksList.map((task,index)=>{const row=document.createElement('div');row.className='task';const head=document.createElement('div');head.className='task-head';const order=document.createElement('span');order.className='task-index';order.textContent=`#${index+1} · ${index}件前`;const title=document.createElement('b');title.textContent=task.task?.prompt||'プロンプト未取得';const observed=document.createElement('time');observed.className='task-time';observed.textContent=task.observed_at?new Date(task.observed_at).toLocaleString('ja-JP',{timeZone:'Asia/Tokyo'}):'日時不明';head.append(order,title,observed);const meta=document.createElement('span'),provider=document.createElement('i');provider.className='provider '+(task.provider==='Claude'?'claude':(task.provider==='Antigravity'||task.provider==='AGY')?'agy':'');provider.textContent=task.provider==='AGY'?'Antigravity':task.provider;meta.append(provider,document.createTextNode(' '+[task.task?.cwd,task.model].filter(Boolean).join(' · ')));row.append(head,meta);return row}));if(!tasks.childNodes.length)tasks.textContent='データなし';const history=$('history');history.replaceChildren(...(data.history||[]).reverse().map(item=>{const detail=document.createElement('details'),title=document.createElement('summary'),pre=document.createElement('pre');title.textContent=item.split('\\n')[0]||'snapshot';pre.textContent=item;detail.append(title,pre);return detail}));if(!history.childNodes.length)history.textContent='履歴なし'}
async function loadSnapshot(){try{render(await (await fetch('/api/snapshot',{cache:'no-store'})).json())}catch(error){text('updated','取得エラー: '+error.message)}}loadSnapshot();setInterval(loadSnapshot,15000);
</script></html>"""


def main(argv: list[str] | None = None) -> None:
    """Start the loopback dashboard."""
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

        has_any = any(v is not None for v in (g5, g7, c5, c7, args.agy_used_percent))
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
            used_percent=args.agy_used_percent,
            resets_at=args.agy_resets_at,
            window_minutes=args.agy_window_minutes,
        )
        print(f"AGY usage saved: {agy_usage_path(args.state_home)}")
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
