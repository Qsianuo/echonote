@echo off
rem Starts EchoNote with the Hugging Face mirror, for networks where
rem huggingface.co is slow or unreachable. Same app, same local processing --
rem only the model download host changes.
cd /d "%~dp0"
EchoNote.exe --mirror
if errorlevel 1 pause
