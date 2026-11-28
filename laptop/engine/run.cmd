@echo off
rem Double-click to start the game engine (reads the broker IP from laptop\infra\.env).
rem Options: run.cmd --skip-checks   /   run.cmd --host 172.20.10.2   (see: run.cmd --help)
cd /d "%~dp0.."
python -m engine %*
pause
