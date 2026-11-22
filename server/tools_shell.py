"""Shell-инструменты MCP: синхронные, фоновые, убиение задач."""

from __future__ import annotations

import asyncio
import os
import platform
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import audit, config as cfg_mod, guard

IS_WINDOWS = cfg_mod.IS_WINDOWS


def _default_shell_executable() -> str | None:
    if IS_WINDOWS:
        return None
    for candidate in ("/bin/zsh", "/bin/bash"):
        if Path(candidate).is_file():
            return candidate
    return None


SHELL_EXE = _default_shell_executable()

JOBS: dict[str, dict[str, Any]] = {}

JOB_MAX_OUTPUT = 512_000


def _prepare(command: str, cwd: str, timeout: int, tool: str) -> tuple[Path, int]:
    """Общие проверки. Возвращает (workdir, timeout). Бросает SecurityError."""
    timeout = max(1, min(int(timeout), int(cfg_mod.CONFIG["max_shell_timeout"])))
    workdir = guard.resolve_path(cwd or ".")
    if not workdir.is_dir():
        raise guard.SecurityError(f"cwd не директория: {workdir}")
    guard.check_shell_allowed(command)
    guard.check_command_paths(command, workdir)
    return workdir, timeout


def shell_run(command: str, cwd: str = ".", timeout: int = 30) -> dict:
    """Выполнить shell-команду синхронно. Долгие задачи — через shell_run_async."""
    try:
        workdir, timeout = _prepare(command, cwd, timeout, "shell_run")
    except guard.SecurityError as e:
        audit.audit("shell_run", {"command": command[:200], "cwd": cwd}, False, str(e))
        return guard.deny_payload(e)
    max_chars = int(cfg_mod.CONFIG["max_output_chars"])
    try:
        run_kwargs: dict = dict(shell=True, cwd=str(workdir),
                                capture_output=True, text=True, timeout=timeout)
        if SHELL_EXE:
            run_kwargs["executable"] = SHELL_EXE
        proc = subprocess.run(command, **run_kwargs)  # type: ignore[arg-type]
        audit.audit("shell_run", {"command": command[:200], "cwd": str(workdir)},
                    True, f"exit={proc.returncode}")
        return {"ok": True, "exit_code": proc.returncode,
                "stdout": (proc.stdout or "")[-max_chars:],
                "stderr": (proc.stderr or "")[-max_chars:]}
    except subprocess.TimeoutExpired as e:
        out = ""
        if e.stdout:
            out += e.stdout.decode("utf-8", errors="replace") \
                if isinstance(e.stdout, bytes) else str(e.stdout)
        if e.stderr:
            out += e.stderr.decode("utf-8", errors="replace") \
                if isinstance(e.stderr, bytes) else str(e.stderr)
        audit.audit("shell_run", {"command": command[:200], "cwd": str(workdir)},
                    False, f"timeout {timeout}s")
        return {"ok": False, "error": f"timeout {timeout}s", "output": out[-max_chars:]}
    except OSError as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def _run_job(job_id: str, command: str, cwd: str, timeout: int) -> None:
    job = JOBS[job_id]
    try:
        kwargs: dict = dict(cwd=cwd, stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.STDOUT)
        if SHELL_EXE:
            kwargs["executable"] = SHELL_EXE
        if os.name != "nt":
            kwargs["start_new_session"] = True  # kill по группе, не по шеллу
        proc = await asyncio.create_subprocess_shell(command, **kwargs)  # type: ignore[arg-type]
        job["pid"] = proc.pid
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            if job["status"] == "killed":
                return  # shell_kill уже зафиксировал статус — не перезаписывать
            job["output"] = out.decode("utf-8", errors="replace")[-JOB_MAX_OUTPUT:]
            job["exit_code"] = proc.returncode
            job["status"] = "done"
        except asyncio.TimeoutError:
            _kill_job_tree(job)
            out, _ = await proc.communicate()
            job["output"] = (out or b"").decode("utf-8", errors="replace")[-JOB_MAX_OUTPUT:]
            job["exit_code"] = 124
            if job["status"] != "killed":
                job["status"] = "timeout"
    except Exception as e:  # noqa: BLE001
        job["status"] = "error"
        job["output"] = f"{type(e).__name__}: {e}"[-JOB_MAX_OUTPUT:]
        job["exit_code"] = 1
    job["finished_at"] = time.time()


