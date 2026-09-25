@echo off
setlocal
cd /d "%~dp0"

rem Prefer the project venv (one level up); fall back to python on PATH.
set "VENV=%~dp0..\.venv\Scripts\activate.bat"
if exist "%VENV%" (
    call "%VENV%"
) else (
    echo [WARN] venv not found: "%VENV%"
    echo [WARN] falling back to python on PATH.
)

python douyin_login_manager.py %*

echo.
echo Exit code: %ERRORLEVEL%
pause

endlocal
