@echo off
setlocal
cd /d "%~dp0"

call "E:\AIcode\.venv\Scripts\activate.bat"

python launcher.py %*

echo.
echo Exit code: %ERRORLEVEL%
pause

endlocal
