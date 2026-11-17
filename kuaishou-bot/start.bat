@echo off
cd /d "%~dp0"
set PY=python
where python >nul 2>nul
if errorlevel 1 set PY=py

if not "%~1"=="" goto run

:menu
cls
echo ================================================
echo            Kuaishou ADB Tool
echo ================================================
echo   1. Status / device check
echo   2. Watch feed 10 min
echo   3. List conversations
echo   4. Read chat messages
echo   5. Send message
echo   6. Download chat video/image
echo   7. Record chat video (auto stop, 200MB cap)
echo   8. Backup login state
echo   9. Start local API server
echo  10. Sync chats into memory DB
echo  11. Ask a question over memory DB
echo   0. Exit
echo ================================================
set /p CH=Select: 
if "%CH%"=="1" goto s1
if "%CH%"=="2" goto s2
if "%CH%"=="3" goto s3
if "%CH%"=="4" goto s4
if "%CH%"=="5" goto s5
if "%CH%"=="6" goto s6
if "%CH%"=="7" goto s7
if "%CH%"=="8" goto s8
if "%CH%"=="9" goto s9
if "%CH%"=="10" goto s10
if "%CH%"=="11" goto s11
if "%CH%"=="0" exit /b
goto menu

:s1
%PY% kuaishou.py status
pause
goto menu

:s2
%PY% kuaishou.py watch --minutes 10
pause
goto menu

:s3
%PY% kuaishou.py msgs
pause
goto menu

:s4
set /p G=Group name: 
%PY% kuaishou.py read "%G%" -n 30
pause
goto menu

:s5
set /p G=Group name: 
set /p T=Text: 
%PY% kuaishou.py send "%G%" "%T%"
pause
goto menu

:s6
set /p G=Group name (empty = latest chat): 
%PY% kuaishou.py download "%G%"
pause
goto menu

:s7
set /p G=Group name (empty = current chat): 
%PY% kuaishou.py media "%G%" --mode clip
pause
goto menu

:s8
%PY% kuaishou.py backup
pause
goto menu

:s9
%PY% kuaishou.py api --port 8765
pause
goto menu

:s10
set /p G=Group name (empty = all chats): 
%PY% kuaishou.py memory sync "%G%"
pause
goto menu

:s11
set /p G=Group name: 
set /p Q=Question: 
%PY% kuaishou.py ask "%G%" "%Q%"
pause
goto menu

:run
%PY% kuaishou.py %*
pause
