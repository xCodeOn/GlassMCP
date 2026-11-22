"""Смоук-тесты MCP-инструментов в строгом и полном режимах."""

from __future__ import annotations

from pathlib import Path

import pytest

from server import tools_fs, tools_shell, tools_extra
from server import config as cfg_mod, guard


class TestFsToolsStrict:
    def test_write_read_edit_delete_cycle(self, workdir_mode):
        p = "demo/hello.txt"
        r = tools_fs.fs_write(p, "hello world")
        assert r["ok"]
        r = tools_fs.fs_read(p)
        assert r["ok"] and "hello world" in r["content"]
        r = tools_fs.fs_edit(p, "world", "glassmcp")
        assert r["ok"] and r["replacements"] == 1
        r = tools_fs.fs_delete(p)
        assert r["ok"]

    def test_write_outside_denied(self, workdir_mode, tmp_path):
        r = tools_fs.fs_write(str(tmp_path / "x.txt"), "nope")
        assert not r["ok"]
        assert "вне рабочей папки" in r["error"]

    def test_glob_symlink_out_filtered(self, workdir_mode, tmp_path):
        import os
        outside = tmp_path / "outs"
        outside.mkdir()
        (outside / "target.py").write_text("print(1)")
        os.symlink(outside, workdir_mode / "leak")
        (workdir_mode / "real.py").write_text("print(2)")
        r = tools_fs.fs_glob("**/*.py")
        assert r["ok"]
        names = [Path(m).name for m in r["matches"]]
        assert "real.py" in names
        assert "target.py" not in names

    def test_grep_finds_inside(self, workdir_mode):
        tools_fs.fs_write("src/main.py", "GLASSMARKER = True\n")
        r = tools_fs.fs_grep("GLASSMARKER", include="*.py")
        assert r["ok"] and len(r["matches"]) == 1
        assert r["matches"][0]["file"] == "src/main.py"

    def test_mkdir_and_tree(self, workdir_mode):
        tools_fs.fs_mkdir("a/b/c")
        r = tools_extra.dir_tree(".", depth=3)
        assert r["ok"]
        assert any(line.strip().endswith("c/") for line in r["tree"].splitlines())


class TestShellTools:
    def test_simple_run(self, workdir_mode):
        r = tools_shell.shell_run("echo glass-ok")
        assert r["ok"] and "glass-ok" in (r.get("stdout") or "")

    def test_command_outside_denied_strict(self, workdir_mode):
        r = tools_shell.shell_run("cat /etc/passwd")
        assert not r["ok"]
        assert "вне рабочей папки" in r["error"]

    def test_deny_pattern(self, workdir_mode, tmp_cfg):
        tmp_cfg["deny_patterns"] = ["sudo"]
        guard.refresh(tmp_cfg)
        r = tools_shell.shell_run("sudo id")
        assert not r["ok"] and "запрещена" in r["error"]

    def test_async_job_lifecycle(self, workdir_mode):
        r = tools_shell.shell_run_async("echo async-done", timeout=10)
        assert r["ok"]
        job_id = r["job_id"]
        import time
        for _ in range(50):
            poll = tools_shell.shell_poll(job_id)
            if poll["status"] != "running":
                break
            time.sleep(0.1)
        assert poll["status"] == "done"
        assert "async-done" in poll["output_tail"]

    def test_shell_kill(self, workdir_mode):
        import time
        r = tools_shell.shell_run_async("sleep 30", timeout=60)
        assert r["ok"]
        time.sleep(0.8)
        kill = tools_shell.shell_kill(r["job_id"])
        assert kill["ok"]
        poll = tools_shell.shell_poll(r["job_id"])
        assert poll["status"] == "killed"


class TestSystemInfo:
    def test_reports_strict_mode(self, workdir_mode):
        r = tools_extra.system_info()
        assert r["ok"] and r["mode"].startswith("рабочая папка")
        assert r["workdir"] == str(workdir_mode)

    def test_reports_full_mode(self, tmp_cfg):
        r = tools_extra.system_info()
        assert r["ok"] and "полный доступ" in r["mode"]


class TestDeletedWorkdir:
    """Рабочую папку удалили, пока сервер работает: всё блокируется,
    и ничто не должно молча пересоздавать её."""

    def test_fs_write_blocked_not_recreated(self, workdir_mode):
        workdir_mode.rmdir()
        r = tools_fs.fs_write("new.txt", "x")
        assert not r["ok"] and "недоступна" in r["error"]
        assert not workdir_mode.exists()

    def test_fs_mkdir_blocked_not_recreated(self, workdir_mode):
        workdir_mode.rmdir()
        r = tools_fs.fs_mkdir("sub")
        assert not r["ok"] and "недоступна" in r["error"]
        assert not workdir_mode.exists()

    def test_shell_blocked(self, workdir_mode):
        workdir_mode.rmdir()
        r = tools_shell.shell_run("ls")
        assert not r["ok"]

    def test_system_info_warns_agent(self, workdir_mode):
        workdir_mode.rmdir()
        info = tools_extra.system_info()
        assert info["workdir_missing"] is True
        assert "панел" in info["warning"]  # подсказка: выбрать новую папку в панели
