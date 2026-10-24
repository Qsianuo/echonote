@echo off
chcp 65001 >nul
cd /d "%~dp0"
title EchoNote - 手机访问模式
".venv\Scripts\python.exe" tools\serve_lan.py %*
echo.
echo 服务已退出。窗口可关闭。
pause
