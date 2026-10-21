@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 exit /b 1
)
".venv\Scripts\python.exe" -c "import pyarrow; assert pyarrow.__version__ == '26.0.0'" >nul 2>&1
if errorlevel 1 (
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 exit /b 1
)
if "%~1"=="" (
    ".venv\Scripts\python.exe" archive.py --help
    echo.
    echo Example: fide-archive.bat export data\fide-2025.bin --output exports\fide-2025.sqlite
    pause
    exit /b 0
)
".venv\Scripts\python.exe" archive.py %*
exit /b %errorlevel%
