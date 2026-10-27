@echo off
chcp 65001 >nul
cd /d "%~dp0core"
where cargo >nul 2>nul
if errorlevel 1 (
  echo [!] Rust не установлен. Скачайте с https://rustup.rs и запустите этот файл снова.
  exit /b 1
)
echo Собираю быстрое ядро на Rust. Первый раз это занимает 2-5 минут...
cargo build --release
if errorlevel 1 (
  echo [!] Сборка не удалась. Бот будет работать на Python-движке.
  exit /b 1
)
if not exist ..\bin mkdir ..\bin
copy /y target\release\copybot-core.exe ..\bin\ >nul
echo Готово: bin\copybot-core.exe
