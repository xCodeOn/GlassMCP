"""Тесты живой проверки Cloudflare-туннеля (probe /health)."""

from __future__ import annotations

import json
import threading

import pytest

from server import tunnel


@pytest.fixture()
def mgr(tmp_path, monkeypatch):
    """Экземпляр менеджера без reattach и без реального cloudflared;
    PID-файл указывает в tmp, чтобы stop() не трогал настоящий."""
    monkeypatch.setattr(tunnel, "PID_FILE", tmp_path / ".tunnel_pid")
    m = tunnel.TunnelManager.__new__(tunnel.TunnelManager)
    m.proc = None
    m.attached_pid = None
    m.url = "https://demo.trycloudflare.com"
    m.error = ""
    m.starting = False
    m.downloading = False
    m.mode = "quick"
    m._url_thread = None
    m.probe = tunnel.TunnelManager._new_probe()
    m._probe_stop = threading.Event()
    m._probe_thread = None
    yield m
    m._probe_stop.set()


class _FakeResp:
    """Заглушка ответа urlopen: контекстный менеджер + read()."""

    def __init__(self, body: bytes):
        self._body = body

    def read(self, n=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ok_resp():
    return _FakeResp(json.dumps({"ok": True, "service": "GlassMCP"}).encode())


class TestProbeOnce:
    def test_ok(self, mgr, monkeypatch):
        monkeypatch.setattr(tunnel.urllib.request, "urlopen",
                            lambda req, timeout: _ok_resp())
        ok, err = mgr._probe_once(mgr.url)
        assert ok is True and err == ""

    def test_network_error(self, mgr, monkeypatch):
        def boom(req, timeout):
            raise OSError("connection refused")
        monkeypatch.setattr(tunnel.urllib.request, "urlopen", boom)
        ok, err = mgr._probe_once(mgr.url)
        assert ok is False and "refused" in err

    def test_bad_json_body(self, mgr, monkeypatch):
        monkeypatch.setattr(tunnel.urllib.request, "urlopen",
                            lambda req, timeout: _FakeResp(b"<html>edge error</html>"))
        ok, err = mgr._probe_once(mgr.url)
        assert ok is False and "не-JSON" in err

    def test_json_without_ok(self, mgr, monkeypatch):
        monkeypatch.setattr(tunnel.urllib.request, "urlopen",
                            lambda req, timeout: _FakeResp(b'{"ok":false}'))
        ok, err = mgr._probe_once(mgr.url)
        assert ok is False and "ok:true" in err


class TestProbeState:
    def test_two_failures_alarm(self, mgr, monkeypatch):
        monkeypatch.setattr(mgr, "_probe_once", lambda url: (False, "timeout"))
        mgr.probe_now()
        # одиночный сбой — ещё не тревога: сеть могла моргнуть
        assert mgr.probe["state"] == "checking"
        mgr.probe_now()
        assert mgr.probe["state"] == "fail"
        assert mgr.probe["fails"] == 2
        assert mgr.probe["last_error"] == "timeout"

    def test_success_resets(self, mgr, monkeypatch):
        monkeypatch.setattr(mgr, "_probe_once", lambda url: (False, "x"))
        mgr.probe_now()
        mgr.probe_now()
        assert mgr.probe["state"] == "fail"
        monkeypatch.setattr(mgr, "_probe_once", lambda url: (True, ""))
        mgr.probe_now()
        assert mgr.probe["state"] == "ok" and mgr.probe["fails"] == 0
        assert mgr.probe["last_ok_at"] is not None

    def test_probe_now_without_url(self, mgr):
        mgr.url = ""
        p = mgr.probe_now()
        assert "не запущен" in p["last_error"]

    def test_status_contains_probe(self, mgr):
        st = mgr.status()
        assert st["probe"]["state"] == "unknown"


class TestProbeThread:
    def test_start_and_stop(self, mgr, monkeypatch):
        monkeypatch.setattr(mgr, "_probe_once", lambda url: (True, ""))
        mgr._start_probe(mgr.url)
        assert mgr._probe_thread is not None
        mgr._probe_stop.set()
        mgr._probe_thread.join(3)
        assert not mgr._probe_thread.is_alive()

    def test_stop_resets_probe(self, mgr, monkeypatch):
        monkeypatch.setattr(mgr, "_probe_once", lambda url: (False, "x"))
        mgr.probe_now()
        mgr.stop()
        assert mgr.probe["state"] == "unknown"
