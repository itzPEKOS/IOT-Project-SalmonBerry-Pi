@echo off
rem Clear test data once before collecting real data. Shows everything first and asks before each delete.
rem   clear_test_data.cmd           ask for each part
rem   clear_test_data.cmd -DryRun   show only, delete nothing
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0clear_test_data.ps1" %*
pause
