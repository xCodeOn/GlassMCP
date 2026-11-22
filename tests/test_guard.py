"""Тесты ядра безопасности: строгий режим рабочей папки, зоны, запреты.

Главная гарантия: в режиме рабочей папки НИКАКОЙ путь (включая `..`,
абсолютные пути и симлинки) не резолвится наружу.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from server import guard
from server.guard import NeedApprovalError, SecurityError


# ---------- строгий режим (workdir) ----------

class TestStrictWorkdir:
    def test_inside_ok(self, workdir_mode, tmp_path):
        target = workdir_mode / "sub" / "file.txt"
        assert guard.resolve_path(str(target)) == target

    def test_relative_resolves_inside(self, workdir_mode):
        resolved = guard.resolve_path("sub/file.txt")
        assert guard.is_inside_base(resolved, workdir_mode)

    def test_dotdot_escape_denied(self, workdir_mode):
        with pytest.raises(SecurityError):
            guard.resolve_path("../outside.txt")

    def test_absolute_outside_denied(self, workdir_mode, tmp_path):
        with pytest.raises(SecurityError):
            guard.resolve_path(str(tmp_path / "outside.txt"))

    def test_home_escape_denied(self, workdir_mode):
        with pytest.raises(SecurityError):
            guard.resolve_path("~/Documents")

    def test_symlink_out_denied(self, workdir_mode, tmp_path):
        """Главный трюк обхода: симлинк внутри папки, ведущий наружу."""
        outside = tmp_path / "secret"
        outside.mkdir()
        link = workdir_mode / "link"
        os.symlink(outside, link)
        with pytest.raises(SecurityError):
            guard.resolve_path(str(link))
        with pytest.raises(SecurityError):
            guard.resolve_path("link/file.txt")

    def test_workdir_itself_ok(self, workdir_mode):
        assert guard.resolve_path(str(workdir_mode)) == workdir_mode

    def test_empty_path_denied(self, workdir_mode):
        with pytest.raises(SecurityError):
            guard.resolve_path("")

    def test_shell_command_outside_denied(self, workdir_mode):
        with pytest.raises(SecurityError):
            guard.check_command_paths("cat /etc/passwd", workdir_mode)

    def test_shell_command_home_denied(self, workdir_mode):
        with pytest.raises(SecurityError):
            guard.check_command_paths("cat ~/secrets.txt", workdir_mode)

    def test_shell_command_inside_ok(self, workdir_mode):
        guard.check_command_paths(f"cat {workdir_mode}/notes.txt", workdir_mode)

    def test_shell_relative_paths_ok(self, workdir_mode):
        guard.check_command_paths("ls sub/ && cat ./notes.txt", workdir_mode)

    def test_refresh_missing_dir_marks_unavailable(self, tmp_cfg, tmp_path):
        # папка удалена: refresh НЕ падает — сервер и панель обязаны запуститься,
        # чтобы показать обязательную модалку выбора новой папки
        cfg = {**tmp_cfg, "workdir": str(tmp_path / "nonexistent")}
        guard.refresh(cfg)
        assert guard.STRICT and guard.workdir_missing()
        with pytest.raises(SecurityError):
            guard.resolve_path("file.txt")
        # даже команда без путей блокируется, пока папка недоступна
        with pytest.raises(SecurityError):
            guard.check_command_paths("ls", guard.WORKDIR)

    def test_refresh_file_as_workdir_marks_unavailable(self, tmp_cfg, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("x")
        cfg = {**tmp_cfg, "workdir": str(f)}
        guard.refresh(cfg)
        assert guard.STRICT and guard.workdir_missing()

    def test_workdir_deleted_at_runtime(self, workdir_mode):
        # удаление «на лету» ловится динамической проверкой, без refresh
        workdir_mode.rmdir()
        assert guard.workdir_missing()
        with pytest.raises(SecurityError):
            guard.resolve_path("x.txt")

    def test_mode_name(self, workdir_mode):
        assert guard.mode_name() == "workdir"


# ---------- полный доступ (зоны) ----------

class TestFullAccess:
    def test_home_desktop_seeded_ok(self, tmp_cfg, isolated_zones):
        guard.refresh(tmp_cfg)
        desktop = Path.home() / "Desktop"
        # Desktop — стартовая зона: не должно требовать подтверждения
        guard.resolve_path(str(desktop))

    def test_new_zone_needs_approval(self, tmp_cfg, isolated_zones):
        guard.refresh(tmp_cfg)
        # /etc на macOS — симлинк в /private; зона = верхний корень после resolve
        with pytest.raises(NeedApprovalError) as exc:
            guard.resolve_path("/etc/hosts")
        assert exc.value.zone.startswith("/")

    def test_list_mode_jail(self, tmp_cfg, isolated_zones, tmp_path):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        cfg = {**tmp_cfg, "allowed_dirs": [str(allowed)]}
        guard.refresh(cfg)
        # внутри — ок (после подтверждения зоны)
        from server import zones as z
        z.ZONES[z.zone_of(allowed)] = "approved"
        assert guard.resolve_path(str(allowed / "x.txt")) == allowed / "x.txt"
        with pytest.raises(SecurityError):
            guard.resolve_path(str(tmp_path / "other"))

    def test_mode_name_full(self, tmp_cfg):
        assert guard.mode_name() == "full"


# ---------- запреты (все режимы) ----------

class TestDenyRules:
    def test_deny_pattern_applied_in_full(self, tmp_cfg):
        tmp_cfg["deny_patterns"] = ["rm\\s+-rf"]
        guard.refresh(tmp_cfg)
        with pytest.raises(SecurityError):
            guard.check_shell_allowed("rm -rf /")

    def test_deny_pattern_applied_in_strict(self, workdir_mode, tmp_cfg):
        tmp_cfg["deny_patterns"] = ["curl"]
        guard.refresh(tmp_cfg)
        with pytest.raises(SecurityError):
            guard.check_shell_allowed("curl example.com")

    def test_deny_path_part(self, tmp_cfg, tmp_path):
        tmp_cfg["deny_paths"] = [".ssh"]
        guard.refresh(tmp_cfg)
        with pytest.raises(SecurityError):
            guard.resolve_path(str(Path.home() / ".ssh" / "id_rsa"))

    def test_deny_path_in_strict(self, workdir_mode, tmp_cfg):
        secret = workdir_mode / "vault"
        secret.mkdir()
        tmp_cfg["deny_paths"] = ["vault"]
        guard.refresh(tmp_cfg)
        with pytest.raises(SecurityError):
            guard.resolve_path(str(secret))


# ---------- защищённые корни ----------

class TestProtectedRoots:
    def test_disk_root_protected(self, tmp_cfg):
        guard.refresh(tmp_cfg)
        assert guard.is_protected_root(Path("/"))

    def test_home_protected(self, tmp_cfg):
        guard.refresh(tmp_cfg)
        assert guard.is_protected_root(Path.home().resolve())

    def test_app_dir_protected(self, tmp_cfg):
        guard.refresh(tmp_cfg)
        assert guard.is_protected_root(guard.cfg_mod.BASE_DIR.resolve())

    def test_workdir_protected(self, workdir_mode):
        assert guard.is_protected_root(workdir_mode)
