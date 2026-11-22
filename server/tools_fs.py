"""Файловые инструменты MCP."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable

from . import audit, config as cfg_mod, guard

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
             ".mypy_cache", ".pytest_cache"}


def _e(fn: Callable[[], dict], tool: str, args: dict[str, Any]) -> dict:
    """Обёртка: guard-ошибки → denial-payload + аудит."""
    try:
        result = fn()
    except guard.SecurityError as e:
        audit.audit(tool, args, False, str(e))
        return guard.deny_payload(e)
    except OSError as e:
        audit.audit(tool, args, False, f"{type(e).__name__}: {e}")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    audit.audit(tool, args, bool(result.get("ok", True)),
                str(result.get("note", result.get("error", "")))[:300])
    return result


def fs_list(path: str = ".", recursive: bool = False, max_entries: int = 100) -> dict:
    """Листинг директории. В полном режиме новую папку верхнего уровня
    сначала надо подтвердить (зоны)."""
    def run() -> dict:
        root = guard.resolve_path(path)
        if not root.exists():
            return {"ok": False, "error": f"не существует: {root}"}
        if root.is_file():
            st = root.stat()
            return {"ok": True, "path": str(root), "type": "file", "size": st.st_size}
        entries: list[dict] = []
        truncated = False
        if recursive:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS][:30]
                for name in dirnames + filenames:
                    full = Path(dirpath) / name
                    try:
                        if guard.STRICT:
                            resolved = full.resolve()
                            if not guard.is_inside_base(resolved, guard.WORKDIR):  # type: ignore[arg-type]
                                continue
                        entries.append({
                            "path": str(full.relative_to(root)),
                            "type": "dir" if full.is_dir() else "file",
                            "size": full.stat().st_size if full.is_file() else 0,
                        })
                    except OSError:
                        continue
                    if len(entries) >= max_entries:
                        truncated = True
                        break
                if truncated:
                    break
        else:
            try:
                children = sorted(root.iterdir(),
                                  key=lambda p: (not p.is_dir(), p.name.lower()))
            except OSError as e:
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}
            for child in children:
                try:
                    entries.append({
                        "name": child.name,
                        "type": "dir" if child.is_dir() else "file",
                        "size": child.stat().st_size if child.is_file() else 0,
                    })
                except OSError:
                    continue
                if len(entries) >= max_entries:
                    truncated = True
                    break
        return {"ok": True, "path": str(root), "entries": entries, "truncated": truncated}
    return _e(run, "fs_list", {"path": path})


def fs_read(path: str, offset: int = 0, limit: int = 100) -> dict:
    """Прочитать текстовый файл построчно (offset 1-indexed).
    Бинарники и файлы больше лимита отклоняются."""
    def run() -> dict:
        target = guard.resolve_path(path)
        if not target.is_file():
            return {"ok": False, "error": f"не файл: {target}"}
        size = target.stat().st_size
        limit_bytes = int(cfg_mod.CONFIG["max_file_bytes"])
        if size > limit_bytes:
            return {"ok": False,
                    "error": f"файл слишком большой ({size} байт), лимит {limit_bytes}"}
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return {"ok": False, "error": "файл не текстовый (UTF-8 не декодируется)"}
        lines = text.splitlines()
        total = len(lines)
        start = max(offset - 1, 0) if offset > 0 else 0
        chunk = lines[start:start + limit]
        numbered = [f"{i + 1 + start}: {line}" for i, line in enumerate(chunk)]
        return {"ok": True, "path": str(target), "total_lines": total,
                "offset": offset, "content": "\n".join(numbered),
                "note": f"lines {start + 1}-{start + len(chunk)}/{total}"}
    return _e(run, "fs_read", {"path": path})


def fs_write(path: str, content: str) -> dict:
    """Создать или ПЕРЕЗАПИСАТЬ файл целиком. Родительские папки создаются."""
    def run() -> dict:
        target = guard.resolve_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"ok": True, "path": str(target), "bytes": len(content.encode())}
    return _e(run, "fs_write", {"path": path})


def fs_edit(path: str, old_string: str, new_string: str,
            replace_all: bool = False) -> dict:
    """Точечная правка: заменить old_string на new_string.
    Ошибка, если не найдено или несколько совпадений (без replace_all)."""
    def run() -> dict:
        target = guard.resolve_path(path)
        if not target.is_file():
            return {"ok": False, "error": f"не файл: {target}"}
        try:
            text = target.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as e:
            return {"ok": False, "error": f"не могу прочитать: {e}"}
        count = text.count(old_string)
        if count == 0:
            return {"ok": False, "error": "old_string не найден в файле"}
        if count > 1 and not replace_all:
            return {"ok": False,
                    "error": f"найдено совпадений: {count}. "
                             f"Уточни контекст или поставь replace_all=true"}
        new_text = text.replace(old_string, new_string) if replace_all \
            else text.replace(old_string, new_string, 1)
        target.write_text(new_text, encoding="utf-8")
        return {"ok": True, "path": str(target),
                "replacements": count if replace_all else 1}
    return _e(run, "fs_edit", {"path": path})


def fs_glob(pattern: str, root: str = ".", max_entries: int = 100) -> dict:
    """Найти файлы по glob-паттерну: '**/*.py', 'src/**/*.ts'.
    Абсолютные паттерны и '..' запрещены."""
    def run() -> dict:
        base = guard.resolve_path(root)
        if not base.is_dir():
            return {"ok": False, "error": f"root не директория: {base}"}
        if pattern.startswith("/") or ".." in pattern.split("/"):
            return {"ok": False, "error": "абсолютные паттерны и '..' запрещены"}
        if re.match(r"^[A-Za-z]:", pattern):
            return {"ok": False, "error": "абсолютные паттерны запрещены (и C:\\ тоже)"}
        try:
            matches = sorted(base.glob(pattern), key=lambda p: str(p))
        except ValueError as e:
            return {"ok": False, "error": f"плохой паттерн: {e}"}
        out: list[str] = []
        truncated = False
        for m in matches:
            try:
                resolved = m.resolve()
            except OSError:
                continue
            if guard.STRICT:
                assert guard.WORKDIR is not None
                if not guard.is_inside_base(resolved, guard.WORKDIR):
                    continue  # симлинк наружу — молча пропускаем
            else:
                if guard._denied_part(resolved):
                    continue
                try:
                    guard.require_zone(resolved)
                except guard.NeedApprovalError:
                    continue
            out.append(str(resolved.relative_to(base)))
            if len(out) >= max_entries:
                truncated = True
                break
        return {"ok": True, "root": str(base), "matches": out, "truncated": truncated}
    return _e(run, "fs_glob", {"pattern": pattern})


def fs_grep(pattern: str, root: str = ".", include: str = "*",
            max_matches: int = 50) -> dict:
    """Поиск по содержимому файлов (regex). Мусор (.git, node_modules, .venv)
    пропускается. Симлинки наружу и неподтверждённые зоны — тоже."""
    def run() -> dict:
        base = guard.resolve_path(root)
        if not base.is_dir():
            return {"ok": False, "error": f"root не директория: {base}"}
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return {"ok": False, "error": f"плохой regex: {e}"}
        hits: list[dict] = []
        truncated = False
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                if not Path(name).match(include):
                    continue
                full = Path(dirpath) / name
                try:
                    if full.stat().st_size > 512 * 1024:
                        continue
                    resolved = full.resolve()
                except OSError:
                    continue
                if guard.STRICT:
                    assert guard.WORKDIR is not None
                    if not guard.is_inside_base(resolved, guard.WORKDIR):
                        continue
                else:
                    if guard._denied_part(resolved):
                        continue
                    try:
                        guard.require_zone(resolved)
                    except guard.NeedApprovalError:
                        continue
                try:
                    with open(resolved, encoding="utf-8", errors="strict") as f:
                        for i, line in enumerate(f, 1):
                            if rx.search(line):
                                rel = str(resolved.relative_to(base))
                                hits.append({"file": rel, "line": i,
                                             "text": line.strip()[:300]})
                                if len(hits) >= max_matches:
                                    break
                except (UnicodeDecodeError, OSError):
                    continue  # бинарник — пропускаем
                if len(hits) >= max_matches:
                    truncated = True
                    break
            if truncated:
                break
        return {"ok": True, "root": str(base), "matches": hits, "truncated": truncated}
    return _e(run, "fs_grep", {"pattern": pattern[:100]})


def fs_mkdir(path: str) -> dict:
    """Создать директорию (включая родителей)."""
    def run() -> dict:
        target = guard.resolve_path(path)
        target.mkdir(parents=True, exist_ok=True)
        return {"ok": True, "path": str(target)}
    return _e(run, "fs_mkdir", {"path": path})


def fs_delete(path: str) -> dict:
    """Удалить файл или ПУСТУЮ директорию. Рекурсивного удаления нет —
    защита от rm -rf. Корень диска, дом, папка приложения и рабочая
    папка не удаляются никогда."""
    def run() -> dict:
        target = guard.resolve_path(path)
        if guard.is_protected_root(target):
            return {"ok": False, "error": "этот путь удалять нельзя (корень/дом/папка сервера)"}
        if target.is_dir():
            target.rmdir()  # упадёт, если не пустая — так и задумано
        elif target.exists():
            target.unlink()
        else:
            return {"ok": False, "error": f"не существует: {target}"}
        return {"ok": True, "path": str(target)}
    return _e(run, "fs_delete", {"path": path})
