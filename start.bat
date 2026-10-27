@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Solana Copy Bot

where python >nul 2>nul
if errorlevel 1 (
  echo [!] Python не найден.
  echo     Скачайте Python с https://www.python.org/downloads/
  echo     При установке отметьте галочку "Add Python to PATH", затем запустите этот файл снова.
  pause
  exit /b 1
)

if not exist .venv\Scripts\python.exe (
  echo Первый запуск: готовлю окружение, это займёт около минуты...
  python -m venv .venv
)
if not exist .venv\.deps_ok (
  echo Устанавливаю нужные библиотеки...
  .venv\Scripts\python.exe -m pip install -q -r requirements.txt
  if errorlevel 1 (
    echo [!] Не удалось установить библиотеки. Проверьте интернет и запустите снова.
    pause
    exit /b 1
  )
  echo ok> .venv\.deps_ok
)

if /i "%1"=="build" (
  del /q .venv\.rust_failed 2>nul
  del /q bin\copybot-core.exe 2>nul
  where cargo >nul 2>nul
  if errorlevel 1 (
    echo [!] Rust не установлен: https://rustup.rs
    pause
    exit /b 1
  )
  call build_core.bat
  if errorlevel 1 (
    pause
    exit /b 1
  )
  pause
  exit /b 0
)
if not exist bin\copybot-core.exe (
  where cargo >nul 2>nul
  if errorlevel 1 (
    if not exist .venv\.rust_hint (
      echo [i] Rust не установлен, бот будет работать на Python-движке.
      echo     Для ускорения установите Rust с https://rustup.rs и запустите start.bat build
      echo ok> .venv\.rust_hint
    )
  ) else (
    if not exist .venv\.rust_failed (
      call build_core.bat
      if errorlevel 1 echo failed> .venv\.rust_failed
    )
  )
)

if /i "%1"=="setup" goto setup
if not exist config.json goto setup
goto run

:setup
.venv\Scripts\python.exe setup_wizard.py
if errorlevel 1 (
  pause
  exit /b 1
)

:run
if /i "%1"=="check" (
  .venv\Scripts\python.exe main.py --check
) else (
  .venv\Scripts\python.exe main.py
)
echo.
pause
