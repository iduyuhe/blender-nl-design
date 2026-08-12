@echo off
REM NL Blender Designer - C3 语音模块一键安装（Windows）
REM 在 blender_nl_design 目录内运行本文件。
setlocal
set PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe
set VENV=C:\Users\Administrator\.workbuddy\binaries\python\envs\voice

if not exist "%VENV%\Scripts\python.exe" (
  "%PY%" -m venv "%VENV%"
)
call "%VENV%\Scripts\pip.exe" install --upgrade pip
call "%VENV%\Scripts\pip.exe" install vosk sounddevice numpy
"%VENV%\Scripts\python.exe" voice_setup.py

echo.
echo 语音模块安装完成。重启 Blender 后在 NL Design 面板即可使用「语音输入」。
pause
