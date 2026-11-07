@echo off
chcp 65001 > nul
cd /d "%~dp0"
title WSC Demo - RUNNING

rem Continue from the data saved by the last run (no reset, keeps growing).
python run_all.py --open

if errorlevel 1 (
  echo.
  echo   Something FAILED - read the reason above.
  echo   Press any key to close this window...
  pause > nul
) else (
  timeout /t 2 /nobreak > nul
)
exit
