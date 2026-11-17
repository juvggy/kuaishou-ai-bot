@echo off
cd /d "%~dp0"
title Kefu Bot

echo ========================================
echo   Kuaishou Customer Bot
echo ========================================
echo.
adb connect 127.0.0.1:16384 >nul 2>nul

netstat -ano | findstr /C:":8800" | findstr /C:"LISTENING" >nul
if not errorlevel 1 goto open

echo starting as background task (keeps running after this window closes)...
schtasks /Query /TN "KefuBot" >nul 2>nul
if errorlevel 1 goto direct
schtasks /Run /TN "KefuBot" >nul 2>nul
goto wait

:direct
start "" /min python -u app.py
goto wait

:wait
for /L %%i in (1,1,20) do (
  ping -n 2 127.0.0.1 >nul
  netstat -ano | findstr /C:":8800" | findstr /C:"LISTENING" >nul
  if not errorlevel 1 goto open
)
echo 8800 did not come up, check emulator / adb / python
pause
goto end

:open
echo backend ready: http://127.0.0.1:8800
echo.
python tui.py

:end
