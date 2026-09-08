<#
.SYNOPSIS
    AI Vitals - Windows Background Launcher (PowerShell)
.DESCRIPTION
    バックグラウンドでHTTPサーバーを起動し、既定のWebブラウザで
    http://127.0.0.1:4202 を開きます。
#>

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$TargetPy = Join-Path $ScriptDir "ai_vitals.py"

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

# Pythonコマンドの検出 (py または python)
$PyCmd = if (Get-Command py -ErrorAction SilentlyContinue) {
    "py"
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    "python"
} else {
    Write-Error "[エラー] Pythonが見つかりませんでした。PATHを確認してください。"
    exit 1
}

Write-Host "[AI Vitals] バックグラウンド起動中..."
Start-Process -FilePath $PyCmd -ArgumentList "`"$TargetPy`"", "-b", ($args -join " ") -WindowStyle Hidden
