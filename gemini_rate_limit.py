"""Pace Gemini requests and keep a daily budget across local runs."""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


DEFAULT_STATE_PATH = Path(__file__).resolve().with_name(".gemini_usage.sqlite3")


def _positive_int_setting(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


class GeminiRateLimiter:
    """Share request spacing and counts between processes in this project."""

    def __init__(
        self,
        model: str,
        state_path: str | Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model
        self.rpm = _positive_int_setting("GEMINI_RPM", 15)
        self.rpd = _positive_int_setting("GEMINI_RPD", 500)
        # A 25% spacing margin makes 15 RPM run at about 12 RPM (5 seconds).
        self.min_interval = 60.0 / self.rpm * 1.25
        self.state_path = Path(state_path) if state_path is not None else DEFAULT_STATE_PATH
        self.clock = clock
        self.sleep = sleep
        self.progress: Callable[[str], None] | None = None
        self.quota_timezone = ZoneInfo("America/Los_Angeles")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.state_path, timeout=10)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS usage ("
            "model TEXT PRIMARY KEY, quota_date TEXT NOT NULL, "
            "request_count INTEGER NOT NULL, last_request_at REAL NOT NULL)"
        )
        return connection

    def _quota_date(self, now: float) -> str:
        return datetime.fromtimestamp(now, self.quota_timezone).date().isoformat()

    def _read_usage(
        self, connection: sqlite3.Connection, quota_date: str
    ) -> tuple[int, float | None]:
        row = connection.execute(
            "SELECT quota_date, request_count, last_request_at FROM usage WHERE model = ?",
            (self.model,),
        ).fetchone()
        if row is None:
            return 0, None
        stored_date, count, last_request_at = row
        return (count if stored_date == quota_date else 0), last_request_at

    def ensure_capacity(self, requests: int) -> int:
        """Reject a batch before sending if its answers exceed the daily budget."""
        connection = self._connect()
        try:
            quota_date = self._quota_date(self.clock())
            count, _ = self._read_usage(connection, quota_date)
        finally:
            connection.close()
        remaining = max(0, self.rpd - count)
        if requests > remaining:
            raise RuntimeError(
                f"Gemini daily request budget: {remaining}/{self.rpd} remaining, "
                f"but this run needs {requests}. Budget resets at midnight Pacific time."
            )
        return remaining

    def wait_for_slot(self) -> None:
        """Reserve one attempt, including attempts that later fail at the API."""
        while True:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                now = self.clock()
                quota_date = self._quota_date(now)
                count, last_request_at = self._read_usage(connection, quota_date)
                if count >= self.rpd:
                    raise RuntimeError(
                        f"Gemini daily request budget exhausted ({count}/{self.rpd}). "
                        "Budget resets at midnight Pacific time."
                    )
                delay = (
                    max(0.0, last_request_at + self.min_interval - now)
                    if last_request_at is not None else 0.0
                )
                if delay == 0:
                    connection.execute(
                        "INSERT INTO usage VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(model) DO UPDATE SET "
                        "quota_date = excluded.quota_date, "
                        "request_count = excluded.request_count, "
                        "last_request_at = excluded.last_request_at",
                        (self.model, quota_date, count + 1, now),
                    )
                    connection.commit()
                    return
                connection.rollback()
            finally:
                connection.close()
            if self.progress is not None:
                self.progress(f"Gemini rate limit: waiting {delay:.1f}s before next request.")
            self.sleep(min(delay, 60.0))
