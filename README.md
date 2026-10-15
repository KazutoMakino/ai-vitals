# AI Vitals

> **Local, dependency-free usage dashboard for Codex, Claude Code, and Antigravity.**  
> 外部依存なし（Python標準ライブラリのみ）・完全ローカル動作のマルチAI利用状況モニタリングツール。

[![CI](https://github.com/KazutoMakino/ai-vitals/actions/workflows/ci.yml/badge.svg)](https://github.com/KazutoMakino/ai-vitals/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

---

## 🌟 特徴 (Features)

- **3大AIコーディングアシスタントを一元監視**
  - **Codex** (ChatGPT)
  - **Claude Code** (Anthropic)
  - **Antigravity** (Google Gemini)
- **ゼロ依存 (Zero Dependencies)**
  - `pip` による追加パッケージのインストールは不要。Python 3.10+ の標準ライブラリ（`http.server`, `urllib` 等）だけで動作します。
- **完全ローカル完結で安全 (100% Local & Privacy-Friendly)**
  - 外部サーバーへの通信やデータ送信は一切行いません（ローカル `127.0.0.1` ループバックアドレスのみバインド）。
  - APIキーやシークレットの入力も不要。ローカルに保存されている各ツールのセッションログから使用量のみを安全に集計します。
- **リッチで便利なモニタリング機能**
  - ⚡ **リアルタイム秒単位カウントダウン**: 5時間リセット・週制限リセットまでの残り時間をライブ表示。
  - 📈 **週枠消費ペース判定**: 経過時間と消費率からペースをリアルタイム判定（`⚡ ハイペース注意` / `⚡ 適正ペース` / `⚡ 安全ペース`）。
  - 📊 **本日横断サマリー (Today's Overview)**: 3ツールの合計タスク数・合算推定APIコスト・総消費トークンを一目で把握。
  - 📅 **月間使用量・クォータ（予算・トークン上限）管理**: 当月の利用実績（推定コスト・トークン数・タスク数）の横断集計と、ツール別・全体の月間予算および上限進捗バー、月間消化ペース判定（`⚡ ハイペース注意` / `⚡ 適正ペース` / `⚡ 安全ペース`）。
  - ⚠️ **コンテキスト肥大化警告**: トークン消費が一定ラインを超えた場合に警告バッジを表示。
  - 🔔 **デスクトップWeb通知**: クォータが上限（残り0%）に達した際やリセット解除時にブラウザ通知。
  - 🚦 **各社障害ステータス常時表示**: OpenAI、Claude、Google各サービスの稼働状態と90日インシデント履歴を常時モニタリング。
- **多彩なビジュアルテーマ (Themes)**
  - ダーク系: Default, Dracula, Cyberpunk, Synthwave, Monokai, Nord
  - ライト系: GitHub Light, Solarized Light, One Light

---

## 📋 前提条件 (Prerequisites)

本ツールを使用するにあたり、以下の環境・前提条件をご確認ください。

### 1. 実行環境
- **Python 3.10 以上**
  - Python 標準ライブラリのみで動作します（`pip` による追加パッケージのインストールは不要です）。
- **Web ブラウザ**
  - モダンブラウザ（Google Chrome, Microsoft Edge, Firefox, Safari 等）。
  - クォータ上限到達やリセット解除のデスクトップ通知を利用する場合は、ブラウザ側で通知の許可が必要です。
- **空きポート**
  - デフォルトの待受ポート `4202`（`--port` オプションで変更可能）。

### 2. 監視対象ツールの利用環境
本ツールは、各アシスタントがローカルに保存するセッションログやキャッシュを解析して可視化します。
以下のいずれか（またはすべて）の利用ログが存在する必要があります：

- **Codex (ChatGPT)**:
  - `~/.codex/sessions/` 配下に JSONL ログが存在すること（`CODEX_HOME` 環境変数で変更可能）。
- **Claude Code (Anthropic)**:
  - `~/.claude/projects/` 配下に JSONL ログ、または `~/.claude/vscode-claude-status-cache.json` が存在すること（`CLAUDE_HOME` 環境変数で変更可能）。
- **Antigravity (Google Gemini)**:
  - `~/.gemini/antigravity/brain/` 配下にトランスクリプトログが存在すること（`ANTIGRAVITY_HOME` 環境変数で変更可能）。

> [!NOTE]
> **すべてのツールを導入している必要はありません**。お使いのツールのログのみが自動検出されてダッシュボードに反映されます（1種類のみの利用でも問題なく動作します）。

### 3. オプション要件
- **Antigravity リアルタイムクォータ連携 (Linux / macOS / Windows 対応)**:
  - 実行中の Antigravity language_server からリアルタイムの公式クォータ（5h / 7d）を自動取得する場合、**Antigravity (IDE) が起動中** である必要があります。
  - OSごとの標準コマンド（Linux: `pgrep`・`ss`・`lsof`、macOS: `ps`・`lsof`、Windows: PowerShell・`netstat`）を利用して自動連携するため、**追加ツールのインストールは不要**です。
- **各社障害ステータス・インシデント履歴の表示**:
  - OpenAI、Claude、Google Cloud の公式ステータス API（Statuspage / incidents.json）へアクセスするため、**インターネット接続（HTTPS 通信）** が必要です。
  - ※ オフライン環境でもローカルログの集計やダッシュボード機能は動作します（ステータス欄のみ「取得不可」表示となります）。

---

## 🚀 クイックスタート (Quick Start)

### 1. リポジトリをクローンまたはダウンロード
```bash
git clone https://github.com/KazutoMakino/ai-vitals.git
cd ai-vitals
```

### 2. 起動

#### Linux / macOS
```bash
# 通常起動（フォアグラウンド）
python3 ai_vitals.py

# バックグラウンド起動（ターミナルを即復帰）
./ai-vitals-bg.sh
```

#### Windows
```cmd
rem 通常起動
py ai_vitals.py

rem バックグラウンド起動（コマンドプロンプトを即復帰、ダブルクリックも可）
ai-vitals-bg.bat

rem PowerShellからバックグラウンド起動
powershell -ExecutionPolicy Bypass -File ai-vitals-bg.ps1
```

※ 起動後、自動的に既定のWebブラウザで `http://127.0.0.1:4202` が開きます。  
※ 画面右上の「⏹ 停止」ボタンから安全にサーバーを終了できます。

---

## ⚙️ コマンドラインオプション (CLI Options)

```text
usage: ai_vitals.py [-h] [--host HOST] [--port PORT]
                    [--codex-home CODEX_HOME] [--state-home STATE_HOME]
                    [--claude-home CLAUDE_HOME] [--agy-home AGY_HOME]
                    [--save-claude-usage] [--save-agy-usage]
                    [-b] [--no-open]
```

| オプション | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `-b`, `--background` | `False` | サーバーをバックグラウンドプロセスとして起動 |
| `--port` | `4202` | ダッシュボードの待受ポート番号 |
| `--no-open` | `False` | 起動時にブラウザを自動で開かない |
| `--host` | `127.0.0.1` | 待受ホスト（セキュリティのため `127.0.0.1` 固定） |

---

## 📥 手動クォータの反映 (Manual Quota Update)

### Claude Code
Claude Codeのセッション中に `/usage` コマンドで確認したプラン残量を保存してダッシュボードに反映できます。
```bash
python3 ai_vitals.py --save-claude-usage \
  --claude-primary-used-percent 25 --claude-primary-resets-at 1788686400 \
  --claude-secondary-used-percent 70 --claude-secondary-resets-at 1788940800
```

### Antigravity (Gemini / Claude / GPT)
画面上の「⚙️ 設定」ダイアログからGUIで入力・保存するか、CLIから保存できます。
```bash
python3 ai_vitals.py --save-agy-usage \
  --agy-gemini-5h-remaining 75 --agy-gemini-7d-remaining 40
```

### 月間予算・上限設定 (Monthly Quotas & Budgets)
画面右上の「☰ メニュー」内の「月間予算・上限設定」からGUIで入力・保存するか、CLIから保存できます。
```bash
python3 ai_vitals.py --save-monthly-settings \
  --billing-day 1 \
  --monthly-budget-codex 15.0 --monthly-tokens-codex 2000000 \
  --monthly-budget-claude 25.0 --monthly-tokens-claude 3000000 \
  --monthly-budget-agy 10.0 --monthly-tokens-agy 5000000
```

---

## 🔒 セキュリティとプライバシー (Security & Privacy)

1. **完全ローカルバインド**
   - サーバーは `127.0.0.1` 以外でのバインドをコードレベルで拒否します。LANや外部ネットワークに公開されることはありません。
2. **認証情報の非保持**
   - APIキーやアクセストークンを要求・保存することはありません。各ツールがローカルに保存しているセッションメタデータのみを読み取ります。
3. **安全なシャットダウン**
   - Web画面上の「⏹ 停止」ボタンから、いつでもプロセスを停止可能です。

---

## 📄 ライセンス (License)

本プロジェクトは [MIT License](LICENSE) の下で公開されています。

---

## ⚠️ 免責事項 (Disclaimer)

本ツールは個人によって開発された**非公式（Unofficial）**のユーティリティです。OpenAI、Anthropic、Google各社とは一切関係ありません。各サービスのアップデートにより仕様が変更される場合があります。不具合や仕様変更にお気づきの際は、[Issues](https://github.com/KazutoMakino/ai-vitals/issues) にてお知らせください。
