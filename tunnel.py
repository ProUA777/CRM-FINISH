# -*- coding: utf-8 -*-
"""
Доступ до CRM з будь-якого місця через Cloudflare Tunnel.

Навіщо: шеф відкриває CRM з дому чи в дорозі просто за посиланням у браузері
телефона - нічого ставити на телефон не треба. Цей модуль сам завантажує
cloudflared (одноразово), запускає його у фоні з токеном тунелю з
налаштувань CRM і перезапускає, якщо він завершився.

Токен передається через змінну середовища TUNNEL_TOKEN, а не аргументом
командного рядка, щоб його не було видно у списку процесів.
"""

import os
import sys
import time
import logging
import threading
import subprocess

log = logging.getLogger("osnastka.tunnel")

CLOUDFLARED_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
MIN_BINARY_BYTES = 5 * 1024 * 1024


def _exe_name():
    return "cloudflared.exe" if sys.platform == "win32" else "cloudflared"


def ensure_cloudflared(data_dir, url=CLOUDFLARED_URL, opener=None):
    """Повертає шлях до cloudflared, завантажуючи його, якщо ще немає.
    Кидає RuntimeError з зрозумілим текстом, якщо не вдалось."""
    path = os.path.join(data_dir, _exe_name())
    if os.path.isfile(path) and os.path.getsize(path) >= MIN_BINARY_BYTES:
        return path
    if sys.platform != "win32" and opener is None:
        raise RuntimeError("Автозавантаження cloudflared підтримується лише у Windows; встановіть cloudflared вручну і покладіть у теку даних")
    os.makedirs(data_dir, exist_ok=True)
    tmp = path + ".part"
    try:
        if opener is not None:
            data = opener(url)
            with open(tmp, "wb") as f:
                f.write(data)
        else:
            import urllib.request
            with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
                while True:
                    chunk = r.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)
        if os.path.getsize(tmp) < MIN_BINARY_BYTES:
            raise RuntimeError("Завантажений файл cloudflared занадто малий (обірване завантаження?)")
        os.replace(tmp, path)
        return path
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Не вдалось завантажити cloudflared: {e}")
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


class TunnelManager:
    """Тримає один процес cloudflared: запуск, зупинка, автоперезапуск."""

    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self._thread = None
        self._stop = threading.Event()
        self.state = "off"      # off | starting | running | error
        self.message = ""
        self._token = None

    def status(self):
        return {"state": self.state, "message": self.message}

    def start(self, token, data_dir, binary=None, backoff=(5, 15, 60)):
        token = (token or "").strip()
        with self._lock:
            if self._thread and self._thread.is_alive() and self._token == token:
                return
        self.stop()
        if not token:
            self.state, self.message = "off", ""
            return
        self._stop.clear()
        self._token = token
        self.state, self.message = "starting", "Запускаю тунель…"
        self._thread = threading.Thread(target=self._run, args=(token, data_dir, binary, backoff), daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        with self._lock:
            proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=3)
        self._thread = None
        self._token = None
        self.state, self.message = "off", ""

    def _run(self, token, data_dir, binary, backoff):
        try:
            exe = binary or ensure_cloudflared(data_dir)
        except Exception as e:
            self.state, self.message = "error", str(e)
            log.error("Тунель: %s", e)
            return
        attempt = 0
        while not self._stop.is_set():
            env = dict(os.environ)
            env["TUNNEL_TOKEN"] = token
            kwargs = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
            try:
                proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "run"], env=env,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
            except Exception as e:
                self.state, self.message = "error", f"Не вдалось запустити cloudflared: {e}"
                log.error(self.message)
                return
            with self._lock:
                self._proc = proc
            started = time.time()
            self.state, self.message = "running", "Тунель працює"
            log.info("Тунель cloudflared запущено")
            while not self._stop.is_set() and proc.poll() is None:
                time.sleep(0.5)
            if self._stop.is_set():
                return
            # процес завершився сам
            attempt = 0 if time.time() - started > 120 else attempt + 1
            delay = backoff[min(attempt, len(backoff) - 1)]
            self.state, self.message = "starting", f"Тунель перезапускається через {delay} с…"
            log.warning("cloudflared завершився (код %s), перезапуск через %s с", proc.returncode, delay)
            if self._stop.wait(delay):
                return


MANAGER = TunnelManager()


def apply_settings(settings, data_dir):
    """Застосовує налаштування CRM: вмикає чи вимикає тунель."""
    enabled = str(settings.get("cf_enabled", "0")) == "1"
    token = (settings.get("cf_tunnel_token") or "").strip()
    if enabled and token:
        MANAGER.start(token, data_dir)
    else:
        MANAGER.stop()
    return MANAGER.status()
