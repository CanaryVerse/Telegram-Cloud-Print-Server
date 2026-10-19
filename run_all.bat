@echo off
setlocal
cd /d "%~dp0"

REM One process: start.py runs the printer and Telegram polling together.
REM Restart ONLY when Python exits with a non-zero code.
if not exist "start.py" goto missing
if not exist "telegram_listener.py" goto missing

where py >nul 2>nul
if %errorlevel%==0 goto use_py
where python >nul 2>nul
if %errorlevel%==0 goto use_python
echo [ERROR] Python not found. Install Python 3 and run install_requirements.bat.
pause
exit /b 1

:use_py
set "PYTHON_CMD=py -3"
goto launch
:use_python
set "PYTHON_CMD=python"
goto launch

:launch
echo Print server + Telegram. Close this window to stop.
:retry
echo.
echo Starting print server and Telegram...
%PYTHON_CMD% -u start.py --telegram
set "EXIT_CODE=%errorlevel%"
if "%EXIT_CODE%"=="0" goto done
REM Windows Ctrl+C / Ctrl+Break should stop, not trigger a restart.
if "%EXIT_CODE%"=="-1073741510" goto done
if "%EXIT_CODE%"=="3221225786" goto done
echo [WARN] Python exited with code %EXIT_CODE%. Restarting after 5 seconds...
timeout /t 5 /nobreak >nul
goto retry

:missing
echo [ERROR] Missing start.py or telegram_listener.py in %CD%
pause
exit /b 1

:done
endlocal
