"""SQLite 持久化。用标准库即可，避免引入 ORM 增加复杂度。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Iterable, Iterator

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    country_code  TEXT NOT NULL,
    source        TEXT NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT NOT NULL UNIQUE,
    published_at  TEXT,
    summary       TEXT DEFAULT '',
    raw_lang      TEXT DEFAULT 'en',
    score         REAL DEFAULT 0,
    score_reason  TEXT DEFAULT '',
    selected      INTEGER DEFAULT 0,
    fetched_body  TEXT DEFAULT '',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_candidates_country ON candidates(country_code);
CREATE INDEX IF NOT EXISTS idx_candidates_score   ON candidates(score DESC);
"""


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_candidates(items: Iterable[dict]) -> int:
    """按 url 去重插入；已存在则跳过。返回新增数量。"""
    inserted = 0
    with connect() as conn:
        for it in items:
            try:
                conn.execute(
                    """
                    INSERT INTO candidates
                        (country_code, source, title, url, published_at, summary,
                         raw_lang, score, score_reason, selected, fetched_body, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        it["country_code"],
                        it["source"],
                        it["title"],
                        it["url"],
                        it.get("published_at"),
                        it.get("summary", ""),
                        it.get("raw_lang", "en"),
                        float(it.get("score", 0.0)),
                        it.get("score_reason", ""),
                        it.get("fetched_body", ""),
                        datetime.utcnow().isoformat(timespec="seconds"),
                    ),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                # URL 已存在则跳过
                pass
    return inserted


def update_score(cand_id: int, score: float, reason: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE candidates SET score = ?, score_reason = ? WHERE id = ?",
            (score, reason, cand_id),
        )


def update_fetched_body(cand_id: int, body: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE candidates SET fetched_body = ? WHERE id = ?",
            (body, cand_id),
        )


def list_candidates(country_code: str | None = None) -> list[sqlite3.Row]:
    with connect() as conn:
        if country_code:
            cur = conn.execute(
                "SELECT * FROM candidates WHERE country_code = ? ORDER BY score DESC, published_at DESC",
                (country_code,),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM candidates ORDER BY country_code, score DESC, published_at DESC"
            )
        return cur.fetchall()


def get_candidates(ids: list[int]) -> list[sqlite3.Row]:
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    with connect() as conn:
        cur = conn.execute(
            f"SELECT * FROM candidates WHERE id IN ({placeholders})",
            ids,
        )
        return cur.fetchall()


def clear_all() -> int:
    with connect() as conn:
        cur = conn.execute("DELETE FROM candidates")
        return cur.rowcount
