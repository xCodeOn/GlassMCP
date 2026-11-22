"""Управление Cloudflare-туннелем: бинарник по платформе, старт/стоп, URL.

Quick-туннель (без регистрации) на http2 — стабильнее в сетях, где
душат QUIC/UDP. Именованный туннель со своим доменом — через config
TUNNEL_NAME/PUBLIC_URL вручную (см. README), панель управляет quick.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from pathlib import Path

from . import config as cfg_mod

CF_VERSION = "2026.11.1"
CF_BASE = f"https://github.com/cloudflare/cloudflared/releases/download/{CF_VERSION}"

BIN_DIR = cfg_mod.BASE_DIR / "bin"
LOG_PATH = cfg_mod.BASE_DIR / "logs" / "cloudflared.log"
PID_FILE = cfg_mod.BASE_DIR / ".tunnel_pid"

URL_RX = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

# --- живая проверка туннеля: GET {url}/health через Cloudflare edge ---
PROBE_INTERVAL_S = 30    # как часто проверять публичный адрес
PROBE_FIRST_DELAY_S = 6  # пауза после получения URL перед первой проверкой
PROBE_TIMEOUT_S = 8      # таймаут одного запроса
PROBE_FAILS_ALARM = 2    # провалов подряд → «туннель отвалился»

# платформа → (имя архива/бинарника, бинарник внутри архива или None)
PLATFORM_ASSETS: dict[tuple[str, str], tuple[str, str | None]] = {
    ("darwin", "arm64"): ("cloudflared-darwin-arm64.tgz", "cloudflared"),
    ("darwin", "amd64"): ("cloudflared-darwin-amd64.tgz", "cloudflared"),
    ("linux", "x86_64"): ("cloudflared-linux-amd64", None),
    ("linux", "aarch64"): ("cloudflared-linux-arm64", None),
    ("windows", "amd64"): ("cloudflared-windows-amd64.exe", None),
}


def _platform_key() -> tuple[str, str]:
    system = sys.platform
    if system == "darwin":
        system = "darwin"
    elif os.name == "nt":
        system = "windows"
    elif not system.startswith("linux"):
        system = "linux"
    machine = os.uname().machine.lower() if os.name != "nt" else os.environ.get("PROCESSOR_ARCHITECTURE", "AMD64").lower()
    if machine in ("arm64", "aarch64"):
        return system, "arm64" if system != "windows" else "arm64"
    if machine in ("x86_64", "amd64"):
        return system, "amd64"
    return system, "amd64"


def binary_path() -> Path:
    name = "cloudflared.exe" if os.name == "nt" else "cloudflared"
    return BIN_DIR / name


def pid_alive(pid: int) -> bool:
    """Кроссплатформенная проверка «жив ли процесс».
    os.kill(pid, 0) на Windows терминирует процесс — потому tasklist."""
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                                 capture_output=True, text=True).stdout or ""
            return str(pid) in out
        except OSError:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # процесс есть, но чужой


def binary_ok() -> bool:
    return binary_path().is_file()


class TunnelManager:
    """Жизненный цикл quick-туннеля. Один экземпляр на процесс."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.attached_pid: int | None = None  # выживший после exec-рестарта
        self.url: str = ""
        self.error: str = ""
        self.starting: bool = False
        self.downloading: bool = False
        self.mode: str = "quick"
        self._url_thread: threading.Thread | None = None
        # живость туннеля: процесс может быть жив, а адрес — не отвечать
        self.probe: dict = self._new_probe()
        self._probe_stop = threading.Event()
        self._probe_thread: threading.Thread | None = None
        self.reattach()

    # ---------- состояние ----------

    def _attached_alive(self) -> bool:
        return self.attached_pid is not None and pid_alive(self.attached_pid)

    def status(self) -> dict:
        alive = (self.proc is not None and self.proc.poll() is None) \
            or self._attached_alive()
        return {
            "running": alive and bool(self.url),
            "starting": self.starting or (alive and not self.url),
            "mode": self.mode,
            "url": self.url,
            "error": self.error,
            "binary_ok": binary_ok(),
            "downloading": self.downloading,
            "probe": dict(self.probe),
        }

    # ---------- повторное присоединение после рестарта процесса ----------

    def reattach(self) -> None:
        """После os.execv-рестарта: cloudflared мог выжить. Если pid жив и в
        логе есть URL — считаем туннель работающим (attached_pid)."""
        try:
            if not PID_FILE.is_file():
                return
            pid = int(PID_FILE.read_text().strip())
            if not pid_alive(pid):
                PID_FILE.unlink(missing_ok=True)
                return
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            if LOG_PATH.is_file():
                text = LOG_PATH.read_text(encoding="utf-8", errors="replace")
                m = URL_RX.findall(text)
                if m:
                    self.url = m[-1]
                    self.mode = "quick"
                    self.attached_pid = pid
                    self._start_probe(self.url)  # подхваченный туннель тоже проверяем
                    return
            # процесс жив, но URL в логе нет — туннель сломан, гасим.
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True)
            else:
                subprocess.run(["kill", str(pid)], capture_output=True)
            PID_FILE.unlink(missing_ok=True)
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            PID_FILE.unlink(missing_ok=True)

    # ---------- скачивание бинарника ----------

    def _download(self) -> None:
        try:
            self.downloading = True
            BIN_DIR.mkdir(parents=True, exist_ok=True)
            system, arch = _platform_key()
            asset, inner = PLATFORM_ASSETS.get(
                (system, "arm64" if arch == "arm64" else "amd64"), (None, None))
            if not asset:
                self.error = f"нет сборки cloudflared для {system}/{arch}"
                return
            src = f"{CF_BASE}/{asset}"
            tmp = BIN_DIR / (asset + ".part")
            self.error = ""
            # urlretrieve без таймаута зависает навсегда — urlopen с timeout
            with urllib.request.urlopen(src, timeout=120) as resp, open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
            dst = binary_path()
            if asset.endswith(".tgz"):
                with tarfile.open(tmp, "r:gz") as tar:
                    member = next((m for m in tar.getmembers()
                                   if m.name.split("/")[-1] == (inner or "cloudflared")), None)
                    if not member:
                        raise RuntimeError("cloudflared не найден в архиве")
                    member.name = dst.name
                    tar.extract(member, BIN_DIR, filter="data")
                tmp.unlink(missing_ok=True)
            else:
                dst.unlink(missing_ok=True)
                os.replace(tmp, dst)
            dst.chmod(0o755)
        except Exception as e:  # noqa: BLE001
            self.error = f"скачивание не удалось: {type(e).__name__}: {e}"
        finally:
            self.downloading = False

    def download_async(self) -> None:
        threading.Thread(target=self._download, daemon=True).start()

    # ---------- проверка живости (GET {url}/health по таймеру) ----------

    @staticmethod
    def _new_probe() -> dict:
        return {"state": "unknown", "fails": 0, "last_error": "",
                "last_ok_at": None, "checked_at": None}

    def _reset_probe(self) -> None:
        self._probe_stop.set()
        self.probe = self._new_probe()

    def _start_probe(self, url: str) -> None:
        """Запустить цикл проверки для текущего URL (старый цикл гасим)."""
        self._probe_stop.set()
        self._probe_stop = threading.Event()
        self.probe = self._new_probe()
        self._probe_thread = threading.Thread(
            target=self._probe_loop, args=(url,), daemon=True)
        self._probe_thread.start()

    def _probe_once(self, url: str) -> tuple[bool, str]:
        """Публичный адрес должен ответить /health с ok:true — иначе туннель
        не работает, даже если процесс cloudflared жив."""
        target = url.rstrip("/") + "/health"
        try:
            req = urllib.request.Request(
                target, headers={"User-Agent": "glassmcp-probe"})
            with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as resp:
                body = resp.read(4096)
            try:
                data = json.loads(body.decode("utf-8", errors="replace"))
            except ValueError:
                return False, f"/health вернул не-JSON ({body[:80]!r})"
            if isinstance(data, dict) and data.get("ok") is True:
                return True, ""
            return False, f"/health ответил без ok:true ({body[:120]!r})"
        except Exception as e:  # noqa: BLE001 — любая сетевая ошибка = не отвечает
            return False, f"{type(e).__name__}: {e}"

    def _update_probe(self, ok: bool, error: str) -> dict:
        p = self.probe
        p["checked_at"] = time.time()
        if ok:
            p["state"], p["fails"], p["last_error"] = "ok", 0, ""
            p["last_ok_at"] = p["checked_at"]
        else:
            p["fails"] += 1
            p["last_error"] = error
            # одиночный сбой — не тревога (сеть моргнула), ждём второго
            p["state"] = "fail" if p["fails"] >= PROBE_FAILS_ALARM else "checking"
        return dict(p)

    def probe_now(self) -> dict:
        """Разовая проверка по запросу (кнопка в панели)."""
        if not self.url:
            return {**self.probe, "last_error": "туннель не запущен"}
        ok, err = self._probe_once(self.url)
        return self._update_probe(ok, err)

    def _probe_loop(self, url: str) -> None:
        stop = self._probe_stop
        stop.wait(PROBE_FIRST_DELAY_S)
        while not stop.is_set():
            if self.url != url:
                return  # туннель перезапустили — цикл больше не нужен
            ok, err = self._probe_once(url)
            self._update_probe(ok, err)
            stop.wait(PROBE_INTERVAL_S)

    # ---------- старт/стоп ----------

    def start_quick(self, mcp_port: int) -> dict:
        already = (self.proc is not None and self.proc.poll() is None) \
            or self._attached_alive()
        if already and self.url:
            # после exec-рестарта цикл проверки мог не подняться — страхуемся
            if self._probe_thread is None or not self._probe_thread.is_alive():
                self._start_probe(self.url)
            return {"ok": True, "already": True, "url": self.url}
        if self.starting:
            return {"ok": True, "starting": True}
        if not binary_ok():
            if self.downloading:
                return {"ok": False, "error": "cloudflared ещё скачивается, повтори через минуту"}
            self.error = ""
            self.download_async()
            return {"ok": False, "error": "cloudflared не найден — скачиваю, повтори через минуту"}
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        # offset конца лога: ищем URL только среди НОВЫХ строк, иначе
        # подхватим адрес прошлой сессии
        try:
            log_offset = LOG_PATH.stat().st_size
        except OSError:
            log_offset = 0
        log = open(LOG_PATH, "a", encoding="utf-8")
        try:
            self.proc = subprocess.Popen(
                [str(binary_path()), "tunnel", "--protocol", "http2",
                 "--url", f"http://127.0.0.1:{mcp_port}"],
                stdout=log, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=(os.name != "nt"),
            )
        except OSError as e:
            log.close()
            self.error = f"не запустить cloudflared: {e}"
            return {"ok": False, "error": self.error}
        PID_FILE.write_text(str(self.proc.pid), encoding="utf-8")
        self.url, self.error, self.starting, self.mode = "", "", True, "quick"
        self._reset_probe()
        self._url_thread = threading.Thread(
            target=self._wait_url, args=(log_offset,), daemon=True)
        self._url_thread.start()
        return {"ok": True, "starting": True}

    def _wait_url(self, offset: int = 0, timeout_s: int = 40) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.proc is None or self.proc.poll() is not None:
                self.starting = False
                self.error = "cloudflared завершился сразу — смотри logs/cloudflared.log"
                return
            try:
                with open(LOG_PATH, "rb") as f:
                    f.seek(offset)
                    text = f.read().decode("utf-8", errors="replace")
                m = URL_RX.findall(text)
                if m:
                    self.url = m[-1]
                    self.error = ""
                    self.starting = False
                    self._start_probe(self.url)
                    return
            except OSError:
                pass
            time.sleep(1)
        self.starting = False
        if not self.url:
            self.error = f"URL не получен за {timeout_s}s — проверь logs/cloudflared.log"

    def stop(self) -> dict:
        was = (self.proc is not None and self.proc.poll() is None) \
            or self._attached_alive()
        self._reset_probe()
        if self.proc is not None:
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            except OSError:
                pass
            self.proc = None
        if PID_FILE.exists():
            try:
                pid = int(PID_FILE.read_text().strip())
                if pid_alive(pid):
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                       capture_output=True)
                    else:
                        os.kill(pid, 15)
            except (ValueError, ProcessLookupError, PermissionError, OSError):
                pass
            PID_FILE.unlink(missing_ok=True)
        self.url, self.starting, self.attached_pid = "", False, None
        return {"ok": True, "was_running": was}

    def shutdown(self) -> None:
        """При завершении сервера."""
        try:
            self.stop()
        except Exception:  # noqa: BLE001
            pass


MANAGER = TunnelManager()
