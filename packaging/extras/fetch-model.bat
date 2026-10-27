@echo off
rem 一次性下载全部模型（语音 + 离线翻译），之后 EchoNote 可以完全断网使用。
rem 只做这一件事：把模型放到 .\models\ 下。
rem 只想要某一个：fetch-model.bat --model mt-zh-en
cd /d "%~dp0"
echo.
echo  正在准备模型（语音约 1.6 GB，离线翻译约 160 MB），请保持网络连接...
echo.
EchoNote.exe --fetch-model %*
echo.
if errorlevel 1 pause
