"""SQLite persistence for activations.

Railway containers have an ephemeral filesystem, so point DB_PATH at a mounted
volume (e.g. /data/bot.db) if you want history to survive redeploys. Live
activations are rehydrated on boot either way, so a redeploy mid-activation
does not orphan a number you already paid for.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

LIVE = "live"
DONE = "done"
CANCELLED = "cancelled"
EXPIRED = "expired"

SCHEMA = """
CREATE TABLE IF NOT EXISTS activations (
    act_id      TEXT PRIMARY KEY,
    phone       TEXT NOT NULL,
    service     TEXT NOT NULL,
    country     TEXT NOT NULL,
    chat_id     INTEGER NOT NULL,
    message_id  INTEGER,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    state       TEXT NOT NULL,
    codes       TEXT NOT NULL DEFAULT '[]',
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_state ON activations(state);
"""


@dataclass
class Activation:
    act_id: str
    phone: str
    service: str
    country: str
    chat_id: int
    created_at: float
    expires_at: float
    state: str = LIVE
    message_id: int | None = None
    codes: list[str] = field(default_factory=list)
    note: str | None = None

    @property
    def seconds_left(self) -> int:
        return max(0, int(self.expires_at - time.time()))

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at


class Store:
    def __init__(self, path: str) -> None:
        db_path = Path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _row_to_activation(row: sqlite3.Row) -> Activation:
        return Activation(
            act_id=row["act_id"],
            phone=row["phone"],
            service=row["service"],
            country=row["country"],
            chat_id=row["chat_id"],
            message_id=row["message_id"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            state=row["state"],
            codes=json.loads(row["codes"] or "[]"),
            note=row["note"],
        )

    def insert(self, act: Activation) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO activations
               (act_id, phone, service, country, chat_id, message_id,
                created_at, expires_at, state, codes, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                act.act_id, act.phone, act.service, act.country, act.chat_id,
                act.message_id, act.created_at, act.expires_at, act.state,
                json.dumps(act.codes), act.note,
            ),
        )
        self._conn.commit()

    def update(self, act: Activation) -> None:
        self._conn.execute(
            """UPDATE activations
               SET state=?, codes=?, message_id=?, note=?, expires_at=?
               WHERE act_id=?""",
            (
                act.state, json.dumps(act.codes), act.message_id,
                act.note, act.expires_at, act.act_id,
            ),
        )
        self._conn.commit()

    def get(self, act_id: str) -> Activation | None:
        row = self._conn.execute(
            "SELECT * FROM activations WHERE act_id=?", (act_id,)
        ).fetchone()
        return self._row_to_activation(row) if row else None

    def live(self) -> list[Activation]:
        rows = self._conn.execute(
            "SELECT * FROM activations WHERE state=? ORDER BY created_at", (LIVE,)
        ).fetchall()
        return [self._row_to_activation(r) for r in rows]

    def recent(self, limit: int = 15) -> list[Activation]:
        rows = self._conn.execute(
            "SELECT * FROM activations ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_activation(r) for r in rows]

    def stats(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT state, COUNT(*) AS n FROM activations GROUP BY state"
        ).fetchall()
        out = {r["state"]: r["n"] for r in rows}
        codes = self._conn.execute(
            "SELECT codes FROM activations"
        ).fetchall()
        out["codes_received"] = sum(len(json.loads(r["codes"] or "[]")) for r in codes)
        return out
