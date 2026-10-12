@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Запуск Cheburcheck Multi на Windows
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 cheburcheck.py %*
  goto end
)
where python >nul 2>nul
if %errorlevel%==0 (
  python cheburcheck.py %*
  goto end
)
echo Python 3 не найден. Установите с https://www.python.org/downloads/ (отметьте "Add python.exe to PATH")
:end
pause
