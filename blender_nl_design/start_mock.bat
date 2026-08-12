@echo off
cd /d "%~dp0"
title Blender Design Agent (offline template mode)
echo Starting offline mock backend (no network, no API cost)...
echo Listening at: http://127.0.0.1:8765/v1/agent/completion
echo Keep this window OPEN.
echo.
"C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe" "D:\sheji_blend\blender_nl_design\mock_evolviq_agent.py"
echo.
echo Backend stopped.
pause
