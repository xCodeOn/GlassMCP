"""GlassMCP: точка входа. MCP-порт (наружу через туннель) + панель (127.0.0.1).

Запуск:  python -m server   (из папки APP)
Лаунчеры: ./run.sh (macOS/Linux), Start-Windows.bat (Windows)
Остановка: Ctrl+C в консоли; для фонового режима (--bg) — ./stop.sh
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from . import VERSION, APP_NAME
from . import audit as audit_mod
from . import config as cfg_mod
from . import guard, tools_extra, tools_fs, tools_shell, tunnel, zones

# ---------- конфиг и режимы ----------

_cfg = cfg_mod.load_config()
if cfg_mod.ensure_token(_cfg):
    # новый токен записан в .env — синхронизировать модульное состояние,
    # которое читают middleware, панель и инструменты
    cfg_mod.CONFIG.update(_cfg)
CONFIG = cfg_mod.CONFIG
guard.refresh(CONFIG)

TOOLS_COUNT = 22

MCP_INSTRUCTIONS = (
    "Coding agent on the owner's computer (GlassMCP). "
    "Call system_info first to learn the access mode. "
    "If mode is a workdir (рабочая папка): you can ONLY work inside that "
    "folder — anything outside returns 'вне рабочей папки'; do not retry outside it. "
    "If mode is full access: FIRST-TOUCH RULE — if any tool returns need_approval "
    "with a zone, STOP and ask the user in chat whether to grant access to that "
    "folder; only after an explicit yes call approve_zone(zone, confirm=true) "
    "and retry the original call. "
    "Always list a directory before editing. Prefer small fs_edit diffs. "
    "Prefer fs_grep/fs_glob before reading whole trees. Keep shell timeouts "
    "short; use shell_run_async for long builds and shell_poll for results."
)


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Bearer / X-API-Key / ?token= — на MCP-порте, кроме /health и /."""

    async def dispatch(self, request, call_next):
        if request.url.path in ("/health", "/"):
            return await call_next(request)
        token = str(cfg_mod.CONFIG.get("token") or "")
        if not token:
            return JSONResponse({"error": "server token not configured"}, status_code=500)
        auth = request.headers.get("authorization", "")
        api_key = request.headers.get("x-api-key", "")
        q_token = request.query_params.get("token", "")
        provided = ""
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
        elif api_key:
            provided = api_key.strip()
        elif q_token:
            provided = q_token.strip()
        if not provided or not secrets.compare_digest(provided, token):
            return JSONResponse(
                {"error": "unauthorized: передай заголовок Authorization: Bearer <TOKEN>"},
                status_code=401,
            )
        return await call_next(request)


def build_mcp_app():
    mcp = FastMCP(
        name="glassmcp",
        instructions=MCP_INSTRUCTIONS,
        host=str(CONFIG["host"]),
        port=int(CONFIG["port"]),
        streamable_http_path="/mcp",
        stateless_http=True,   # устойчивее через туннель: нет sticky-сессий
        json_response=True,    # plain JSON лучше проходит туннели
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False),
    )
    tools = [
        tools_extra.system_info,
        tools_fs.fs_list, tools_fs.fs_read, tools_fs.fs_write, tools_fs.fs_edit,
        tools_fs.fs_glob, tools_fs.fs_grep, tools_fs.fs_mkdir, tools_fs.fs_delete,
        tools_shell.shell_run, tools_shell.shell_run_async,
        tools_shell.shell_poll, tools_shell.shell_kill,
        tools_extra.file_info, tools_extra.dir_tree, tools_extra.web_fetch,
        tools_extra.clipboard_write, tools_extra.clipboard_read,
        tools_extra.server_status,
    ]
    zones_tools = [
        approve_zone, list_zones, revoke_zone,
    ]
    for fn in tools + zones_tools:
        mcp.tool()(fn)

    app = mcp.streamable_http_app()
    app.routes.append(Route("/health", health_endpoint, methods=["GET"]))
    app.routes.append(Route("/", index_endpoint, methods=["GET"]))
    app.add_middleware(TokenAuthMiddleware)
    return app


async def health_endpoint(request):
    # публичный эндпоинт: без путей и деталей песочницы
    return JSONResponse({
        "ok": True,
        "service": APP_NAME,
        "version": VERSION,
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "mode": guard.mode_name(),
        "tools_count": TOOLS_COUNT,
    })


async def index_endpoint(request):
    return PlainTextResponse(
        f"{APP_NAME} MCP v{VERSION}\n"
        "Streamable HTTP: POST /mcp  (Authorization: Bearer <TOKEN>, "
        "X-API-Key: <TOKEN> или ?token=<TOKEN>)\n"
        "Health: GET /health\n"
        "Панель управления: http://127.0.0.1:8090 (только с этого ПК)\n"
    )


# ---------- зоны как MCP-инструменты (нужен доступ к zones) ----------

def approve_zone(zone: str, confirm: bool = False) -> dict:
    """Подтвердить доступ к новой папке (зоне). Вызывай ТОЛЬКО после явного
    «да» пользователя в чате, с confirm=true. Потом повтори исходный вызов."""
    zone = zone.strip()
    if guard.STRICT:
        return {"ok": False,
                "error": "режим рабочей папки: зоны не используются, всё вне "
                         f"{guard.WORKDIR} запрещено"}
    if zones.is_approved(zone):
        return {"ok": True, "zone": zone, "already": True}
    if not confirm:
        return {"ok": False, "error": f"зона {zone} не подтверждена: нужен "
                                      f"approve_zone с confirm=true после согласия пользователя"}
    zones.approve(zone)
    audit_mod.audit("approve_zone", {"zone": zone}, True, "approved by user")
    return {"ok": True, "zone": zone, "hint": "доступ открыт и запомнен, повтори исходный вызов"}


