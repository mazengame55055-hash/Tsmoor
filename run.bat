@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Kaggle Render Bot

if not exist ".venv\Scripts\python.exe" (
    echo [setup] Creating virtual environment...
    python -m venv .venv || (echo Python 3.10+ is required. Install it from python.org and tick "Add to PATH". & pause & exit /b 1)
)
call ".venv\Scripts\activate.bat"
echo [setup] Installing/updating dependencies...
pip install -q -r requirements.txt || (echo pip failed. & pause & exit /b 1)

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo.
    echo  Put your DISCORD_TOKEN in the .env file that is about to open, save it, then run run.bat again.
    notepad ".env"
    pause
    exit /b 0
)

:loop
echo [run] Starting bot...
python -m bot.main
if %errorlevel%==78 (
    echo [run] Fix the configuration above, then run run.bat again.
    pause
    exit /b 1
)
echo.
echo [run] Bot stopped. Restarting in 10 seconds... (close this window to quit)
timeout /t 10 >nul
goto loop
