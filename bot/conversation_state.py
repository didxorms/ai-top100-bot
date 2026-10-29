"""카카오 대화와 OpenAI의 마지막 응답 ID를 연결합니다. 대화 원문은 저장하지 않습니다."""

import hashlib
import json
import sqlite3
import time
from pathlib import Path


def session_key_from_event(event: dict):
    data = event.get("d")
    if not isinstance(data, dict):
        return None
    user = data.get("botUserKey")
    room = data.get("botGroupKey")
    if data.get("isChannelChatroom") is True:
        if not isinstance(user, str) or not user:
            return None
        identity = ["channel", user]
    elif isinstance(room, str) and room:
        # 사용자 선택: 그룹방에서는 방 전체가 하나의 대화를 공유합니다.
        identity = ["group", room]
    else:
        # 식별 정보가 없을 때 모든 요청을 하나의 대화로 합치지 않습니다.
        return None
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ConversationStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS conversations ("
            "session_key TEXT PRIMARY KEY, response_id TEXT NOT NULL)"
        )
        return connection

    def get(self, session_key: str):
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT response_id FROM conversations WHERE session_key = ?", (session_key,)
            ).fetchone()
            return row[0] if row else None
        finally:
            connection.close()

    def set(self, session_key: str, response_id: str):
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    "INSERT INTO conversations (session_key, response_id) VALUES (?, ?) "
                    "ON CONFLICT(session_key) DO UPDATE SET response_id = excluded.response_id",
                    (session_key, response_id),
                )
        finally:
            connection.close()

    def reset(self, session_key: str):
        # 이후 요청이 이전 응답을 참조하지 않게 합니다. OpenAI의 기록 삭제 API는 아닙니다.
        connection = self._connect()
        try:
            with connection:
                connection.execute("DELETE FROM conversations WHERE session_key = ?", (session_key,))
        finally:
            connection.close()

    def claim_callback(self, callback_token: str):
        """동일 콜백은 재시작 후에도 한 번만 접수합니다. 원문 토큰은 저장하지 않습니다."""
        callback_key = hashlib.sha256(callback_token.encode("utf-8")).hexdigest()
        now = time.time()
        connection = self._connect()
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS callback_receipts ("
                "callback_key TEXT PRIMARY KEY, expires_at REAL NOT NULL)"
            )
            with connection:
                connection.execute("DELETE FROM callback_receipts WHERE expires_at <= ?", (now,))
                inserted = connection.execute(
                    "INSERT OR IGNORE INTO callback_receipts (callback_key, expires_at) VALUES (?, ?)",
                    # 콜백 유효 시간 5분보다 길게 보관해 늦은 중복 전송도 걸러냅니다.
                    (callback_key, now + 600),
                ).rowcount
            return inserted == 1
        finally:
            connection.close()

    def check_health(self):
        """DB의 읽기·쓰기를 확인하고 점검용 변경 사항은 되돌립니다."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR REPLACE INTO conversations (session_key, response_id) VALUES (?, ?)",
                ("__healthcheck__", "probe"),
            )
            connection.execute("SELECT response_id FROM conversations LIMIT 1").fetchone()
        finally:
            connection.rollback()
            connection.close()
