@echo off
rem Mikan RSS x qBittorrent Manager - quick launcher
rem Double-click to run. Args pass through, e.g.: mikan.bat --check-qb
chcp 65001 >nul
title Mikan RSS x qBittorrent Manager
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
py -3 mikan_qb_manager.py %*
echo.
pause
