@echo off
setlocal

cd /d "%~dp0"

set "APP_ROOT=%~dp0"
set "APP_PYW=%APP_ROOT%src\main.pyw"
set "IMPORT_CHECK=import pystray, PIL, win32gui, psutil, pynvml, clr, starlette, uvicorn, websockets"
set "PYTHON_EXE=%APP_ROOT%.venv\Scripts\python.exe"
set "PYTHONW_EXE=%APP_ROOT%.venv\Scripts\pythonw.exe"

if not exist "%PYTHON_EXE%" goto :missing_venv

"%PYTHON_EXE%" -c "%IMPORT_CHECK%" >nul 2>&1
if errorlevel 1 goto :missing_deps

if exist "%PYTHONW_EXE%" (
	start "" "%PYTHONW_EXE%" "%APP_PYW%"
) else (
	start "" "%PYTHON_EXE%" "%APP_PYW%"
)
exit /b 0

:missing_venv
echo Little Helper failed to start.
echo.
echo Project virtual environment not found:
echo   %PYTHON_EXE%
echo.
echo Create it with:
echo   C:/Users/Marklalon/AppData/Local/Programs/Python/Python310/python.exe -m venv .venv
echo   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
goto :fail

:missing_deps
echo Little Helper failed to start.
echo.
echo Required packages are missing from project virtual environment:
echo   %PYTHON_EXE%
echo.
echo Install them with:
echo   .\.venv\Scripts\python.exe -m pip install -r requirements.txt

:fail
echo.
echo For troubleshooting, run:
echo   "%PYTHON_EXE%" "%APP_PYW%"
pause
exit /b 1
