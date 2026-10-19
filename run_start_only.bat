@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 goto use_py
where python >nul 2>nul
if %errorlevel%==0 goto use_python
echo [ERROR] Python not found.
pause
exit /b 1
:use_py
set "PYTHON_CMD=py -3"
goto launch
:use_python
set "PYTHON_CMD=python"
goto launch
:launch
:retry
%PYTHON_CMD% -u start.py
set "EXIT_CODE=%errorlevel%"
if "%EXIT_CODE%"=="0" goto done
if "%EXIT_CODE%"=="-1073741510" goto done
if "%EXIT_CODE%"=="3221225786" goto done
echo [WARN] Python exited with code %EXIT_CODE%. Restarting after 5 seconds...
timeout /t 5 /nobreak >nul
goto retry
:done
endlocal
