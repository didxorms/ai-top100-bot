"""외부 서비스에 영향을 주지 않는 행사 신청 연습용 HTTP API입니다."""

import secrets
import sqlite3
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field


EVENTS = {
    "EVT001": {"title": "AI 자동화 체험", "capacity": 10},
    "EVT002": {"title": "챗봇 제작 실습", "capacity": 2},
}


class PracticeError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class PracticeStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS practice_pending (
                actor TEXT PRIMARY KEY, code TEXT NOT NULL,
                event_id TEXT NOT NULL, quantity INTEGER NOT NULL, expires REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS practice_registrations (
                id TEXT PRIMARY KEY, actor TEXT NOT NULL, code TEXT NOT NULL,
                event_id TEXT NOT NULL, quantity INTEGER NOT NULL,
                UNIQUE(actor, code)
            );
        """)
        return connection

    @staticmethod
    def event(connection, event_id):
        if event_id not in EVENTS:
            raise PracticeError("event_not_found")
        used = connection.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM practice_registrations WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0]
        return {"id": event_id, **EVENTS[event_id], "remaining": EVENTS[event_id]["capacity"] - used,
                "practice_only": True}

    def list_events(self):
        connection = self.connect()
        try:
            return [self.event(connection, key) for key in EVENTS]
        finally:
            connection.close()

    def prepare(self, actor, event_id, quantity):
        connection = self.connect()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                event = self.event(connection, event_id)
                if event["remaining"] < quantity:
                    raise PracticeError("not_enough_seats")
                code = secrets.token_hex(4).upper()
                connection.execute(
                    "INSERT OR REPLACE INTO practice_pending VALUES (?, ?, ?, ?, ?)",
                    (actor, code, event_id, quantity, time.time() + 600),
                )
                return {"status": "confirmation_required", "code": code,
                        "event_id": event_id, "title": event["title"], "quantity": quantity,
                        "practice_only": True}
        finally:
            connection.close()

    def pending(self, actor):
        connection = self.connect()
        try:
            row = connection.execute(
                "SELECT * FROM practice_pending WHERE actor = ? AND expires > ?", (actor, time.time()),
            ).fetchone()
            if row is None:
                return None
            return {"code": row["code"], "title": EVENTS[row["event_id"]]["title"],
                    "quantity": row["quantity"]}
        finally:
            connection.close()

    def cancel(self, actor):
        connection = self.connect()
        try:
            with connection:
                connection.execute("DELETE FROM practice_pending WHERE actor = ?", (actor,))
        finally:
            connection.close()

    def confirm(self, actor, code):
        connection = self.connect()
        try:
            with connection:
                # 잔여석 확인과 신청을 한 트랜잭션으로 처리합니다.
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT id, event_id, quantity FROM practice_registrations WHERE actor = ? AND code = ?",
                    (actor, code),
                ).fetchone()
                if existing:
                    return {**dict(existing), "practice_only": True, "already_confirmed": True}
                row = connection.execute(
                    "SELECT * FROM practice_pending WHERE actor = ? AND code = ?", (actor, code),
                ).fetchone()
                if row is None or row["expires"] <= time.time():
                    raise PracticeError("confirmation_missing_or_expired")
                event = self.event(connection, row["event_id"])
                if event["remaining"] < row["quantity"]:
                    raise PracticeError("not_enough_seats")
                registration = {"id": "PRACTICE-" + secrets.token_hex(6).upper(),
                                "event_id": row["event_id"], "quantity": row["quantity"]}
                connection.execute(
                    "INSERT INTO practice_registrations VALUES (?, ?, ?, ?, ?)",
                    (registration["id"], actor, code, row["event_id"], row["quantity"]),
                )
                connection.execute("DELETE FROM practice_pending WHERE actor = ?", (actor,))
                return {**registration, "practice_only": True, "already_confirmed": False}
        finally:
            connection.close()

    def registrations(self, actor):
        connection = self.connect()
        try:
            rows = connection.execute(
                "SELECT id, event_id, quantity FROM practice_registrations WHERE actor = ? ORDER BY rowid",
                (actor,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()


class Confirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(pattern=r"^[A-F0-9]{8}$", strict=True)


def create_practice_api(store: PracticeStore):
    # 봇 내부의 ASGI 전송으로만 호출합니다. 공개 앱에 mount하지 않습니다.
    api = FastAPI()

    @api.get("/events")
    def events():
        return {"events": store.list_events(), "practice_only": True}

    @api.get("/events/{event_id}")
    def event(event_id: str):
        for item in store.list_events():
            if item["id"] == event_id:
                return item
        raise HTTPException(404, detail="event_not_found")

    @api.get("/registrations")
    def registrations(x_practice_actor: str = Header(min_length=1)):
        return {"registrations": store.registrations(x_practice_actor), "practice_only": True}

    @api.post("/registrations/confirm")
    def confirm(body: Confirmation, x_practice_actor: str = Header(min_length=1)):
        try:
            return store.confirm(x_practice_actor, body.code)
        except PracticeError as exc:
            raise HTTPException(409, detail=exc.code) from exc

    return api
