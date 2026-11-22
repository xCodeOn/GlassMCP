"""Зоны доступа: «спрашивать только про новую папку верхнего уровня».

Используются в режиме полного доступа. В режиме рабочей папки зоны
не нужны — jail применяется вместо них.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import config as cfg_mod

ZONES_FILE = cfg_mod.BASE_DIR / "approved_zones.json"


def zone_of(resolved: Path) -> str:
    """Зона = папка верхнего уровня.

    macOS/Linux: /Users/<имя>/<папка> или /<раздел> (/etc, /tmp).
    Windows:     C:\\Users\\<имя>\\<папка> или C:\\<папка>.
    """
    parts = resolved.parts
    if len(parts) >= 4 and parts[1].lower() == "users":
        return str(Path(*parts[:4]))
    if len(parts) >= 2:
        return str(Path(*parts[:2]))
    return str(resolved)


def _seed_zones() -> dict[str, str]:
    """Стартовая знакомая папка — Рабочий стол. Остальное спросит при первом касании."""
    return {str(Path.home() / "Desktop"): "seed"}


def load_zones() -> dict[str, str]:
    if ZONES_FILE.is_file():
        try:
            data = json.loads(ZONES_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
            if isinstance(data, list):  # legacy-формат (список строк)
                return {str(z): "legacy" for z in data}
        except (json.JSONDecodeError, OSError):
            pass
    zones = _seed_zones()
    save_zones(zones)
    return zones


def save_zones(zones: dict[str, str]) -> None:
    try:
        ZONES_FILE.write_text(
            json.dumps(dict(sorted(zones.items())), ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    except OSError:
        pass


ZONES: dict[str, str] = load_zones()


def is_approved(zone: str) -> bool:
    return zone in ZONES


def approve(zone: str) -> bool:
    """True если зона новая (была только что добавлена)."""
    if zone in ZONES:
        return False
    from datetime import datetime, timezone
    ZONES[zone] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_zones(ZONES)
    return True


def revoke(zone: str) -> bool:
    if zone in ZONES:
        del ZONES[zone]
        save_zones(ZONES)
        return True
    return False
