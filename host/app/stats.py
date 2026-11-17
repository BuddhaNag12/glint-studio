"""The job log behind the dashboard's analytics: one SQLite row per finished job."""

from __future__ import annotations

import datetime as dt
import logging
import math
import sqlite3
import time
from pathlib import Path

from app.jobs import Job
from app.model_folders import get_setting

logger = logging.getLogger(__name__)

KEEP_DAYS = 400
DAY = 86400
RANGES = {"today": 1, "yesterday": 1, "7d": 7, "30d": 30, "90d": 90, "month": 0}


def day_bounds(
    range_: str | None = None,
    days: int | None = None,
    start: str | None = None,
    end: str | None = None,
    tz: int = 0,
    now: float | None = None,
) -> tuple[int, int]:
    """First and last day (days since 1970-01-01, in the caller's time zone) of a named range,
    `days` ending today, or ISO dates. Raises ValueError for anything else."""
    now = time.time() if now is None else now
    today = int((now + tz * 60) // DAY)
    if start or end:
        first, last = (dt.date.fromisoformat(d or "") - dt.date(1970, 1, 1) for d in (start, end))
        first, last = first.days, last.days
        if first > last or last - first >= KEEP_DAYS:
            raise ValueError(f"Pick up to {KEEP_DAYS} days, with from before to.")
        return first, last
    if range_ == "yesterday":
        return today - 1, today - 1
    if range_ == "month":
        return today - (dt.date(1970, 1, 1) + dt.timedelta(days=today)).day + 1, today
    n = RANGES.get(range_, 0) if range_ else days
    if not n:
        raise ValueError(f"Range must be one of {', '.join(RANGES)}.")
    return today - n + 1, today


class Stats:
    def __init__(self, path: Path | None, engine_file: Path | None = None) -> None:
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.engine_file = engine_file
        self.started = time.time()  # "This session" means since the host started
        # One connection, used only from the event loop thread; writes are tiny.
        self.db = sqlite3.connect(str(path) if path else ":memory:", check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS jobs (finished REAL, model TEXT, style TEXT, status TEXT,"
            " error TEXT, queue_ms INTEGER, run_ms INTEGER, local INTEGER, key TEXT)"
        )
        if "key" not in {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}:
            self.db.execute("ALTER TABLE jobs ADD COLUMN key TEXT")  # logs from before per-app keys
        # Key daily caps count from their own table, so turning usage off or clearing it can't lift a cap.
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'key_jobs'").fetchone():
            self.db.execute("CREATE TABLE key_jobs (key TEXT, finished REAL, images INTEGER NOT NULL DEFAULT 1)")
            self.db.execute(
                "INSERT INTO key_jobs (key, finished) SELECT key, finished FROM jobs WHERE key IS NOT NULL AND status = 'done'"
            )
        if "images" not in {row[1] for row in self.db.execute("PRAGMA table_info(key_jobs)")}:
            self.db.execute("ALTER TABLE key_jobs ADD COLUMN images INTEGER NOT NULL DEFAULT 1")  # caps once counted jobs
        self._prune()

    def recording(self) -> bool:
        value = get_setting(self.engine_file, "record_usage", True)
        return value if isinstance(value, bool) else True

    def _prune(self) -> None:
        cutoff = time.time() - KEEP_DAYS * DAY
        self.db.execute("DELETE FROM jobs WHERE finished < ?", (cutoff,))
        self.db.execute("DELETE FROM key_jobs WHERE finished < ?", (cutoff,))
        self.db.commit()

    def record(self, job: Job) -> None:
        try:
            finished = job.finished or time.time()
            if job.key not in (None, "main") and job.status == "done":
                images = len(job.result.images) if job.result else 1
                self.db.execute("INSERT INTO key_jobs VALUES (?, ?, ?)", (job.key, finished, images))
            if self.recording():
                queue_ms = int(((job.started or finished) - job.created) * 1000)
                err = job.api_error
                self.db.execute(
                    "INSERT INTO jobs (finished, model, style, status, error, queue_ms, run_ms, local, key)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (finished, job.model, job.style, job.status, err.code if err else None, queue_ms,
                     job.elapsed_ms, int(job.local), job.key),
                )
            self._prune()
        except sqlite3.Error:
            logger.exception("Couldn't record job %s", job.id)  # analytics must never fail a job

    def clear(self) -> None:
        self.db.execute("DELETE FROM jobs")
        self.db.commit()

    def key_usage(self, now: float | None = None) -> dict[str, tuple[int, float]]:
        """Per key id: (images made in the last 24 hours, when it last made one)."""
        since = (time.time() if now is None else now) - DAY
        return {key: (recent, last) for key, recent, last in self.db.execute(
            "SELECT key, SUM((finished >= ?) * images), MAX(finished) FROM key_jobs GROUP BY key", (since,)
        )}

    def summary(
        self, first: int, last: int, tz: int = 0, model: str | None = None, session: bool = False
    ) -> dict:
        """Days `first`..`last` (from day_bounds) in a time zone `tz` minutes east of UTC. Day
        numbers count local days, so `day` (n * 86400) reads right as a UTC date.
        shortcut: one fixed offset for the whole range, so an hour can shift across a DST change."""
        off = int(tz) * 60
        lo, hi = first * DAY - off, (last + 1) * DAY - off
        if session:
            lo = max(lo, self.started)

        def scoped(lo: float, hi: float):
            w = "finished >= ? AND finished < ?" + (" AND model = ?" if model else "")
            args = (lo, hi, *([model] if model else []))
            return lambda sql, *a: self.db.execute(sql.format(w=w), (*args, *a)).fetchall()

        q = scoped(lo, hi)
        totals = "SELECT COUNT(*), SUM(status = 'done'), SUM(status = 'failed'), SUM(status = 'cancelled'), SUM(local) FROM jobs WHERE {w}"
        per_day = {int(d): (done, failed) for d, done, failed in q(
            f"SELECT CAST((finished + {off}) / 86400 AS INTEGER), SUM(status = 'done'), SUM(status = 'failed')"
            " FROM jobs WHERE {w} GROUP BY 1"
        )}
        heatmap = [[0] * 24 for _ in range(7)]  # Monday first; 1970-01-01 was a Thursday
        for weekday, hour, n in q(
            f"SELECT (CAST((finished + {off}) / 86400 AS INTEGER) + 3) % 7, ((CAST(finished AS INTEGER) + {off}) % 86400) / 3600,"
            " COUNT(*) FROM jobs WHERE {w} AND status = 'done' GROUP BY 1, 2"
        ):
            heatmap[weekday][hour] = n
        # The contribution calendar: 53 Monday-first weeks ending today, whatever range is picked.
        today = int((time.time() + off) // DAY)
        start = today - (today + 3) % 7 - 52 * 7
        done_by_day = dict(scoped(start * DAY - off, (today + 1) * DAY - off)(
            f"SELECT CAST((finished + {off}) / 86400 AS INTEGER), COUNT(*) FROM jobs WHERE {{w}} AND status = 'done' GROUP BY 1"
        ))
        models = []
        for name, count in q("SELECT model, COUNT(*) FROM jobs WHERE {w} GROUP BY model ORDER BY 2 DESC"):
            runs = [r for (r,) in q(
                "SELECT run_ms FROM jobs WHERE {w} AND model = ? AND status = 'done' ORDER BY run_ms", name
            )]
            models.append({"model": name, "jobs": count, "p50_ms": _pct(runs, 0.5), "p95_ms": _pct(runs, 0.95)})
        total, done, failed, cancelled, local = q(totals)[0]
        previous = None
        if not session:  # the same number of days just before
            span = hi - lo
            _, p_done, p_failed, _, _ = scoped(lo - span, lo)(totals)[0]
            previous = {"done": p_done or 0, "failed": p_failed or 0}
        return {
            "from": first * DAY,
            "to": last * DAY,
            "days": [
                {"day": day * DAY, "done": per_day.get(day, (0, 0))[0], "failed": per_day.get(day, (0, 0))[1]}
                for day in range(first, last + 1)
            ],
            "totals": {"jobs": total, "done": done or 0, "failed": failed or 0, "cancelled": cancelled or 0,
                       "local": local or 0},
            "previous": previous,
            "errors": [{"code": c, "count": n} for c, n in q(
                "SELECT error, COUNT(*) FROM jobs WHERE {w} AND error IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"
            )],
            "models": models,
            "styles": [{"style": s, "count": n} for s, n in q(
                "SELECT style, COUNT(*) FROM jobs WHERE {w} AND style IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"
            )],
            "heatmap": heatmap,
            "year": {"from": start * DAY, "done": [done_by_day.get(d, 0) for d in range(start, today + 1)]},
            "all_models": [m for (m,) in self.db.execute("SELECT DISTINCT model FROM jobs ORDER BY 1")],
            "session_started": self.started,
            "recording": self.recording(),
        }


def _pct(sorted_values: list[int], p: float) -> int | None:
    """Nearest-rank percentile."""
    if not sorted_values:
        return None
    return sorted_values[max(0, min(len(sorted_values) - 1, math.ceil(p * len(sorted_values)) - 1))]
