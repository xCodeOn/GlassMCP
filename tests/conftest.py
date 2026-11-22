"""Общие фикстуры тестов: изолированные CONFIG/zones/tmp-пути."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

from server import config as cfg_mod  # noqa: E402
from server import guard  # noqa: E402
from server import zones  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_tunnel(tmp_path, monkeypatch):
    """Тесты не должны видеть реальный рантайм: MANAGER при импорте делает
    reattach() по живым .tunnel_pid и logs/cloudflared.log. Если запущен
    сервер с туннелем, статус «tunnel_running» протекал бы в тесты —
    обнуляем состояние менеджера на время каждого теста."""
    from server import tunnel as tmod
    monkeypatch.setattr(tmod, "PID_FILE", tmp_path / ".tunnel_pid")
    monkeypatch.setattr(tmod, "LOG_PATH", tmp_path / "cloudflared.log")
    m = tmod.MANAGER
    m._probe_stop.set()  # заглушить фоновый probe-поток, поднятый на импорте
    monkeypatch.setattr(m, "proc", None, raising=False)
    for attr, val in (("attached_pid", None), ("url", ""), ("error", ""),
                      ("starting", False), ("mode", "quick"),
                      ("_url_thread", None), ("_probe_thread", None)):
        monkeypatch.setattr(m, attr, val)
    monkeypatch.setattr(m, "probe", tmod.TunnelManager._new_probe())
    monkeypatch.setattr(m, "_probe_stop", threading.Event())
    yield


@pytest.fixture()
def tmp_cfg(tmp_path, monkeypatch):
    """CONFIG в tmp-папке: yaml, .env, аудит — ничего не пишется в реальный APP."""
    cfg = {**cfg_mod.DEFAULTS, "token": "test-token-123", "workdir": "",
           "allowed_dirs": ["*"], "deny_patterns": [], "deny_paths": [],
           "audit_log": str(tmp_path / "audit.log")}
    monkeypatch.setattr(cfg_mod, "CONFIG", cfg)
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(cfg_mod, "ENV_PATH", tmp_path / ".env")
    guard.refresh(cfg)
    yield cfg
    guard.refresh(cfg)


@pytest.fixture()
def workdir_mode(tmp_cfg, tmp_path, monkeypatch):
    """Строгий режим: рабочая папка = tmp_path/work."""
    work = tmp_path / "work"
    work.mkdir()
    work = work.resolve()
    tmp_cfg["workdir"] = str(work)
    guard.refresh(tmp_cfg)
    assert guard.STRICT and guard.WORKDIR == work
    yield work


@pytest.fixture()
def isolated_zones(tmp_path, monkeypatch):
    """Зоны в tmp-файле (как в проде: стартовый сид — Рабочий стол)."""
    monkeypatch.setattr(zones, "ZONES_FILE", tmp_path / "zones.json")
    monkeypatch.setattr(zones, "ZONES", {str(Path.home() / "Desktop"): "seed"})
    yield zones
