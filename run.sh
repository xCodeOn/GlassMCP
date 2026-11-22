#!/bin/bash
# GlassMCP: запуск (macOS/Linux). Ничего не ставит в систему:
# портативный Python → ./python, зависимости → .venv, cloudflared → ./bin.
#
#   ./run.sh            — запуск в этой консоли, остановка: Ctrl+C
#   ./run.sh --bg       — фоновый запуск + открыть панель в браузере
#   ./run.sh --tunnel   — сразу поднять Cloudflare-туннель (в любом режиме)
#   ./stop.sh           — остановить сервер, запущенный с --bg
set -e
cd "$(dirname "$0")"
APP_DIR="$PWD"

PY_TAG="20261110"       # сборка python-build-standalone (pinned)
PY_VER="3.12.16"

# ---------- цвета и красивый вывод (только в терминале) ----------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
  C_CYAN=$'\033[36m'; C_GREEN=$'\033[32m'; C_RED=$'\033[31m'
else
  C_RESET=""; C_BOLD=""; C_DIM=""; C_CYAN=""; C_GREEN=""; C_RED=""
fi
step() { printf '%s→%s %s\n' "$C_CYAN" "$C_RESET" "$*"; }
ok()   { printf '%s✔%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
fail() { printf '%s✖ %s%s\n' "$C_RED" "$*" "$C_RESET" >&2; }

# ---------- флаги ----------
FLAG_TUNNEL=0
FLAG_BG=0
for arg in "$@"; do
  case "$arg" in
    --tunnel) FLAG_TUNNEL=1 ;;
    --bg) FLAG_BG=1 ;;
    --fg) ;;  # совместимость: foreground теперь режим по умолчанию
    *) fail "неизвестный флаг: $arg"
       printf 'использование: ./run.sh [--bg] [--tunnel]\n'; exit 2 ;;
  esac
done

# ---------- 1) Python: портативный в ./python, иначе системный ----------
pick_python() {
  if [ -x "./python/bin/python3" ]; then echo "./python/bin/python3"; return; fi
  if [ -x "./python/python.exe" ]; then echo "./python/python.exe"; return; fi
  if command -v python3 >/dev/null 2>&1; then echo "python3"; return; fi
  echo ""
}

