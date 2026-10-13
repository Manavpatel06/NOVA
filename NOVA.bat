@echo off
rem NOVA -- console launcher (visible logs). For silent/tray launch use the
rem Start Menu shortcut created by install.ps1.
title NOVA
cd /d D:\NOVA
".venv\Scripts\python.exe" -m nova %*
if errorlevel 1 (
  echo.
  echo NOVA exited with an error -- the messages above have the details.
  pause
)
