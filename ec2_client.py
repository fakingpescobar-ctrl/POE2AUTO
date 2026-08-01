"""TCP-клиент для ExileCore2 ShareData плагина.

Подключается к ShareData (C# плагин, инжектируется ВНУТРЬ процесса PoE2 через
ExileCore2) и получает готовый JSON со всеми игровыми данными:
  - awake_entities: список сущностей (монстры, игроки, сундуки) с HP, позициями
  - player_info: HP/Mana игрока, баффы, позиция
  - area_raw_name: код текущей зоны (для определения hideout)
  - game_state: состояние игры (20 = in-game)

Преимущества над PROCESS_VM_READ:
  - НЕ ломается на патчах (оффсеты обновляет сообщество через GameOffsets2.dll)
  - НЕ нужно знать оффсеты — получаем готовые значения
  - Высокая частота обновления (каждый кадр игры, ~60Hz)

Формат запроса:  "/getData?type=partial\\n"
Формат ответа:    JSON + "\\n"

Адаптировано из poe_bot (github.com/Qvin0000, MIT).
"""
from __future__ import annotations

import json
import logging
import socket
import threading
import time
from typing import Optional

_log = logging.getLogger("poe2auto.ec2client")

CONNECT_TIMEOUT = 5.0
RECV_TIMEOUT = 3.0
MAX_RETRIES = 3
RETRY_DELAY = 0.1


class EC2Client:
    """TCP-клиент для общения с ExileCore2 ShareData плагином.

    Thread-safe: все операции защищены локом.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 50006):
        self.host = host
        self.port = port
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._connected = False

    @property
    def connected(self) -> bool:
        """True если TCP-соединение установлено."""
        return self._connected and self._sock is not None

    def connect(self) -> bool:
        """Установить TCP-соединение с ShareData.

        Returns:
            True если соединение успешно (или уже установлено).
        """
        with self._lock:
            if self._connected and self._sock is not None:
                return True
            try:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._sock.settimeout(CONNECT_TIMEOUT)
                self._sock.connect((self.host, self.port))
                self._sock.settimeout(RECV_TIMEOUT)
                self._connected = True
                _log.info("EC2 TCP connected to %s:%d", self.host, self.port)
                return True
            except (socket.error, OSError) as e:
                _log.warning("EC2 TCP connect failed: %s", e)
                self._close_unlocked()
                return False

    def disconnect(self):
        """Закрыть TCP-соединение."""
        with self._lock:
            self._close_unlocked()

    def _close_unlocked(self):
        """Закрыть сокет без блокировки (вызывать под локом)."""
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._connected = False

    def request(self, path: str) -> Optional[dict]:
        """Отправить запрос к ShareData и получить JSON-ответ.

        Args:
            path: Путь запроса, например "/getData?type=partial"

        Returns:
            Распарсенный JSON (dict) или None при ошибке.
        """
        for attempt in range(MAX_RETRIES):
            if not self.connected:
                if not self.connect():
                    time.sleep(RETRY_DELAY)
                    continue

            with self._lock:
                if self._sock is None:
                    continue
                try:
                    self._sock.sendall(f"{path}\n".encode("utf-8"))
                    buffer = b""
                    while True:
                        chunk = self._sock.recv(8192)
                        if not chunk:
                            break
                        buffer += chunk
                        if b"\n" in buffer:
                            response_line, _, _ = buffer.partition(b"\n")
                            return json.loads(response_line.decode("utf-8"))
                except (ConnectionResetError, BrokenPipeError, socket.timeout,
                        OSError, json.JSONDecodeError) as e:
                    _log.debug("EC2 request attempt %d failed: %s", attempt + 1, e)
                    self._close_unlocked()

            time.sleep(RETRY_DELAY)

        _log.warning("EC2 request failed after %d retries: %s", MAX_RETRIES, path)
        return None

    def get_data(self, detailed: bool = False) -> Optional[dict]:
        """Получить данные игры через /getData.

        Args:
            detailed: True для полного ответа (terrain, controller).
                      False для partial (быстрее, достаточно для бота).

        Returns:
            JSON dict или None при ошибке.
        """
        req_type = "full" if detailed else "partial"
        return self.request(f"/getData?type={req_type}")

    def force_refresh_area(self) -> Optional[dict]:
        """Принудительно обновить кеш зоны на стороне ExileCore2."""
        return self.request("/ForceRefreshArea")
