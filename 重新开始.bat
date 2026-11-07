@echo off
chcp 65001 > nul
cd /d "%~dp0"
title WSC Demo - REBUILD

rem Fresh start: wipe both databases and generate 30 days of history.
python run_all.py --demo

if errorlevel 1 (
  echo.
  echo   Something FAILED - read the reason above.
  echo   Press any key to close this window...
  pause > nul
) else (
  timeout /t 2 /nobreak > nul
)
exit
