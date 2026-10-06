"""Вход на панель: форма с логином и паролем, сессия в cookie.

Сессии хранятся на диске (только хеши токенов), поэтому переживают перезапуск сервера.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

COOKIE = "session"
REMEMBER_DAYS = 30  # «запомнить меня»
SESSION_HOURS = 24  # без «запомнить меня»
MAX_FAILS = 5  # неверных паролей подряд с одного адреса
LOCK_SECONDS = 60  # на сколько после этого блокируется вход


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    def __init__(self, credentials, store: Path):
        user, _, password = (credentials or "").partition(":")
        self.user, self.password = user or None, password
        self.store = store
        self.lock = threading.Lock()
        self.sessions = {}  # sha256(токен) → когда истекает (unix time)
        self.fails = {}  # ip → (сколько подряд, до какого времени заблокирован)
        self.load()

    @property
    def enabled(self):
        return self.user is not None

    def load(self):
        try:
            data = json.loads(self.store.read_text())
        except (OSError, ValueError):
            return
        now = time.time()
        self.sessions = {h: exp for h, exp in data.items() if exp > now}

    def save(self):
        tmp = self.store.with_suffix(".tmp")
        with self.lock:
            data = json.dumps(self.sessions)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(data)
        tmp.replace(self.store)

    def login(self, ip, user, password):
        """Проверяет пароль. Возвращает (ok, текст ошибки)."""
        now = time.time()
        with self.lock:
            count, until = self.fails.get(ip, (0, 0))
            if until > now:
                return False, f"Слишком много попыток. Подождите {int(until - now) + 1} с."
        ok = (hmac.compare_digest(user.encode(), (self.user or "").encode())
              and hmac.compare_digest(password.encode(), self.password.encode()))
        with self.lock:
            if ok:
                self.fails.pop(ip, None)
                return True, None
            count += 1
            self.fails[ip] = (0, now + LOCK_SECONDS) if count >= MAX_FAILS else (count, 0)
        return False, "Неверный логин или пароль"

    def create(self, remember):
        """Новая сессия. Возвращает (токен, срок жизни cookie в секундах или None)."""
        token = secrets.token_urlsafe(32)
        ttl = REMEMBER_DAYS * 86400 if remember else SESSION_HOURS * 3600
        with self.lock:
            now = time.time()
            self.sessions = {h: exp for h, exp in self.sessions.items() if exp > now}
            self.sessions[_hash(token)] = now + ttl
        self.save()
        return token, ttl if remember else None

    def valid(self, token):
        if not token:
            return False
        with self.lock:
            return self.sessions.get(_hash(token), 0) > time.time()

    def drop(self, token):
        with self.lock:
            removed = self.sessions.pop(_hash(token or ""), None)
        if removed:
            self.save()

    def basic_ok(self, header):
        """HTTP Basic для VLC, curl и скриптов: http://логин:пароль@адрес/stream.mjpg."""
        if not header.startswith("Basic "):
            return False
        expected = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        return hmac.compare_digest(header[6:].strip(), expected)
