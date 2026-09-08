@echo off
setlocal
chcp 65001 >nul 2>&1

rem ==============================================================================
rem AI Vitals - Windows Background Launcher
rem
rem バックグラウンドでHTTPサーバーを起動し、既定のWebブラウザで
rem http://127.0.0.1:4202 を開きます。コマンドプロンプトは即座に復帰します。
rem ==============================================================================

set "SCRIPT_DIR=%~dp0"
set "TARGET_PY=%SCRIPT_DIR%ai_vitals.py"

rem Pythonランチャー (py) または python コマンドの検出
where py >nul 2>nul
if %ERRORLEVEL% equ 0 (
    set "PY_CMD=py"
) else (
    where python >nul 2>nul
    if %ERRORLEVEL% equ 0 (
        set "PY_CMD=python"
    ) else (
        echo [エラー] Pythonが見つかりませんでした。PythonがインストールされPATHに通っているか確認してください。
        pause
        exit /b 1
    )
)

echo [AI Vitals] バックグラウンド起動中...
%PY_CMD% "%TARGET_PY%" -b %*

endlocal
