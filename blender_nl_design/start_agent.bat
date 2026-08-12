@echo off
cd /d "%~dp0"
title Blender Design Agent (local backend - DeepSeek LLM)
echo Starting Blender Design Agent backend (DeepSeek)...
echo Listening at: http://127.0.0.1:8765/v1/agent/completion
echo Keep this window OPEN. If you close it, Blender cannot generate code.
echo.
"C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe" "D:\sheji_blend\blender_nl_design\agent_server.py"
echo.
echo Backend stopped.
pause
