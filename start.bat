@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo [EchoNote] 检查 Python 版本...
where python >nul 2>nul
if errorlevel 1 (
  echo [EchoNote] 未找到 python 命令。请安装 Python 3.10 或更高版本：
  echo             https://www.python.org/downloads/
  pause
  exit /b 1
)

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
  echo [EchoNote] Python 版本过低。EchoNote 需要 Python 3.10 及以上。
  echo             Python 3.9 已于 2025 年 10 月停止安全支持，请升级后再试。
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [EchoNote] 正在创建虚拟环境...
  python -m venv .venv
  if errorlevel 1 (
    echo [EchoNote] 创建虚拟环境失败。
    pause
    exit /b 1
  )
)

set "PY=.venv\Scripts\python.exe"

echo [EchoNote] 检查模型与网络...
"%PY%" tools\preflight.py
if errorlevel 1 (
  echo [EchoNote] 启动已中止，请按上面的提示处理后重试。
  pause
  exit /b 1
)

rem 只在 requirements.txt 变化时安装依赖：离线二次启动不会被 pip 阻塞。
"%PY%" -c "import hashlib,pathlib;print(hashlib.sha256(pathlib.Path('requirements.txt').read_bytes()).hexdigest()[:16], end='')" > ".venv\.reqhash.tmp"
set /p REQHASH=<".venv\.reqhash.tmp"
del /q ".venv\.reqhash.tmp" 2>nul
set "MARKER=.venv\.deps-hash"
set "OLDHASH="
if exist "%MARKER%" set /p OLDHASH=<"%MARKER%"

if not "%REQHASH%"=="%OLDHASH%" (
  echo [EchoNote] 安装依赖...
  "%PY%" -m pip install -q --upgrade pip
  "%PY%" -m pip install -q -r requirements.txt
  if errorlevel 1 (
    echo [EchoNote] 依赖安装失败。
    echo             国内网络可设置镜像：set PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
    pause
    exit /b 1
  )
  >"%MARKER%" echo %REQHASH%
) else (
  echo [EchoNote] 依赖已就绪，跳过安装。
)

if "%ECHONOTE_HOST%"=="" set "ECHONOTE_HOST=127.0.0.1"
if "%ECHONOTE_PORT%"=="" set "ECHONOTE_PORT=8000"

echo.
echo ============================================
echo   EchoNote 声记 v0.9.0 技术预览
echo   地址: http://%ECHONOTE_HOST%:%ECHONOTE_PORT%
echo   模型就绪后会自动打开浏览器
echo   首次运行需要联网下载模型，之后可离线使用
echo ============================================
echo.

rem 先等 /api/ready 就绪，再打开浏览器（否则首屏会像“坏了”）。
start "" /b "%PY%" tools\wait_ready.py --host %ECHONOTE_HOST% --port %ECHONOTE_PORT% --open

"%PY%" -m app

pause