def list_zones() -> dict:
    """Показать папки, доступ к которым уже подтверждён (вопросов не будет)."""
    if guard.STRICT:
        return {"ok": True, "mode": "workdir", "zones": [],
                "note": f"режим рабочей папки: доступен только {guard.WORKDIR}"}
    return {"ok": True, "zones": sorted(zones.ZONES)}


def revoke_zone(zone: str, confirm: bool = False) -> dict:
    """Отозвать доступ к папке: следующие обращения снова спросят пользователя."""
    zone = zone.strip()
    if guard.STRICT:
        return {"ok": False, "error": "режим рабочей папки: зоны не используются"}
    if not zones.is_approved(zone):
        return {"ok": True, "zone": zone, "already": "не была подтверждена"}
    if not confirm:
        return {"ok": False, "error": f"подтверди отзыв: revoke_zone(zone=\"{zone}\", confirm=true)"}
    zones.revoke(zone)
    audit_mod.audit("revoke_zone", {"zone": zone}, True, "revoked")
    return {"ok": True, "zone": zone, "hint": "доступ отозван, дальше будет спрашивать"}


# ---------- main ----------

async def run() -> None:
    import uvicorn

    from .dashboard import build_dashboard_app

    mcp_app = build_mcp_app()
    dash_app = build_dashboard_app()

    mcp_cfg = uvicorn.Config(mcp_app, host=str(CONFIG["host"]), port=int(CONFIG["port"]),
                             log_level="warning")
    dash_cfg = uvicorn.Config(dash_app, host="127.0.0.1",
                              port=int(CONFIG["dashboard_port"]), log_level="warning")
    servers = [uvicorn.Server(mcp_cfg), uvicorn.Server(dash_cfg)]

    loop = asyncio.get_running_loop()
    stop_signal = {"done": False}

    def _shutdown() -> None:
        if stop_signal["done"]:
            return
        stop_signal["done"] = True
        tunnel.MANAGER.shutdown()
        for s in servers:
            s.should_exit = True

    import signal

    def _install_handlers() -> None:
        """uvicorn при serve() сам ставит свои SIGINT/SIGTERM-хендлеры и
        затирает наши — поэтому переустанавливаем ПОСЛЕ старта серверов:
        наш хендлер гасит ОБА сервера и туннель, а не только последний."""
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _shutdown)
            except (NotImplementedError, RuntimeError):  # Windows
                signal.signal(sig, lambda *_: _shutdown())

    async def _wait_started_and_install():
        for _ in range(100):  # до ~10с
            if all(getattr(s, "started", False) for s in servers):
                break
            await asyncio.sleep(0.1)
        _install_handlers()

    installer = asyncio.create_task(_wait_started_and_install())

    host, port = CONFIG["host"], CONFIG["port"]
    dash_port = CONFIG["dashboard_port"]
    mode = guard.mode_name()
    _print_banner(str(host), int(port), int(dash_port), mode)
    sys.stdout.flush()

    await asyncio.gather(*(s.serve() for s in servers), installer)
    # штатный (несигнальный) выход — тоже гасим туннель
    tunnel.MANAGER.shutdown()
    print("\n✔ сервер остановлен — туннель и оба порта закрыты")
    sys.stdout.flush()


def _print_banner(host: str, port: int, dash_port: int, mode: str) -> None:
    """Аккуратный стартовый баннер; цвета — только в терминале (NO_COLOR учитывается)."""
    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    def paint(text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if color else text

    width = 64
    pad = width - 4  # "║ " + текст + " ║"

    def row(text: str = "") -> str:
        if len(text) > pad:
            text = text[: pad - 1] + "…"
        return "║ " + text.ljust(pad) + " ║"

    def kv(label: str, value: str) -> str:
        return row(f"{label:<18} {value}")

    if mode == "workdir":
        if guard.workdir_missing():
            mode_text = "рабочая папка НЕДОСТУПНА — выбери новую в панели"
        else:
            mode_text = f"рабочая папка: {guard.WORKDIR}"
    elif mode == "full":
        mode_text = "полный доступ (новые папки подтверждать)"
    else:
        mode_text = f"песочница: {', '.join(map(str, guard.ALLOWED_DIRS))}"

    print()
    print(paint("╔" + "═" * (width - 2) + "╗", "36"))
    print(paint(row(f"{APP_NAME} v{VERSION}"), "1;36"))
    print(paint("╠" + "═" * (width - 2) + "╣", "36"))
    print(kv("Панель управления:", f"http://127.0.0.1:{dash_port}"))
    print(kv("MCP адрес:", f"http://{host}:{port}/mcp"))
    print(kv("Режим:", mode_text))
    print(kv("Инструментов:", str(TOOLS_COUNT)))
    print(paint("╠" + "═" * (width - 2) + "╣", "36"))
    print(paint(kv("Остановка:", "Ctrl+C — остановит сервер и туннель"), "1;33"))
    print(paint("╚" + "═" * (width - 2) + "╝", "36"))


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        tunnel.MANAGER.shutdown()
        print("\n✔ сервер остановлен")


if __name__ == "__main__":
    main()
