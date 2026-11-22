#!/bin/bash
# GlassMCP: полный запуск (macOS) — двойной клик в Finder.
# Python → зависимости → сервер в этом окне → панель в браузере.
# Остановка: Ctrl+C в окне Terminal. Фоновый режим: ./run.sh --bg
cd "$(dirname "$0")"
exec ./run.sh "$@"