def _kill_job_tree(job: dict) -> None:
    pid = job.get("pid")
    if not pid:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True)
        return
    import signal
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)  # вся группа: дети тоже
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def _prune_jobs(max_age_s: int = 3600) -> None:
    """Фоновые записи завершённых job'ов старше часа удаляем (утечка памяти)."""
    now = time.time()
    for jid in [j for j, v in JOBS.items()
                if v["status"] != "running"
                and v.get("finished_at") and now - v["finished_at"] > max_age_s]:
        JOBS.pop(jid, None)


def shell_run_async(command: str, cwd: str = ".", timeout: int = 120) -> dict:
    """Запустить долгую команду в фоне (сборка, тесты, сервер).
    Результат забирать через shell_poll(job_id). Таймаут ограничен
    max_shell_timeout из настроек."""
    _prune_jobs()
    timeout = max(1, min(int(timeout), int(cfg_mod.CONFIG["max_shell_timeout"])))
    try:
        workdir, timeout = _prepare(command, cwd, timeout, "shell_run_async")
    except guard.SecurityError as e:
        audit.audit("shell_run_async", {"command": command[:200], "cwd": cwd},
                    False, str(e))
        return guard.deny_payload(e)
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "running", "output": "", "exit_code": None,
                    "pid": None, "started_at": time.time(),
                    "command": command[:300], "cwd": str(workdir)}
    # Инструменты FastMCP исполняются в worker-потоках без event loop —
    # поэтому каждому job свой поток со своим лупом (надёжно везде).
    threading.Thread(
        target=lambda: asyncio.run(_run_job(job_id, command, str(workdir), timeout)),
        daemon=True,
    ).start()
    audit.audit("shell_run_async", {"command": command[:200]}, True, f"job={job_id}")
    return {"ok": True, "job_id": job_id,
            "hint": "забери результат через shell_poll(job_id)"}


def shell_poll(job_id: str, tail_chars: int = 8000) -> dict:
    """Статус и вывод фоновой команды. status: running|done|timeout|killed|error."""
    job = JOBS.get(job_id)
    if not job:
        return {"ok": False, "error": f"job не найден: {job_id}"}
    out = job.get("output", "") or ""
    running_jobs = sum(1 for j in JOBS.values() if j["status"] == "running")
    return {"ok": True, "job_id": job_id, "status": job["status"],
            "exit_code": job.get("exit_code"),
            "output_tail": out[-max(200, min(tail_chars, JOB_MAX_OUTPUT)):],
            "note": f"jobs running: {running_jobs}"}


def shell_kill(job_id: str) -> dict:
    """Остановить фоновую команду вместе с дочерними процессами (группа)."""
    job = JOBS.get(job_id)
    if not job:
        return {"ok": False, "error": f"job не найден: {job_id}"}
    if job["status"] != "running":
        return {"ok": False, "error": f"job уже завершён (status={job['status']})"}
    pid = job.get("pid")
    if not pid:
        return {"ok": False, "error": "у job ещё нет pid"}
    killed = True
    _kill_job_tree(job)
    if os.name != "nt":
        import signal
        time.sleep(0.5)
        try:
            os.killpg(os.getpgid(pid), 0)  # группа жива?
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    job["status"] = "killed"
    job["finished_at"] = time.time()
    audit.audit("shell_kill", {"job_id": job_id}, killed, f"pid={pid}")
    return {"ok": killed, "job_id": job_id, "status": "killed", "pid": pid}
