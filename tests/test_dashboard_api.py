"""Тесты Dashboard API через starlette TestClient."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

from server import config as cfg_mod, guard  # noqa: E402
from server import dashboard as dash  # noqa: E402


@pytest.fixture()
def client(tmp_cfg, monkeypatch):
    # запретить реальный exec-рестарт в тестах; TestClient не локальный сокет
    monkeypatch.setattr(dash, "_exec_restart", lambda: None)
    monkeypatch.setattr(dash, "LOCAL_ONLY", False)
    app = dash.build_dashboard_app()
    with TestClient(app) as c:
        yield c


class TestLocalOnly:
    def test_cf_header_rejected(self, client):
        r = client.get("/api/status", headers={"cf-connecting-ip": "1.2.3.4"})
        assert r.status_code == 403

    def test_local_ok(self, client):
        r = client.get("/api/status")
        assert r.status_code == 200
        assert r.json()["ok"]


class TestStatusAndConfig:
    def test_status_shape(self, client):
        data = client.get("/api/status").json()
        assert data["ok"]
        assert data["server"]["running"] is True
        assert data["server"]["tools_count"] == 22
        assert data["mode"] in ("full", "list", "workdir")
        assert "tunnel" in data and "binary_ok" in data["tunnel"]

    def test_config_get_masks_token(self, client):
        data = client.get("/api/config").json()["config"]
        assert data["token_full"] == "test-token-123"
        assert "••••" in data["token_masked"]

    def test_config_post_limits(self, client, tmp_cfg):
        r = client.post("/api/config", json={"max_output_chars": 55555})
        assert r.status_code == 200 and r.json()["ok"]
        assert cfg_mod.CONFIG["max_output_chars"] == 55555

    def test_config_post_bad_number(self, client):
        r = client.post("/api/config", json={"max_output_chars": "abc"})
        assert r.status_code == 400

    def test_workdir_set_and_clear(self, client, tmp_cfg, tmp_path):
        work = tmp_path / "wt"
        work.mkdir()
        r = client.post("/api/config", json={"workdir": str(work)})
        assert r.json()["ok"] and r.json()["mode"] == "workdir"
        assert str(work.resolve()) == cfg_mod.CONFIG["workdir"]
        r = client.post("/api/config", json={"workdir": None})
        assert r.json()["ok"] and r.json()["mode"] == "full"
        assert cfg_mod.CONFIG["workdir"] == ""

    def test_workdir_missing_rejected(self, client, tmp_cfg, tmp_path):
        r = client.post("/api/config", json={"workdir": str(tmp_path / "nope")})
        assert r.status_code == 400

    def test_workdir_app_dir_rejected(self, client, tmp_cfg):
        r = client.post("/api/config", json={"workdir": str(cfg_mod.BASE_DIR)})
        assert r.status_code == 400

    def test_regenerate_token(self, client, tmp_cfg, monkeypatch, tmp_path):
        monkeypatch.setattr(cfg_mod, "ENV_PATH", tmp_path / ".env")
        old = cfg_mod.CONFIG["token"]
        r = client.post("/api/config", json={"regenerate_token": True})
        assert r.json()["ok"]
        new = r.json().get("token")
        assert new and new != old
        assert cfg_mod.CONFIG["token"] == new

    def test_port_change_triggers_restart_flag(self, client, tmp_cfg, monkeypatch):
        monkeypatch.setattr(dash, "_exec_restart", lambda: None)
        r = client.post("/api/config", json={"mcp_port": 8123})
        assert r.json()["restarting"] is True
        assert cfg_mod.CONFIG["port"] == 8123


class TestZonesAndBrowse:
    def test_zones_seeded_desktop(self, client, tmp_cfg, isolated_zones):
        r = client.get("/api/zones").json()
        assert r["ok"]
        assert any(z["zone"] == str(Path.home() / "Desktop") for z in r["zones"])

    def test_revoke_zone(self, client, tmp_cfg, isolated_zones):
        from server import zones as z
        z.ZONES["/tmp/x"] = "approved"
        r = client.post("/api/zones/revoke", json={"zone": "/tmp/x"})
        assert r.json()["ok"] and r.json()["removed"]
        assert "/tmp/x" not in z.ZONES

    def test_browse_home(self, client):
        r = client.get("/api/browse").json()
        assert r["ok"] and r["path"] == str(Path.home().resolve())
        assert isinstance(r["dirs"], list)

    def test_browse_path(self, client, tmp_path):
        d = tmp_path / "folder"
        d.mkdir()
        (d / "sub").mkdir()
        r = client.get("/api/browse", params={"path": str(d)}).json()
        assert r["ok"] and any(d["name"] == "sub" for d in r["dirs"])
        assert r["parent"] == str(tmp_path.resolve())

    def test_browse_file_rejected(self, client, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("x")
        r = client.get("/api/browse", params={"path": str(f)})
        assert r.status_code == 400


class TestConnections:
    def test_shape(self, client, tmp_cfg):
        r = client.get("/api/connections").json()
        assert r["ok"]
        assert r["url_local"].endswith("/mcp")
        assert r["token"] == "test-token-123"
        assert "tunnel_running" in r
        assert r["url_custom"] == "" and r["url_external"] == ""
        assert r["external_source"] == ""


class TestPublicUrl:
    def test_set_domain_defaults_https(self, client, tmp_cfg):
        r = client.post("/api/config", json={"public_url": "mcp.example.com"})
        assert r.json()["ok"]
        assert cfg_mod.CONFIG["public_url"] == "https://mcp.example.com"

    def test_set_ip_defaults_http(self, client, tmp_cfg):
        r = client.post("/api/config", json={"public_url": "203.0.113.5:8000"})
        assert r.json()["ok"]
        assert cfg_mod.CONFIG["public_url"] == "http://203.0.113.5:8000"

    def test_bad_url_rejected_400(self, client, tmp_cfg):
        before = cfg_mod.CONFIG["public_url"]
        r = client.post("/api/config", json={"public_url": "ftp://example.com"})
        assert r.status_code == 400
        assert cfg_mod.CONFIG["public_url"] == before

    def test_custom_wins_in_connections(self, client, tmp_cfg):
        client.post("/api/config", json={"public_url": "mcp.example.com"})
        st = client.get("/api/status").json()
        assert st["public_url"] == "https://mcp.example.com"
        conn = client.get("/api/connections").json()
        assert conn["url_external"] == "https://mcp.example.com"
        assert conn["external_source"] == "custom"

    def test_clear(self, client, tmp_cfg):
        client.post("/api/config", json={"public_url": "mcp.example.com"})
        r = client.post("/api/config", json={"public_url": ""})
        assert r.json()["ok"]
        assert cfg_mod.CONFIG["public_url"] == ""
        conn = client.get("/api/connections").json()
        assert conn["external_source"] == ""  # туннель в тестах не поднят



class TestOnboarding:
    def test_onboarding_required_when_no_workdir(self, client, tmp_cfg):
        st = client.get("/api/status").json()
        # workdir пуст и onboarded не установлен → обязательная модалка первого запуска
        assert st["onboarding_required"] is True
        assert st["workdir_missing"] is False

    def test_workdir_set_marks_onboarded(self, client, tmp_cfg, tmp_path):
        import yaml
        work = tmp_path / "wt"
        work.mkdir()
        r = client.post("/api/config", json={"workdir": str(work)})
        assert r.json()["ok"]
        assert cfg_mod.CONFIG["onboarded"] is True
        st = client.get("/api/status").json()
        assert st["onboarding_required"] is False and st["workdir_missing"] is False
        # флаг переживает сохранение в yaml
        data = yaml.safe_load(cfg_mod.CONFIG_PATH.read_text())
        assert data["onboarded"] is True

    def test_full_access_after_onboarding_not_forced(self, client, tmp_cfg, tmp_path):
        work = tmp_path / "wt"
        work.mkdir()
        client.post("/api/config", json={"workdir": str(work)})
        # пользователь сознательно отключил папку — модалка больше не форсируется
        client.post("/api/config", json={"workdir": None})
        st = client.get("/api/status").json()
        assert st["onboarding_required"] is False

    def test_deleted_workdir_flag_and_no_500(self, client, tmp_cfg, tmp_path):
        work = tmp_path / "gone"
        work.mkdir()
        client.post("/api/config", json={"workdir": str(work)})
        work.rmdir()  # папку удалили, пока сервер работает
        st = client.get("/api/status").json()
        assert st["workdir_missing"] is True and st["mode"] == "workdir"
        # раньше любой POST настроек падал с 500 — теперь работает
        r = client.post("/api/config", json={"max_output_chars": 33333})
        assert r.status_code == 200 and r.json()["ok"]
        # выбор новой папки снимает флаг
        other = tmp_path / "new"
        other.mkdir()
        r = client.post("/api/config", json={"workdir": str(other)})
        assert r.json()["ok"]
        st = client.get("/api/status").json()
        assert st["workdir_missing"] is False


class TestTunnelCheck:
    def test_no_tunnel_400(self, client, tmp_cfg, monkeypatch):
        from server import tunnel
        monkeypatch.setattr(tunnel.MANAGER, "url", "")
        r = client.post("/api/tunnel/check")
        assert r.status_code == 400

    def test_check_ok(self, client, tmp_cfg, monkeypatch):
        from server import tunnel
        monkeypatch.setattr(tunnel.MANAGER, "url", "https://x.trycloudflare.com")
        monkeypatch.setattr(tunnel.MANAGER, "probe_now",
                            lambda: {"state": "ok", "fails": 0})
        r = client.post("/api/tunnel/check").json()
        assert r["ok"] and r["probe"]["state"] == "ok"


class TestReviewFixes:
    """Фиксы по результатам ревью."""

    def test_deny_pattern_invalid_regex_rejected(self, client, tmp_cfg):
        from server import config as cfg_mod, guard
        r = client.post("/api/config", json={"deny_patterns": ["([unclosed"]})
        assert r.status_code == 400
        # CONFIG не мутирован — панель не «кирпичится»
        assert cfg_mod.CONFIG["deny_patterns"] == []
        # повторный валидный запрос работает
        r = client.post("/api/config", json={"deny_patterns": ["sudo"]})
        assert r.json()["ok"]

    def test_workdir_fail_closed_restore(self, client, tmp_cfg, tmp_path):
        from server import config as cfg_mod, guard
        work = tmp_path / "wd"
        work.mkdir()
        cfg_mod.CONFIG["workdir"] = str(work.resolve())
        guard.refresh(cfg_mod.CONFIG)
        # ставим несуществующую папку → отказ, workdir должен остаться прежним
        r = client.post("/api/config", json={"workdir": str(tmp_path / "nope")})
        assert r.status_code == 400
        assert cfg_mod.CONFIG["workdir"] == str(work.resolve())
        assert guard.WORKDIR == work.resolve()

    def test_host_header_rebinding_rejected(self, client, tmp_cfg, monkeypatch):
        # peer у TestClient не локальный, поэтому Host-логику проверяем юнитом
        assert dash._host_header_allowed("evil.example.com") is False
        assert dash._host_header_allowed("127.0.0.1:9999") is False
        assert dash._host_header_allowed(
            f"127.0.0.1:{cfg_mod.CONFIG['dashboard_port']}") is True
        assert dash._host_header_allowed(None) is True

    def test_browse_dirs_have_full_paths(self, client, tmp_path):
        d = tmp_path / "folder"
        d.mkdir()
        (d / "sub").mkdir()
        r = client.get("/api/browse", params={"path": str(d)}).json()
        assert r["ok"]
        entry = next(x for x in r["dirs"] if x["name"] == "sub")
        assert entry["path"] == str(d / "sub")

    def test_audit_tail_bad_value(self, client):
        r = client.get("/api/audit", params={"tail": "abc"})
        assert r.status_code == 400

    def test_zones_added_at_timestamp(self, client, tmp_cfg, isolated_zones):
        from server import zones as z
        z.approve("/tmp/demo")
        added = z.ZONES["/tmp/demo"]
        assert added not in ("approved", "seed") and "T" in added
