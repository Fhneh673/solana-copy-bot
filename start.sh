#!/usr/bin/env bash
# Запуск на Mac / Linux:  bash start.sh   (настройка заново: setup, проверка: check, пересборка ядра Rust: build)
cd "$(dirname "$0")" || exit 1

PY=$(command -v python3 || command -v python)
if [ -z "$PY" ]; then
  echo "[!] Python не найден. Установите Python 3.10+ с https://www.python.org/downloads/ и запустите снова."
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "Первый запуск: готовлю окружение, это займёт около минуты..."
  "$PY" -m venv .venv || exit 1
fi
if [ ! -f .venv/.deps_ok ]; then
  echo "Устанавливаю нужные библиотеки..."
  .venv/bin/python -m pip install -q -r requirements.txt || { echo "[!] Не удалось установить библиотеки."; exit 1; }
  touch .venv/.deps_ok
fi

if [ "$1" = "build" ]; then   # принудительная пересборка ядра; бота после неё не запускаем
  rm -f .venv/.rust_failed
  command -v cargo >/dev/null 2>&1 || { echo "[!] Rust не установлен: https://rustup.rs"; exit 1; }
  rm -f bin/copybot-core
  exec bash build_core.sh
fi
if [ ! -x bin/copybot-core ]; then
  if command -v cargo >/dev/null 2>&1; then
    if [ ! -f .venv/.rust_failed ]; then
      bash build_core.sh || touch .venv/.rust_failed
    fi
  elif [ ! -f .venv/.rust_hint ]; then
    echo "[i] Rust не установлен — бот работает на Python-движке. Для ускорения: https://rustup.rs , затем bash start.sh build"
    touch .venv/.rust_hint
  fi
fi

if [ "$1" = "setup" ] || [ ! -f config.json ]; then
  .venv/bin/python setup_wizard.py || exit 1
fi

if [ "$1" = "check" ]; then
  exec .venv/bin/python main.py --check
fi
exec .venv/bin/python main.py
