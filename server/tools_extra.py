"""Расширения MCP: системная информация, файловые метаданные, дерево,
веб-загрузка, буфер обмена, статус сервера."""

from __future__ import annotations

import mimetypes
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import VERSION
from . import audit, config as cfg_mod, guard
from .tools_shell import JOBS

STARTED_AT = time.time()


def system_info() -> dict:
    """Информация о хосте и режиме доступа. Вызови первым, чтобы понять где ты."""
    mode = guard.mode_name()
    if mode == "workdir":
        mode_desc = f"рабочая папка (строго): {guard.WORKDIR}"
    elif mode == "full":
        mode_desc = "полный доступ от имени пользователя (новые папки подтверждать)"
    else:
        mode_desc = f"песочница: {', '.join(map(str, guard.ALLOWED_DIRS))}"
    info = {
        "ok": True,
        "app": "GlassMCP",
        "version": VERSION,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cwd_server": os.getcwd(),
        "mode": mode_desc,
        "workdir": str(guard.WORKDIR) if guard.WORKDIR else "",
        "allowed_dirs": (["* (весь диск)"] if guard.FULL_ACCESS
                         else [str(p) for p in guard.ALLOWED_DIRS]),
        "time_utc": datetime.now(timezone.utc).isoformat(),
    }
    if guard.workdir_missing():
        info["workdir_missing"] = True
        info["warning"] = (
            f"Рабочая папка {guard.WORKDIR} недоступна (удалена или переименована). "
            "Все операции заблокированы. Скажи пользователю: выбери новую папку "
            "в панели управления — там открылось обязательное окно выбора."
        )
    audit.audit("system_info", {}, True)
    return info


def file_info(path: str) -> dict:
    """Метаданные файла/папки: тип, размер, изменён, mime (без чтения содержимого)."""
    try:
        target = guard.resolve_path(path)
    except guard.SecurityError as e:
        audit.audit("file_info", {"path": path}, False, str(e))
        return guard.deny_payload(e)
    if not target.exists():
        audit.audit("file_info", {"path": path}, False, "not found")
        return {"ok": False, "error": f"не существует: {target}"}
    st = target.stat()
    mime = mimetypes.guess_type(target.name)[0]
    audit.audit("file_info", {"path": path}, True)
    return {"ok": True, "path": str(target),
            "type": "dir" if target.is_dir() else "file",
            "size": st.st_size, "mime": mime,
            "modified_utc": datetime.fromtimestamp(
                st.st_mtime, tz=timezone.utc).isoformat()}


