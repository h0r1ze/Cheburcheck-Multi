#!/usr/bin/env sh
# Запуск Cheburcheck Multi на Linux/macOS
cd "$(dirname "$0")" || exit 1
if command -v python3 >/dev/null 2>&1; then exec python3 cheburcheck.py "$@"; fi
if command -v python >/dev/null 2>&1; then exec python cheburcheck.py "$@"; fi
echo "Python 3 не найден. Установите: sudo apt install python3 (или dnf install python3)"
exit 1
