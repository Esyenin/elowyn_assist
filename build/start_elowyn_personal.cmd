@echo off
setlocal

cd /d "%~dp0.."
title Elowyn Personal

if not exist ".venv\Scripts\python.exe" (
    echo Elowyn virtual environment was not found.
    echo Expected: %CD%\.venv\Scripts\python.exe
    echo.
    pause
    exit /b 1
)

if not exist "build\start_elowyn_personal.py" (
    echo Elowyn personal launcher was not found.
    echo.
    pause
    exit /b 1
)

echo Starting Elowyn Personal...
echo Keep this window open. Press Ctrl+C to stop Elowyn.
echo.

".venv\Scripts\python.exe" "build\start_elowyn_personal.py" %*
set "ELOWYN_EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%ELOWYN_EXIT_CODE%"=="0" (
    echo Elowyn stopped with an error. See the message above.
) else (
    echo Elowyn stopped.
)

if /I not "%~1"=="--check" pause
exit /b %ELOWYN_EXIT_CODE%
