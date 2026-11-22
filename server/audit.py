"""Аудит: каждый вызов инструмента пишется в журнал."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from . import config as cfg_mod


def audit(tool: str, args: dict[str, Any], ok: bool, note: str = "") -> None:
    try:
        cfg = cfg_mod.CONFIG
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "args": {k: str(v)[:500] for k, v in args.items()},
            "ok": bool(ok),
            "note": str(note)[:500],
        }
        path = cfg_mod.audit_log_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — аудит никогда не ломает основной вызов
        pass


def read_tail(tail: int = 200) -> list[str]:
    """Последние строки журнала (текстом, по одной JSON-записи на строку)."""
    try:
        path = cfg_mod.audit_log_path(cfg_mod.CONFIG)
        if not path.is_file():
            return []
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 512_000))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
        return [l for l in lines if l.strip()][-tail:]
    except Exception:  # noqa: BLE001
        return []
