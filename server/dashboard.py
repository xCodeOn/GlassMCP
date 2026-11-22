"""Панель управления: REST API + статика. Доступна ТОЛЬКО с 127.0.0.1.

Публичный туннель смотрит на MCP-порт, а не сюда, — но на всякий случай
запросы с CF-заголовками к API отклоняются и по содержимому.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.staticfiles import StaticFiles

from . import VERSION
from . import audit as audit_mod
from . import config as cfg_mod, guard, tunnel, zones
from .tools_extra import STARTED_AT

STATIC_DIR = Path(__file__).resolve().parent / "static"
SERVER_LOG = cfg_mod.BASE_DIR / "logs" / "server.log"

LOCAL_ONLY = True  # панели нужен только 127.0.0.1; тесты отключают host-проверку


def _json(data: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status)


def _err(msg: str, status: int = 400) -> JSONResponse:
    return _json({"ok": False, "error": msg}, status)


def _host_header_allowed(host_header: str | None) -> bool:
    """Host-заголовок должен быть локальным адресом панели (DNS rebinding)."""
    if not host_header:
        return True  # пустой Host не бывает у реальных клиентов; не блокируем прокси-тесты
    allowed = {
        f"127.0.0.1:{cfg_mod.CONFIG['dashboard_port']}",
        f"localhost:{cfg_mod.CONFIG['dashboard_port']}",
        f"[::1]:{cfg_mod.CONFIG['dashboard_port']}",
    }
    return host_header.lower() in allowed


class LocalOnlyMiddleware(BaseHTTPMiddleware):
    """CF-заголовки отклоняются всегда; host/peer — только локальные.
    Проверка Host закрывает DNS rebinding (страница с чужого домена не
    достучится до API даже с 127.0.0.1)."""

    async def dispatch(self, request: Request, call_next):
        if request.headers.get("cf-connecting-ip"):
            return _json({"ok": False, "error": "доступ через туннель запрещён"}, 403)
        if LOCAL_ONLY:
            host = request.client.host if request.client else ""
            if host not in ("127.0.0.1", "::1", "localhost"):
                return _json({"ok": False, "error": "панель доступна только с этого ПК"}, 403)
            if not _host_header_allowed(request.headers.get("host")):
                return _json({"ok": False, "error": "неизвестный Host"}, 403)
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        resp: Response = await call_next(request)
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; font-src 'self'; "
            "base-uri 'none'; frame-ancestors 'none'")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        return resp


# ---------- handlers ----------

async def api_status(request: Request) -> JSONResponse:
    cfg = cfg_mod.CONFIG
    tunnel_state = tunnel.MANAGER.status()
    return _json({
        "ok": True,
        "server": {
            "running": True,
            "version": VERSION,
            "uptime_s": int(time.time() - STARTED_AT),
            "mcp_host": str(cfg["host"]),
            "mcp_port": int(cfg["port"]),
            "token_set": bool(cfg.get("token")),
            "tools_count": 22,
        },
        "mode": guard.mode_name(),
        "workdir": str(guard.WORKDIR) if guard.WORKDIR else "",
        # папка удалена → панель покажет обязательную модалку выбора новой;
        # workdir не выбран и онбординг не пройден → модалка первого запуска
        "workdir_missing": guard.workdir_missing(),
        "onboarding_required": not guard.STRICT and not bool(cfg.get("onboarded")),
        "tunnel": tunnel_state,
        "public_url": str(cfg.get("public_url") or ""),
        "zones_count": len(zones.ZONES),
        "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })


async def api_config_get(request: Request) -> JSONResponse:
    cfg = cfg_mod.CONFIG
    token = str(cfg.get("token") or "")
    return _json({"ok": True, "config": {
        "token_masked": cfg_mod.mask_token(token),
        "token_full": token,
        "host": cfg["host"],
        "mcp_port": cfg["port"],
        "dashboard_port": cfg["dashboard_port"],
        "public_url": str(cfg.get("public_url") or ""),
        "workdir": str(cfg.get("workdir") or ""),
        "allowed_dirs": list(cfg["allowed_dirs"]),
        "max_file_bytes": cfg["max_file_bytes"],
        "max_output_chars": cfg["max_output_chars"],
        "default_shell_timeout": cfg["default_shell_timeout"],
        "max_shell_timeout": cfg["max_shell_timeout"],
        "deny_patterns": list(cfg["deny_patterns"]),
        "deny_paths": list(cfg["deny_paths"]),
        "audit_log": str(cfg.get("audit_log", "./audit.log")),
    }})


async def api_config_post(request: Request) -> JSONResponse:
    cfg = cfg_mod.CONFIG
    prev_workdir = str(cfg.get("workdir") or "")
    try:
        body: dict[str, Any] = await request.json()
    except Exception:  # noqa: BLE001
        return _err("тело запроса не JSON")
    new_token = ""
    restarting = False

    # рабочая папка
    if "workdir" in body:
        wd = body["workdir"]
        if wd is None or str(wd).strip() == "":
            cfg["workdir"] = ""
        else:
            p = Path(os.path.expanduser(str(wd).strip()))
            if not p.is_dir():
                return _err(f"папка не существует или не директория: {p}")
            try:
                resolved = p.resolve()
            except OSError as e:
                return _err(f"не резолвится: {e}")
            if resolved == cfg_mod.BASE_DIR or cfg_mod.BASE_DIR in resolved.parents:
                return _err("рабочей папкой нельзя назначать папку приложения")
            cfg["workdir"] = str(resolved)
            # папка выбрана → первичная настройка пройдена, модалку больше не форсируем
            cfg["onboarded"] = True

    # целые настройки
    for key in ("max_file_bytes", "max_output_chars",
                "default_shell_timeout", "max_shell_timeout"):
        if key in body:
            try:
                cfg[key] = int(body[key])
            except (TypeError, ValueError):
                return _err(f"{key}: ожидалось число")
    if "max_file_bytes" in body and "workdir" not in body:
        pass  # валидация в _validate

    # списки
    for key in ("deny_patterns", "deny_paths"):
        if key in body:
            val = body[key]
            if isinstance(val, str):
                val = [l.strip() for l in val.splitlines() if l.strip()]
            if not isinstance(val, list):
                return _err(f"{key}: ожидался список")
            val = [str(v) for v in val]
            if key == "deny_patterns":
                # невалидный regex не должен мутировать CONFIG (иначе
                # панель «кирпичится» до рестарта)
                import re as _re
                for pattern in val:
                    try:
                        _re.compile(pattern, _re.IGNORECASE)
                    except _re.error as e:
                        return _err(f"deny_patterns: плохой regex '{pattern}': {e}")
            cfg[key] = val
    if "allowed_dirs" in body and not (cfg.get("workdir")):
        val = body["allowed_dirs"]
        if isinstance(val, str):
            val = [l.strip() for l in val.replace(",", "\n").splitlines() if l.strip()]
        if not isinstance(val, list) or not val:
            return _err("allowed_dirs: непустой список")
        cfg["allowed_dirs"] = [str(v) for v in val]

    # порты → нужен рестарт процесса
    for key in ("mcp_port", "dashboard_port"):
        if key in body:
            try:
                cfg["port" if key == "mcp_port" else "dashboard_port"] = \
                    max(1, min(65535, int(body[key])))
            except (TypeError, ValueError):
                return _err(f"{key}: ожидалось число")
            restarting = True

    # свой внешний адрес (IP/домен) — только отображение, рестарт не нужен
    if "public_url" in body:
        try:
            cfg["public_url"] = cfg_mod.normalize_public_url(body["public_url"])
        except ValueError as e:
            return _err(f"public_url: {e}")

    if body.get("regenerate_token"):
        new_token = cfg_mod.regenerate_token(cfg)

    try:        guard.refresh(cfg)
    except guard.SecurityError as e:
        # fail-closed: откат к ПРЕЖНЕМУ workdir, а не к полному доступу
        cfg["workdir"] = prev_workdir
        guard.refresh(cfg)
        cfg_mod.save_config(cfg)
        return _err(str(e), 422)
    cfg_mod.save_config(cfg)
    audit_mod.audit("dashboard_config", {k: str(v)[:120] for k, v in body.items()},
                    True, f"restarting={restarting}")
    payload: dict[str, Any] = {"ok": True, "restarted": False,
                               "mode": guard.mode_name()}
    if new_token:
        payload["token"] = new_token
    if restarting:
        payload["restarting"] = True
        return JSONResponse(payload, background=BackgroundTask(_exec_restart))
    return _json(payload)


def _exec_restart() -> None:
    """Полный перезапуск процесса (execv): применит порты и всё остальное."""
    tunnel.MANAGER.shutdown()
    os.chdir(cfg_mod.BASE_DIR)
    os.execv(sys.executable, [sys.executable, "-m", "server"])


async def api_server_restart(request: Request) -> JSONResponse:
    return _json({"ok": True, "restarting": True},
                 background=BackgroundTask(_exec_restart))


async def api_zones(request: Request) -> JSONResponse:
    return _json({
        "ok": True,
        "mode": guard.mode_name(),
        "workdir": str(guard.WORKDIR) if guard.WORKDIR else "",
        "zones": [{"zone": z, "added_at": a} for z, a in sorted(zones.ZONES.items())],
    })


async def api_zones_revoke(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _err("тело запроса не JSON")
    zone = str(body.get("zone") or "").strip()
    if not zone:
        return _err("укажи zone")
    removed = zones.revoke(zone)
    audit_mod.audit("dashboard_revoke_zone", {"zone": zone}, removed)
    return _json({"ok": True, "removed": removed})


async def api_audit(request: Request) -> JSONResponse:
    try:
        tail = max(1, min(int(request.query_params.get("tail") or 200), 2000))
    except ValueError:
        return _err("tail: ожидалось число")
    lines = audit_mod.read_tail(tail)
    # сырые JSON-строки — их рендерит и фильтрует панель (parseAudit)
    return _json({"ok": True, "lines": lines, "file_exists": bool(lines)})


async def api_logs(request: Request) -> JSONResponse:
    tail = max(1, min(int(request.query_params.get("tail") or 200), 2000))
    try:
        if not SERVER_LOG.is_file():
            return _json({"ok": True, "lines": []})
        with open(SERVER_LOG, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 256_000))
            text = f.read().decode("utf-8", errors="replace")
        return _json({"ok": True, "lines": text.splitlines()[-tail:]})
    except OSError as e:
        return _json({"ok": False, "error": str(e)})


async def api_tunnel_start(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    mode = str(body.get("mode") or "quick")
    if mode != "quick":
        return _err("поддерживается mode=quick (именованный туннель — см. README)")
    cfg = cfg_mod.CONFIG
    result = tunnel.MANAGER.start_quick(int(cfg["port"]))
    return _json(result)


async def api_tunnel_stop(request: Request) -> JSONResponse:
    return _json(tunnel.MANAGER.stop())


async def api_tunnel_check(request: Request) -> JSONResponse:
    """Разовая живая проверка: стучимся на публичный URL /health."""
    if not tunnel.MANAGER.url:
        return _err("туннель не запущен — проверять нечего")
    return _json({"ok": True, "probe": tunnel.MANAGER.probe_now()})


async def api_browse(request: Request) -> JSONResponse:
    raw = request.query_params.get("path") or ""
    p = Path(os.path.expanduser(raw)) if raw.strip() else Path.home()
    if cfg_mod.IS_WINDOWS and len(raw.strip()) == 2 and raw.strip()[1] == ":":
        p = Path(raw.strip() + "\\")
    try:
        resolved = p.resolve()
    except OSError as e:
        return _err(f"не резолвится: {e}")
    if not resolved.is_dir():
        return _err("не директория")
    dirs: list[dict] = []
    try:
        for child in sorted(resolved.iterdir(), key=lambda c: c.name.lower()):
            if child.is_dir() and not child.name.startswith("."):
                # полный путь: панель шлёт его обратно при клике
                dirs.append({"name": child.name, "path": str(child)})
    except OSError as e:
        return _err(str(e), 403)
    parent = str(resolved.parent) if resolved.parent != resolved else None
    drives: list[str] = []
    if cfg_mod.IS_WINDOWS:
        import string
        drives = [f"{d}:\\" for d in string.ascii_uppercase
                  if Path(f"{d}:\\").exists()]
    return _json({"ok": True, "path": str(resolved), "parent": parent,
                  "dirs": dirs, "drives": drives})


async def api_connections(request: Request) -> JSONResponse:
    cfg = cfg_mod.CONFIG
    token = str(cfg.get("token") or "")
    tstate = tunnel.MANAGER.status()
    custom = str(cfg.get("public_url") or "")
    tunnel_url = tstate["url"] if tstate["running"] else ""
    return _json({
        "ok": True,
        "url_local": f"http://127.0.0.1:{cfg['port']}/mcp",
        "url_tunnel": tstate["url"],
        "tunnel_running": bool(tstate["running"]),
        # свой адрес имеет приоритет над туннельным: он постоянный
        "url_custom": custom,
        "url_external": custom or tunnel_url,
        "external_source": "custom" if custom else ("tunnel" if tunnel_url else ""),
        "token": token,
        "token_masked": cfg_mod.mask_token(token),
        "mode": guard.mode_name(),
        "workdir": str(guard.WORKDIR) if guard.WORKDIR else "",
    })


# ---------- сборка приложения ----------

def build_dashboard_app():
    from starlette.applications import Starlette
    from starlette.routing import Route

    routes = [
        Route("/api/status", api_status, methods=["GET"]),
        Route("/api/config", api_config_get, methods=["GET"]),
        Route("/api/config", api_config_post, methods=["POST"]),
        Route("/api/server/restart", api_server_restart, methods=["POST"]),
        Route("/api/zones", api_zones, methods=["GET"]),
        Route("/api/zones/revoke", api_zones_revoke, methods=["POST"]),
        Route("/api/audit", api_audit, methods=["GET"]),
        Route("/api/logs", api_logs, methods=["GET"]),
        Route("/api/tunnel/start", api_tunnel_start, methods=["POST"]),
        Route("/api/tunnel/stop", api_tunnel_stop, methods=["POST"]),
        Route("/api/tunnel/check", api_tunnel_check, methods=["POST"]),
        Route("/api/browse", api_browse, methods=["GET"]),
        Route("/api/connections", api_connections, methods=["GET"]),
    ]
    app = Starlette(routes=routes)
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    app.add_middleware(LocalOnlyMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    return app