PY=$(pick_python)
if [ -z "$PY" ]; then
  step "Python не найден — скачиваю портативный..."
  ARCH="$(uname -m)"
  case "$(uname -s)-$ARCH" in
    Darwin-arm64)  ASSET="cpython-${PY_VER}%2B${PY_TAG}-aarch64-apple-darwin-install_only.tar.gz" ;;
    Darwin-x86_64) ASSET="cpython-${PY_VER}%2B${PY_TAG}-x86_64-apple-darwin-install_only.tar.gz" ;;
    Linux-x86_64)  ASSET="cpython-${PY_VER}%2B${PY_TAG}-x86_64-unknown-linux-gnu-install_only.tar.gz" ;;
    Linux-aarch64) ASSET="cpython-${PY_VER}%2B${PY_TAG}-aarch64-unknown-linux-gnu-install_only.tar.gz" ;;
    *) fail "нет готовой сборки для $(uname -s)-$ARCH"; exit 1 ;;
  esac
  URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_TAG}/${ASSET}"
  mkdir -p .pydl && rm -rf .pydl/*
  curl -sL --fail -o .pydl/py.tgz "$URL" || { fail "не скачать Python (проверь сеть)"; exit 1; }
  tar xzf .pydl/py.tgz -C .pydl || { fail "архив повреждён"; exit 1; }
  rm -rf python && mv .pydl/python python && rm -rf .pydl
  PY="./python/bin/python3"
  ok "портативный Python $PY_VER готов"
fi

# ---------- 2) venv и зависимости ----------
# .venv может быть битым (например, после переноса папки с ПК на ПК):
# symlink python3 указывает в старый путь — пересоздаём из локального Python.
if [ ! -x ./.venv/bin/python ]; then
  [ -d ./.venv ] && step "venv повреждён — пересоздаю..." || step "создаю виртуальное окружение..."
  rm -rf .venv
  "$PY" -m venv .venv
fi
./.venv/bin/python -m pip install -q --upgrade pip 2>/dev/null || true
step "проверяю зависимости..."
./.venv/bin/pip install -q -r requirements.txt
ok "зависимости в порядке"

# ---------- 3) токен ----------
if [ ! -f .env ] || ! grep -q "^MCP_TOKEN=" .env; then
  TOK=$(./.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(32))")
  # >> а не >: не терять другие ключи существующего .env (MCP_PORT и т.п.)
  echo "MCP_TOKEN=$TOK" >> .env
  chmod 600 .env 2>/dev/null || true
  ok "сгенерирован новый токен (.env)"
fi

mkdir -p logs

DASH_PORT=$(./.venv/bin/python -c "import yaml; print(yaml.safe_load(open('config.yaml')).get('dashboard_port', 8090))" 2>/dev/null || echo 8090)
MCP_PORT=$(./.venv/bin/python -c "import yaml; print(yaml.safe_load(open('config.yaml')).get('port', 8000))" 2>/dev/null || echo 8000)

# если уже запущен — не дублируем
if [ -f .server_pid ] && kill -0 "$(cat .server_pid)" 2>/dev/null; then
  ok "уже запущен (pid $(cat .server_pid)). Панель: http://127.0.0.1:$DASH_PORT"
  "$PY" -c "import webbrowser; webbrowser.open('http://127.0.0.1:$DASH_PORT')" 2>/dev/null || true
  exit 0
fi

open_browser() {
  if command -v open >/dev/null 2>&1; then
    open "http://127.0.0.1:$DASH_PORT" || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "http://127.0.0.1:$DASH_PORT" || true
  else
    "$PY" -c "import webbrowser; webbrowser.open('http://127.0.0.1:$DASH_PORT')" 2>/dev/null || true
  fi
}

start_tunnel_when_up() {
  for i in $(seq 1 40); do
    sleep 0.5
    curl -s -m 1 "http://127.0.0.1:$DASH_PORT/api/status" >/dev/null 2>&1 && break
  done
  curl -s -m 10 -X POST "http://127.0.0.1:$DASH_PORT/api/tunnel/start" \
    -H "Content-Type: application/json" -d '{"mode":"quick"}' >/dev/null 2>&1 || true
}

# ---------- 4) foreground (по умолчанию): Ctrl+C останавливает всё ----------
if [ "$FLAG_BG" = "0" ]; then
  if [ "$FLAG_TUNNEL" = "1" ]; then
    step "туннель поднимется сразу после старта — адрес появится в панели"
    start_tunnel_when_up &
  fi
  # браузер открываем только в интерактивном терминале, с паузой на старт
  if [ -t 1 ]; then
    ( sleep 2; open_browser ) &
  fi
  exec ./.venv/bin/python -m server
fi

# ---------- 5) фоновый режим (--bg): остановка через ./stop.sh ----------
step "запускаю сервер в фоне..."
nohup ./.venv/bin/python -m server >> logs/server.log 2>&1 &
echo $! > .server_pid

# ждём панель, туннель и открываем
for i in $(seq 1 30); do
  sleep 0.5
  curl -s -m 1 "http://127.0.0.1:$DASH_PORT/api/status" >/dev/null 2>&1 && break
done

if [ "$FLAG_TUNNEL" = "1" ]; then
  step "поднимаю туннель..."
  start_tunnel_when_up
fi

open_browser

printf '\n'
ok "${C_BOLD}GlassMCP запущен в фоне${C_RESET} (pid $(cat .server_pid))"
printf '  %sпанель:%s  %shttp://127.0.0.1:%s%s\n' "$C_BOLD" "$C_RESET" "$C_CYAN" "$DASH_PORT" "$C_RESET"
printf '  %sMCP:%s     %shttp://127.0.0.1:%s/mcp%s\n' "$C_BOLD" "$C_RESET" "$C_CYAN" "$MCP_PORT" "$C_RESET"
if [ "$FLAG_TUNNEL" = "1" ]; then
  printf '  %sтуннель:%s адрес появится в панели (секция «Туннель»), до ~30 секунд\n' "$C_BOLD" "$C_RESET"
fi
printf '  %sстоп:%s    ./stop.sh\n' "$C_BOLD" "$C_RESET"
printf '  %sлоги:%s    logs/server.log\n' "$C_DIM" "$C_RESET"
