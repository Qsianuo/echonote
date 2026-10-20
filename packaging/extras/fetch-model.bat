@echo off
rem 一次性下载语音模型，之后 EchoNote 可以完全断网使用。
rem 只做这一件事：把模型放到 .\models\ 下。
cd /d "%~dp0"
echo.
echo  正在准备模型（约 1.6 GB），请保持网络连接...
echo.
EchoNote.exe --fetch-model %*
echo.
if errorlevel 1 pause
