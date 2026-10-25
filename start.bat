@echo off
rem Find-a-Line in the browser: double-click this file (needs uv, see setup-for-regular-folk.md)
cd /d "%~dp0"
uv run findaline.py web
pause
