"""The job log behind the dashboard's analytics: one SQLite row per finished job."""

from __future__ import annotations

import logging
import math
import sqlite3
import time
from pathlib import Path

from app.jobs import Job

logger = logging.getLogger(__name__)

KEEP_DAYS = 90
DAY = 86400


class Stats:
    def __init__(self, path: Path | None) -> None:
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        # One connection, used only from the event loop thread; writes are tiny.
        self.db = sqlite3.connect(str(path) if path else ":memory:", check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS jobs (finished REAL, model TEXT, style TEXT, status TEXT,"
            " error TEXT, queue_ms INTEGER, run_ms INTEGER, local INTEGER, key TEXT)"
        )
        if "key" not in {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}:
            self.db.execute("ALTER TABLE jobs ADD COLUMN key TEXT")  # logs from before per-app keys
        self.db.execute("DELETE FROM jobs WHERE finished < ?", (time.time() - KEEP_DAYS * DAY,))
        self.db.commit()

    def record(self, job: Job) -> None:
        try:
            finished = job.finished or time.time()
            queue_ms = int(((job.started or finished) - job.created) * 1000)
            err = job.api_error
            self.db.execute(
                "INSERT INTO jobs (finished, model, style, status, error, queue_ms, run_ms, local, key)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (finished, job.model, job.style, job.status, err.code if err else None, queue_ms,
                 job.elapsed_ms, int(job.local), job.key),
            )
            self.db.commit()
        except sqlite3.Error:
            logger.exception("Couldn't record job %s", job.id)  # analytics must never fail a job

    def key_usage(self, now: float | None = None) -> dict[str, tuple[int, float]]:
        """Per key id: (images made in the last 24 hours, when it last made one)."""
        since = (time.time() if now is None else now) - DAY
        return {key: (recent, last) for key, recent, last in self.db.execute(
            "SELECT key, SUM(status = 'done' AND finished >= ?), MAX(finished) FROM jobs"
            " WHERE key IS NOT NULL AND status = 'done' GROUP BY key", (since,)
        )}

    def summary(self, days: int, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        today = int(now // DAY) * DAY  # UTC days
        since = today - (days - 1) * DAY
        q = lambda sql, *a: self.db.execute(sql, (since, *a)).fetchall()

        per_day = {int(d): (done, failed) for d, done, failed in q(
            "SELECT CAST(finished / 86400 AS INTEGER) * 86400, SUM(status = 'done'), SUM(status = 'failed')"
            " FROM jobs WHERE finished >= ? GROUP BY 1"
        )}
        models = []
        for model, count in q("SELECT model, COUNT(*) FROM jobs WHERE finished >= ? GROUP BY model ORDER BY 2 DESC"):
            runs = [r for (r,) in q(
                "SELECT run_ms FROM jobs WHERE finished >= ? AND model = ? AND status = 'done' ORDER BY run_ms", model
            )]
            models.append({"model": model, "jobs": count, "p50_ms": _pct(runs, 0.5), "p95_ms": _pct(runs, 0.95)})
        total, done, failed, cancelled, local = q(
            "SELECT COUNT(*), SUM(status = 'done'), SUM(status = 'failed'), SUM(status = 'cancelled'), SUM(local)"
            " FROM jobs WHERE finished >= ?"
        )[0]
        return {
            "days": [
                {"day": day, "done": per_day.get(day, (0, 0))[0], "failed": per_day.get(day, (0, 0))[1]}
                for day in range(since, today + 1, DAY)
            ],
            "totals": {"jobs": total, "done": done or 0, "failed": failed or 0, "cancelled": cancelled or 0,
                       "local": local or 0},
            "errors": [{"code": c, "count": n} for c, n in q(
                "SELECT error, COUNT(*) FROM jobs WHERE finished >= ? AND error IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"
            )],
            "models": models,
            "styles": [{"style": s, "count": n} for s, n in q(
                "SELECT style, COUNT(*) FROM jobs WHERE finished >= ? AND style IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"
            )],
        }


def _pct(sorted_values: list[int], p: float) -> int | None:
    """Nearest-rank percentile."""
    if not sorted_values:
        return None
    return sorted_values[max(0, min(len(sorted_values) - 1, math.ceil(p * len(sorted_values)) - 1))]
