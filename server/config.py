"""Конфигурация GlassMCP: config.yaml + .env, сохранение из панели."""

from __future__ import annotations

import ipaddress
import os
import platform
import re
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent  # папка APP
CONFIG_PATH = BASE_DIR / "config.yaml"
ENV_PATH = BASE_DIR / ".env"

IS_WINDOWS = os.name == "nt" or platform.system().lower() == "windows"

DEFAULTS: dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 8000,            # MCP-порт (наружу через туннель)
    "dashboard_port": 8090,  # панель: только 127.0.0.1
    "public_url": "",        # свой внешний адрес (IP/домен), если настроен снаружи
    "workdir": "",           # "" = полный доступ; путь = строгий режим
    "onboarded": False,      # рабочая папка уже выбиралась (первый запуск пройден)
    "allowed_dirs": ["*"],   # используется только когда workdir пуст
    "max_file_bytes": 1_048_576,
    "max_output_chars": 20_000,
    "default_shell_timeout": 30,
    "max_shell_timeout": 120,
    "deny_patterns": [],
    "deny_paths": [],
    "audit_log": "./audit.log",
}


def _load_dotenv() -> None:
    """Минимальный .env-лоадер без зависимостей: KEY=VALUE, кавычки, #."""
    if not ENV_PATH.is_file():
        return
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val


def load_config() -> dict[str, Any]:
    _load_dotenv()
    cfg: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    merged = {**DEFAULTS, **{k: v for k, v in cfg.items() if v is not None}}
    # env имеет приоритет над yaml
    if os.getenv("MCP_TOKEN"):
        merged["token"] = os.environ["MCP_TOKEN"]
    if os.getenv("MCP_PORT"):
        merged["port"] = int(os.environ["MCP_PORT"])
    if os.getenv("DASHBOARD_PORT"):
        merged["dashboard_port"] = int(os.environ["DASHBOARD_PORT"])
    _validate(merged)
    return merged


_HOST_LABEL_RX = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def normalize_public_url(raw: Any) -> str:
    """Свой внешний адрес: http(s)://IP-or-hostname[:port][/mcp].

    Схему подставляем сами: http:// для IP, https:// для домена.
    Пустая строка = адрес не задан. Ошибки — ValueError с текстом для панели.
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    if any(ch.isspace() for ch in s):
        raise ValueError("адрес не должен содержать пробелы")
    if "://" not in s:
        host_guess = ""
        try:
            host_guess = urlsplit("//" + s).hostname or ""
        except ValueError:
            pass
        s = ("http://" if _is_ip(host_guess) else "https://") + s
    try:
        parts = urlsplit(s)
    except ValueError:
        raise ValueError("не похоже на URL — пример: https://mcp.example.com или http://1.2.3.4:8000")
    if parts.scheme not in ("http", "https"):
        raise ValueError("поддерживаются только http:// и https://")
    if parts.username or parts.password:
        raise ValueError("логин/пароль в адресе нельзя — используй токен доступа")
    if parts.query or parts.fragment:
        raise ValueError("без ?query и #fragment — токен клиент передаёт отдельно")
    try:
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        raise ValueError("некорректный порт или скобки в адресе")
    if not host:
        raise ValueError("не указан хост (IP или домен)")
    if not _is_ip(host):
        labels = host.split(".")
        if len(host) > 253 or any(not _HOST_LABEL_RX.match(l) for l in labels):
            raise ValueError(f"некорректный домен: {host}")
    path = parts.path.rstrip("/")
    if path not in ("", "/mcp"):
        raise ValueError("путь должен быть пустым или /mcp — остальное добавляет клиент")
    if _is_ip(host) and ":" in host:
        host = f"[{host}]"  # IPv6 в URL — в скобках
    netloc = host + (f":{port}" if port else "")
    return f"{parts.scheme}://{netloc}{path}"


def _validate(cfg: dict[str, Any]) -> None:
    cfg["host"] = str(cfg.get("host") or "127.0.0.1")
    cfg["port"] = max(1, min(65535, int(cfg.get("port") or 8000)))
    cfg["dashboard_port"] = max(1, min(65535, int(cfg.get("dashboard_port") or 8090)))
    cfg["max_file_bytes"] = max(1024, int(cfg.get("max_file_bytes") or DEFAULTS["max_file_bytes"]))
    cfg["max_output_chars"] = max(1000, int(cfg.get("max_output_chars") or DEFAULTS["max_output_chars"]))
    cfg["default_shell_timeout"] = max(1, int(cfg.get("default_shell_timeout") or 30))
    cfg["max_shell_timeout"] = max(1, int(cfg.get("max_shell_timeout") or 120))
    if cfg["default_shell_timeout"] > cfg["max_shell_timeout"]:
        cfg["default_shell_timeout"] = cfg["max_shell_timeout"]
    if not isinstance(cfg.get("allowed_dirs"), list):
        cfg["allowed_dirs"] = ["*"]
    if not isinstance(cfg.get("deny_patterns"), list):
        cfg["deny_patterns"] = []
    if not isinstance(cfg.get("deny_paths"), list):
        cfg["deny_paths"] = []
    cfg["workdir"] = str(cfg.get("workdir") or "").strip()
    cfg["onboarded"] = bool(cfg.get("onboarded"))
    cfg["token"] = str(cfg.get("token") or "")
    # кривой public_url из yaml не должен ронять запуск: просто игнорируем его
    try:
        cfg["public_url"] = normalize_public_url(cfg.get("public_url"))
    except ValueError:
        cfg["public_url"] = ""


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def write_env_token(token: str) -> None:
    """Записать MCP_TOKEN в .env (создать/обновить, не трогая другие строки)."""
    lines: list[str] = []
    if ENV_PATH.is_file():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    out, replaced = [], False
    for line in lines:
        if line.strip().startswith("MCP_TOKEN="):
            out.append(f"MCP_TOKEN={token}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"MCP_TOKEN={token}")
    ENV_PATH.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    os.environ["MCP_TOKEN"] = token


def ensure_token(cfg: dict[str, Any]) -> bool:
    """Если токена нет — сгенерировать и сохранить. True если создан новый."""
    if cfg.get("token") and not str(cfg["token"]).startswith("CHANGE_ME"):
        return False
    tok = generate_token()
    write_env_token(tok)
    cfg["token"] = tok
    return True


def regenerate_token(cfg: dict[str, Any]) -> str:
    tok = generate_token()
    write_env_token(tok)
    cfg["token"] = tok
    return tok


def save_config(cfg: dict[str, Any]) -> None:
    """Сохранить настройки в config.yaml. Токен в yaml НЕ пишется (живёт в .env)."""
    _validate(cfg)
    data = {k: v for k, v in cfg.items() if k != "token"}
    CONFIG_PATH.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def audit_log_path(cfg: dict[str, Any]) -> Path:
    raw = str(cfg.get("audit_log") or "./audit.log")
    p = Path(raw)
    if not p.is_absolute() and not (IS_WINDOWS and re.match(r"^[A-Za-z]:[\\/]", raw)):
        p = BASE_DIR / p
    return p


def mask_token(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 10:
        return token[:2] + "••••"
    return f"{token[:6]}••••{token[-4:]}"


# Модульный CONFIG: все модули читают его на вызове (панель мутирует напрямую).
CONFIG = load_config()
