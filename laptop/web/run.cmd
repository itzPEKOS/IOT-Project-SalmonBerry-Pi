@echo off
rem Double-click to start, or: run.cmd -Lan   /   run.cmd -Sim   (same options as run.ps1)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
