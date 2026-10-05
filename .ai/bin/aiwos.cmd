@echo off
rem AI Work OS launcher for cmd / PowerShell.
setlocal
set "RT=%~dp0..\runtime"
if defined AIWOS_PYTHON ( "%AIWOS_PYTHON%" "%RT%\aiwos_main.py" %* & exit /b %errorlevel% )
where py >nul 2>nul && ( py -3 "%RT%\aiwos_main.py" %* & exit /b %errorlevel% )
where python3 >nul 2>nul && ( python3 "%RT%\aiwos_main.py" %* & exit /b %errorlevel% )
python "%RT%\aiwos_main.py" %*
