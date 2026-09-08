@echo off
rem Stop TeleOps. Keep this file pure ASCII: cmd.exe reads .bat using the
rem system ANSI codepage, so UTF-8 text here would be parsed as garbage.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\stop.ps1"
pause
