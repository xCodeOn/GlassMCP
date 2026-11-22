#!/bin/bash
# GlassMCP: остановить сервер и туннель (macOS/Linux).
# Нужен для фонового режима (--bg); сервер в консоли останавливается по Ctrl+C.
cd "$(dirname "$0")"

for pidfile in .server_pid .tunnel_pid; do
  if [ -f "$pidfile" ]; then
    PID=$(cat "$pidfile")
    if kill -0 "$PID" 2>/dev/null; then
      kill "$PID" 2>/dev/null
      for i in 1 2 3 4 5; do kill -0 "$PID" 2>/dev/null || break; sleep 0.4; done
      kill -9 "$PID" 2>/dev/null || true
      echo "остановлен ($pidfile: $PID)"
    fi
    rm -f "$pidfile"
  fi
done

# pid-файлы — единственный источник правды: pkill по маске мог бы задеть
# туннели других проектов, поэтому не используем.

DASH_PORT=$(./.venv/bin/python -c "import yaml; print(yaml.safe_load(open('config.yaml')).get('dashboard_port', 8090))" 2>/dev/null || echo 8090)
curl -s -m 1 "http://127.0.0.1:$DASH_PORT/api/status" >/dev/null 2>&1 \
  && echo "внимание: панель ещё отвечает" \
  || echo "готово: всё молчит"
