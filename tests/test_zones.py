"""Тесты зон и конфигурации."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from server import config as cfg_mod
from server import zones


class TestZones:
    def test_seed_creates_desktop(self, tmp_path):
        zones.ZONES_FILE = tmp_path / "zones.json"
        z = zones.load_zones()
        assert str(Path.home() / "Desktop") in z

    def test_approve_and_revoke(self, tmp_path):
        zones.ZONES_FILE = tmp_path / "zones.json"
        zones.ZONES = {}
        assert zones.approve("/tmp/demo") is True
        assert zones.approve("/tmp/demo") is False  # повторно — уже была
        assert zones.is_approved("/tmp/demo")
        # персистентность: перечитали с диска
        reloaded = zones.load_zones()
        assert "/tmp/demo" in reloaded
        assert zones.revoke("/tmp/demo") is True
        assert not zones.is_approved("/tmp/demo")

    def test_zone_of_users(self):
        from server.zones import zone_of
        home = Path.home().resolve()
        target = home / "Projects" / "demo"
        assert zone_of(target) == str(home / "Projects")


class TestConfig:
    def test_defaults_applied(self, tmp_cfg):
        assert tmp_cfg["port"] == 8000
        assert tmp_cfg["dashboard_port"] == 8090
        assert tmp_cfg["max_file_bytes"] == 1048576

    def test_validate_clamps_ports(self, tmp_cfg):
        tmp_cfg["port"] = 99999
        cfg_mod._validate(tmp_cfg)
        assert tmp_cfg["port"] == 65535
        tmp_cfg["port"] = -5
        cfg_mod._validate(tmp_cfg)
        assert tmp_cfg["port"] == 1

    def test_save_and_load_roundtrip(self, tmp_cfg, monkeypatch, tmp_path):
        monkeypatch.setattr(cfg_mod, "CONFIG_PATH", tmp_path / "config.yaml")
        tmp_cfg["max_output_chars"] = 12345
        cfg_mod.save_config(tmp_cfg)
        data = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert data["max_output_chars"] == 12345
        assert "token" not in data  # токен не утекает в yaml

    def test_write_env_token_replaces(self, tmp_cfg, monkeypatch, tmp_path):
        monkeypatch.setattr(cfg_mod, "ENV_PATH", tmp_path / ".env")
        monkeypatch.delenv("MCP_TOKEN", raising=False)
        cfg_mod.write_env_token("first")
        assert "MCP_TOKEN=first" in (tmp_path / ".env").read_text()
        cfg_mod.write_env_token("second")
        text = (tmp_path / ".env").read_text()
        assert "MCP_TOKEN=second" in text
        assert "first" not in text

    def test_mask_token(self):
        assert "••••" in cfg_mod.mask_token("abcdefghijklmno")
        assert cfg_mod.mask_token("") == ""

    def test_normalize_public_url(self):
        n = cfg_mod.normalize_public_url
        assert n("") == ""
        assert n("   ") == ""
        # схема по умолчанию: домен → https, IP → http
        assert n("mcp.example.com") == "https://mcp.example.com"
        assert n("203.0.113.5:8000") == "http://203.0.113.5:8000"
        # явная схема и порт сохраняются, /mcp принимается и нормализуется
        assert n("http://example.com:8443/mcp") == "http://example.com:8443/mcp"
        assert n("https://example.com/") == "https://example.com"
        # IPv6 — в скобках
        assert n("http://[::1]:8000") == "http://[::1]:8000"

    def test_normalize_public_url_rejects_junk(self):
        n = cfg_mod.normalize_public_url
        for bad in ("не адрес", "ftp://example.com", "http://a b", "https://x/?token=1",
                    "https://user:pass@example.com", "https://exa mple.com/#f",
                    "https://example.com/other"):
            with pytest.raises(ValueError):
                n(bad)

    def test_validate_public_url_fail_safe(self, tmp_cfg):
        tmp_cfg["public_url"] = "https://good.example.com"
        cfg_mod._validate(tmp_cfg)
        assert tmp_cfg["public_url"] == "https://good.example.com"
        # кривое значение из yaml не роняет запуск — просто игнорируется
        tmp_cfg["public_url"] = "bad url here"
        cfg_mod._validate(tmp_cfg)
        assert tmp_cfg["public_url"] == ""

    def test_ensure_token_generates(self, tmp_cfg, monkeypatch, tmp_path):
        monkeypatch.setattr(cfg_mod, "ENV_PATH", tmp_path / ".env")
        tmp_cfg["token"] = ""
        created = cfg_mod.ensure_token(tmp_cfg)
        assert created and tmp_cfg["token"]
