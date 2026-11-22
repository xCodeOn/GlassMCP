"""Проверка путей и команд — ядро безопасности.

Три режима (переключаются из панели, читаются из config.workdir):

- **strict** (режим рабочей папки): ВСЕ пути fs-инструментов обязаны
  резолвиться строго внутрь рабочей папки (realpath, симлинки и `..`
  раскрываются — выйти через них нельзя). shell выполняется с cwd=папка,
  абсолютные пути наружу в командах отклоняются (best-effort, честно
  задокументировано в README). Зоны не используются. Если папку удалили —
  работа блокируется (workdir_missing) до выбора новой в панели.
- **full** (allowed_dirs содержит "*"): как в Claude Code — весь диск,
  защита: подтверждение новых папок (зоны) + audit.log.
- **list** (allowed_dirs — список путей): jail по списку + зоны.

deny_patterns (regex на команды) и deny_paths (компоненты пути)
применяются во всех режимах.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from . import config as cfg_mod, zones

# ---------- ошибки ----------

class SecurityError(ValueError):
    pass


class NeedApprovalError(SecurityError):
    """Новая зона: агент должен спросить пользователя, вызвать
    approve_zone(confirm=true) и повторить исходный вызов."""

    def __init__(self, zone: str):
        super().__init__(f"новая папка: {zone}")
        self.zone = zone


# ---------- динамическое состояние (обновляется из панели) ----------

WORKDIR: Path | None = None
STRICT: bool = False
FULL_ACCESS: bool = True
ALLOWED_DIRS: list[Path] = []
DENY_RES: list[re.Pattern[str]] = []
DENY_PATH_PARTS: list[str] = []


def refresh(cfg: dict | None = None) -> None:
    """Пересчитать режимы/джейлы из CONFIG. Вызывается при старте и из панели."""
    cfg = cfg or cfg_mod.CONFIG
    global WORKDIR, STRICT, FULL_ACCESS, ALLOWED_DIRS, DENY_RES, DENY_PATH_PARTS
    wd = str(cfg.get("workdir") or "").strip()
    if wd:
        p = Path(os.path.expanduser(wd))
        try:
            WORKDIR = p.resolve()
        except OSError:
            WORKDIR = p.absolute()
        # Папка может отсутствовать (удалили/переименовали) — это НЕ повод
        # ронять запуск: панель должна подняться и предложить выбрать новую.
        # Недоступность ловится динамически через workdir_missing().
        STRICT = True
        FULL_ACCESS = False
        ALLOWED_DIRS = [WORKDIR]
    else:
        WORKDIR = None
        STRICT = False
        dirs = list(cfg.get("allowed_dirs") or ["*"])
        FULL_ACCESS = "*" in dirs or bool(cfg.get("allow_all_paths"))
        ALLOWED_DIRS = [
            Path(os.path.expanduser(p)).resolve() for p in dirs if p != "*"
        ]
    DENY_RES = [re.compile(p, re.IGNORECASE) for p in cfg.get("deny_patterns", [])]
    DENY_PATH_PARTS = list(cfg.get("deny_paths", []))


def mode_name() -> str:
    if STRICT:
        return "workdir"
    return "full" if FULL_ACCESS else "list"


_WORKDIR_MISSING_MSG = (
    "рабочая папка недоступна (удалена или переименована). "
    "Доступ заблокирован: выбери новую папку в панели — откроется окно выбора."
)


def workdir_missing() -> bool:
    """Рабочая папка настроена, но недоступна (удалена/переименована/файл).
    Проверка динамическая: ловит удаление и при старте, и во время работы."""
    return bool(STRICT and (WORKDIR is None or not WORKDIR.is_dir()))


# ---------- зоны ----------

def require_zone(resolved: Path) -> None:
    if zones.is_approved(zones.zone_of(resolved)):
        return
    raise NeedApprovalError(zones.zone_of(resolved))


# ---------- проверки команд shell ----------

_POSIX_PATH_RX = re.compile(
    r"(?<![\w.])(/(?:Users|private|etc|tmp|var|opt|usr|Applications|Library"
    r"|home|mnt|data|srv|media|workspace)[^\s'\"`$|;&<>]*)"
)
_TILDE_PATH_RX = re.compile(r"(?<![\w.~])(~[/\\][^\s'\"`$|;&<>]*)")
_WIN_PATH_RX = re.compile(r"(?<![\w])([A-Za-z]:[\\/][^\s'\"`$|;&<>]*)")


def _denied_part(resolved: Path) -> str | None:
    parts = {p.lower() for p in resolved.parts}
    for denied in DENY_PATH_PARTS:
        if denied.lower() in parts:
            return denied
    return None


def is_inside_base(resolved: Path, base: Path) -> bool:
    try:
        return resolved == base or base in resolved.parents
    except OSError:
        return False


def _inside_any_allowed(resolved: Path) -> bool:
    if FULL_ACCESS:
        return True
    return any(is_inside_base(resolved, root) for root in ALLOWED_DIRS)


def is_protected_root(target: Path) -> bool:
    """Корни, которые удалять нельзя никогда."""
    try:
        if target.parent == target:  # "/" или "C:\"
            return True
        if target == Path.home().resolve():
            return True
        if target == cfg_mod.BASE_DIR.resolve():
            return True
        if WORKDIR is not None and target == WORKDIR:
            return True
        if ALLOWED_DIRS and any(target == root for root in ALLOWED_DIRS):
            return True
    except OSError:
        pass
    return False


def resolve_path(user_path: str, cwd: str | None = None) -> Path:
    """Резолвить путь с проверками режима.

    strict: база — рабочая папка; выход (в т.ч. через симлинк или `..`) — ошибка.
    full: база cwd/home, jail выключен, новая папка верхнего уровня → зоны.
    list: база cwd/первый allowed_dir, jail по списку + зоны.
    """
    if not user_path or not user_path.strip():
        raise SecurityError("пустой путь запрещён")
    p = Path(os.path.expanduser(user_path.strip()))
    if not p.is_absolute():
        if STRICT:
            base = WORKDIR  # type: ignore[assignment]
        elif cwd:
            base = Path(os.path.expanduser(cwd)).resolve()
        elif ALLOWED_DIRS:
            base = ALLOWED_DIRS[0]
        else:
            base = Path.home().resolve()
        p = base / p
    try:
        resolved = p.resolve()  # раскрывает симлинки и ".."
    except (OSError, ValueError):
        # ValueError: null-байты и прочий мусор в пути — чистый отказ
        raise SecurityError(f"недопустимый путь: {user_path[:120]!r}")

    denied = _denied_part(resolved)
    if denied:
        raise SecurityError(f"путь содержит запрещённый компонент '{denied}': {resolved}")

    if STRICT:
        assert WORKDIR is not None
        if workdir_missing():
            # fail-closed: даже fs_write/fs_mkdir не должны молча пересоздавать
            # удалённую папку — сначала пользователь выбирает новую в панели
            raise SecurityError(f"{_WORKDIR_MISSING_MSG} Путь: {WORKDIR}")
        if not is_inside_base(resolved, WORKDIR):
            raise SecurityError(
                f"вне рабочей папки: {resolved}. Разрешено только: {WORKDIR}"
            )
        return resolved

    if not _inside_any_allowed(resolved):
        raise SecurityError(
            f"путь вне песочницы: {resolved}. "
            f"Разрешено: {', '.join(map(str, ALLOWED_DIRS)) or 'весь диск'}"
        )
    require_zone(resolved)
    return resolved


def check_shell_allowed(command: str) -> None:
    """deny_patterns применяются во всех режимах (явный запрет пользователя)."""
    for rx in DENY_RES:
        if rx.search(command):
            raise SecurityError(
                f"команда запрещена правилом '{rx.pattern}': {command[:120]}"
            )


def check_command_paths(command: str, cwd: Path | None) -> None:
    """Абсолютные пути внутри команды: strict — только внутри рабочей папки;
    иначе — зоны. ~ раскрывается. Best-effort, см. README."""
    if STRICT and workdir_missing():
        raise SecurityError(_WORKDIR_MISSING_MSG)
    candidates: list[str] = [m.group(1) for m in _POSIX_PATH_RX.finditer(command)]
    candidates += [m.group(1) for m in _TILDE_PATH_RX.finditer(command)]
    candidates += [m.group(1) for m in _WIN_PATH_RX.finditer(command)]
    for raw in candidates:
        raw = raw.rstrip(".,:)")
        try:
            resolved = Path(os.path.expanduser(raw)).resolve()
        except OSError:
            continue
        if _denied_part(resolved):
            raise SecurityError(f"команда затрагивает запрещённый путь: {resolved}")
        if STRICT:
            assert WORKDIR is not None
            if not is_inside_base(resolved, WORKDIR):
                raise SecurityError(
                    f"команда обращается к пути вне рабочей папки: {resolved}. "
                    f"Разрешено только: {WORKDIR}"
                )
        elif not FULL_ACCESS:
            if not _inside_any_allowed(resolved):
                raise SecurityError(f"команда затрагивает путь вне песочницы: {resolved}")
            require_zone(resolved)


# ---------- сообщения для агента ----------

def deny_payload(e: SecurityError) -> dict:
    if isinstance(e, NeedApprovalError):
        return {
            "ok": False,
            "need_approval": True,
            "zone": e.zone,
            "error": (
                f"НОВАЯ ПАПКА {e.zone}: остановись и спроси пользователя в чате, "
                f"дать ли доступ. Если да — вызови approve_zone(zone=\"{e.zone}\", "
                f"confirm=true) и повтори исходный вызов."
            ),
        }
    if STRICT:
        assert WORKDIR is not None
        return {
            "ok": False,
            "error": f"{e}. Режим рабочей папки: работай строго внутри {WORKDIR}.",
        }
    return {"ok": False, "error": str(e)}
