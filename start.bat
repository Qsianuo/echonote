@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [EchoNote] 正在创建 Python 虚拟环境...
  python -m venv .venv
  if errorlevel 1 (
    echo [EchoNote] 创建虚拟环境失败，请确认已安装 Python 3.9 及以上版本。
    pause
    exit /b 1
  )
)

echo [EchoNote] 检查依赖...
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo [EchoNote] 依赖安装失败。
  echo 国内网络可先执行: set HTTPS_PROXY=http://127.0.0.1:7897
  pause
  exit /b 1
)

echo.
echo ============================================
echo   EchoNote 声记已启动
echo   浏览器打开: http://127.0.0.1:8000
echo   首次运行会自动下载语音模型，请耐心等待
echo ============================================
echo.

start "" http://127.0.0.1:8000
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000

pause
