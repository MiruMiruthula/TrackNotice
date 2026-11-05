@echo off
title TransitNotice
cd /d "%~dp0"
echo.
echo ===== TransitNotice - first start can take 2-3 minutes (installing packages) =====
where py >nul 2>nul && (set "PY=py") || (set "PY=python")
if not exist ".venv\Scripts\python.exe" (
  echo Creating Python environment...
  %PY% -m venv .venv
  if errorlevel 1 ( echo.& echo ERROR: Python 3.10+ is not installed or not on PATH. Install it from python.org and tick "Add Python to PATH". & pause & exit /b 1 )
)
call ".venv\Scripts\activate.bat"
echo Installing packages...
python -m pip install -q -r requirements.txt
if errorlevel 1 ( echo.& echo ERROR: package install failed - check your internet connection. & pause & exit /b 1 )
if not exist ".env" copy ".env.example" ".env" >nul
python manage.py initdb
echo.
echo Opening http://127.0.0.1:5000 in your browser in a few seconds.
echo The real notices load automatically in about 1-2 minutes (watch this window).
echo Admin page: http://127.0.0.1:5000/admin   token: see ADMIN_TOKEN in the .env file (default admin123)
echo Keep this window open. Press Ctrl+C to stop.
echo.
start "" cmd /c "timeout /t 5 >nul & start http://127.0.0.1:5000"
python manage.py run
pause
