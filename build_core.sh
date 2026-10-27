#!/usr/bin/env bash
# Сборка быстрого ядра на Rust:  bash build_core.sh
cd "$(dirname "$0")/core" || exit 1
if ! command -v cargo >/dev/null 2>&1; then
  echo "[!] Rust не установлен. Установите: https://rustup.rs и запустите снова."
  exit 1
fi
echo "Собираю быстрое ядро на Rust. Первый раз это занимает 2-5 минут..."
cargo build --release || { echo "[!] Сборка не удалась. Бот будет работать на Python-движке."; exit 1; }
mkdir -p ../bin && cp target/release/copybot-core ../bin/ && chmod +x ../bin/copybot-core
echo "Готово: bin/copybot-core"