def dir_tree(path: str = ".", depth: int = 2, max_entries: int = 200) -> dict:
    """Компактное дерево директории (глубина depth, без содержимого .venv/.git)."""
    from .tools_fs import SKIP_DIRS
    try:
        root = guard.resolve_path(path)
    except guard.SecurityError as e:
        audit.audit("dir_tree", {"path": path}, False, str(e))
        return guard.deny_payload(e)
    if not root.is_dir():
        audit.audit("dir_tree", {"path": path}, False, "not a dir")
        return {"ok": False, "error": f"не директория: {root}"}

    lines: list[str] = [str(root)]
    count = 1
    truncated = False

    def walk(dir_: Path, prefix: str, level: int) -> None:
        nonlocal count, truncated
        if level > depth or count >= max_entries:
            if count >= max_entries:
                truncated = True
            return
        try:
            children = sorted(dir_.iterdir(),
                              key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return
        for i, child in enumerate(children):
            if count >= max_entries:
                truncated = True
                return
            if child.is_dir() and child.name in SKIP_DIRS:
                continue
            connector = "└── " if i == len(children) - 1 else "├── "
            try:
                size = child.stat().st_size if child.is_file() else 0
            except OSError:
                size = 0
            label = child.name + ("/" if child.is_dir() else f"  ({size}B)")
            lines.append(prefix + connector + label)
            count += 1
            if child.is_dir():
                walk(child, prefix + ("    " if i == len(children) - 1 else "│   "),
                     level + 1)

    walk(root, "", 1)
    audit.audit("dir_tree", {"path": path}, True, f"{count} entries")
    return {"ok": True, "tree": "\n".join(lines), "truncated": truncated}


async def web_fetch(url: str, max_chars: int = 20000) -> dict:
    """Скачать текст по HTTP(S) и вернуть его (страницы, API, сырые файлы).
    Только http/https. Большие ответы обрезаются."""
    if not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "error": "только http:// и https:// URL"}
    try:
        import httpx
    except ImportError:
        return {"ok": False, "error": "httpx не установлен"}
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15,
                                     headers={"User-Agent": "GlassMCP/1.0"}) as client:
            resp = await client.get(url)
        text = resp.text[:max_chars]
        truncated = len(resp.text) > max_chars
        audit.audit("web_fetch", {"url": url[:300]}, resp.status_code < 400,
                    f"status={resp.status_code}")
        return {"ok": resp.status_code < 400, "url": str(resp.url),
                "status": resp.status_code,
                "content_type": resp.headers.get("content-type", ""),
                "content": text, "truncated": truncated}
    except Exception as e:  # noqa: BLE001
        audit.audit("web_fetch", {"url": url[:300]}, False, str(e))
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _clipboard_cmd() -> tuple[list[str], list[str]] | None:
    """(команда записи, команда чтения) для платформы."""
    if sys.platform == "darwin":
        return (["pbcopy"], ["pbpaste"])
    if cfg_mod.IS_WINDOWS:
        return (["powershell", "-NoProfile", "-Command", "$input | Set-Clipboard"],
                ["powershell", "-NoProfile", "-Command", "Get-Clipboard"])
    # Linux: пробуем Wayland, затем X11
    if subprocess.run(["which", "wl-copy"], capture_output=True).returncode == 0:
        return (["wl-copy"], ["wl-paste"])
    if subprocess.run(["which", "xclip"], capture_output=True).returncode == 0:
        return (["xclip", "-selection", "clipboard"],
                ["xclip", "-selection", "clipboard", "-o"])
    return None


def clipboard_write(text: str) -> dict:
    """Положить текст в буфер обмена этого ПК."""
    cmds = _clipboard_cmd()
    if not cmds:
        audit.audit("clipboard_write", {}, False, "no clipboard tool")
        return {"ok": False, "error": "буфер обмена недоступен: нет pbcopy/xclip/wl-copy"}
    try:
        p = subprocess.run(cmds[0], input=text.encode("utf-8"),
                           capture_output=True, timeout=5)
        audit.audit("clipboard_write", {}, p.returncode == 0,
                    f"{len(text)} chars")
        return {"ok": p.returncode == 0, "chars": len(text)}
    except OSError as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def clipboard_read() -> dict:
    """Прочитать текст из буфера обмена этого ПК."""
    cmds = _clipboard_cmd()
    if not cmds:
        audit.audit("clipboard_read", {}, False, "no clipboard tool")
        return {"ok": False, "error": "буфер обмена недоступен: нет pbpaste/xclip/wl-paste"}
    try:
        p = subprocess.run(cmds[1], capture_output=True, timeout=5)
        text = p.stdout.decode("utf-8", errors="replace")
        limit = int(cfg_mod.CONFIG["max_output_chars"])
        audit.audit("clipboard_read", {}, True, f"{len(text)} chars")
        return {"ok": True, "content": text[:limit],
                "truncated": len(text) > limit}
    except OSError as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def server_status() -> dict:
    """Статус самого сервера: версия, аптайм, режим, фоновые задачи."""
    running_jobs = sum(1 for j in JOBS.values() if j["status"] == "running")
    audit.audit("server_status", {}, True)
    return {"ok": True, "app": "GlassMCP", "version": VERSION,
            "uptime_s": int(time.time() - STARTED_AT),
            "mode": guard.mode_name(),
            "workdir": str(guard.WORKDIR) if guard.WORKDIR else "",
            "jobs_running": running_jobs,
            "token_set": bool(cfg_mod.CONFIG.get("token"))}
