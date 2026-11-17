"""Usage history: ranges, the model filter, the heatmap, clearing, the record switch and retention."""

from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.jobs import Job
from app.stats import DAY, KEEP_DAYS, Stats, day_bounds
from tests.test_api import _setup_test_app

AUTH = {"Authorization": "Bearer test-secret-key"}
NOW = dt.datetime(2026, 11, 17, 12, 0, tzinfo=dt.timezone.utc).timestamp()  # a Tuesday
TODAY = int(NOW // DAY)


def job(finished: float, model: str = "zit", status: str = "done", key: str | None = None) -> Job:
    return Job(id="j", model=model, prompt="p", response_format="url", local=True, status=status,
               created=finished - 2, started=finished - 1, finished=finished, key=key)


def test_ranges_are_local_days() -> None:
    assert day_bounds("today", now=NOW) == (TODAY, TODAY)
    assert day_bounds("yesterday", now=NOW) == (TODAY - 1, TODAY - 1)
    assert day_bounds("7d", now=NOW) == (TODAY - 6, TODAY)
    assert day_bounds(None, days=14, now=NOW) == (TODAY - 13, TODAY)  # days= still works
    assert day_bounds("month", now=NOW) == (TODAY - 16, TODAY)
    assert day_bounds(start="2026-11-01", end="2026-11-03") == (TODAY - 16, TODAY - 14)
    # 23:00 UTC is already tomorrow in UTC+2.
    assert day_bounds("today", tz=120, now=NOW + 11 * 3600) == (TODAY + 1, TODAY + 1)
    for bad in [{"range_": "decade"}, {"start": "2026-11-03", "end": "2026-11-01"}, {"start": "nope", "end": "x"},
                {"start": "2020-01-01", "end": "2026-01-01"}]:
        with pytest.raises(ValueError):
            day_bounds(**bad)


def test_summary_filters_compares_and_buckets_by_local_hour() -> None:
    stats = Stats(None)
    for t, model, status in [
        (NOW, "zit", "done"), (NOW, "zit", "failed"), (NOW - 3600, "flux", "done"),  # today
        (NOW - DAY, "zit", "done"),  # yesterday
    ]:
        stats.record(job(t, model, status))

    s = stats.summary(TODAY, TODAY)
    assert s["totals"]["done"] == 2 and s["totals"]["failed"] == 1
    assert s["previous"] == {"done": 1, "failed": 0}
    assert s["all_models"] == ["flux", "zit"]

    s = stats.summary(TODAY, TODAY, model="zit")
    assert s["totals"]["jobs"] == 2 and [m["model"] for m in s["models"]] == ["zit"]
    assert s["previous"] == {"done": 1, "failed": 0}

    # Tuesday is row 1; 12:00 UTC is 13:30 at +90 minutes, 11:00 UTC is 12:30.
    heat = stats.summary(TODAY, TODAY, tz=90)["heatmap"]
    assert heat[1][13] == 1 and heat[1][12] == 1 and sum(map(sum, heat)) == 2  # done only
    assert stats.summary(TODAY - 1, TODAY - 1)["heatmap"][0][12] == 1  # Monday


def test_session_counts_only_since_the_host_started() -> None:
    stats = Stats(None)
    stats.record(job(stats.started - 10))
    stats.record(job(time.time()))
    today = int(time.time() // DAY)
    assert stats.summary(today - 1, today)["totals"]["done"] == 2
    s = stats.summary(today - 1, today, session=True)
    assert s["totals"]["done"] == 1 and s["previous"] is None


def test_record_switch_clear_and_retention(tmp_path: Path) -> None:
    engine = tmp_path / "engine.json"
    db = tmp_path / "stats.db"
    old = time.time() - (KEEP_DAYS + 1) * DAY
    stats = Stats(db, engine)
    stats.record(job(old, key="phone"))
    stats.record(job(time.time(), key="phone"))
    assert stats.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1  # pruned on record

    engine.write_text(json.dumps({"record_usage": False}))
    stats.record(job(time.time(), key="phone"))
    assert stats.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1  # kept, nothing added
    assert stats.key_usage()["phone"][0] == 2  # but the key's daily cap still counts

    stats.clear()
    assert stats.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert stats.key_usage()["phone"][0] == 2

    stats.db.execute("INSERT INTO jobs (finished, model, status) VALUES (?, 'zit', 'done')", (old,))
    stats.db.commit()
    stats.db.close()
    engine.write_text(json.dumps({"record_usage": "yes"}))  # not a bool: the default
    stats = Stats(db, engine)
    assert stats.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0  # pruned on start
    assert stats.recording()


def test_usage_endpoints(tmp_path: Path) -> None:
    app, _, _ = _setup_test_app(tmp_path, engine_file=tmp_path / "engine.json")
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        app.state.stats.record(job(time.time()))
        s = client.get("/api/v1/stats?range=today&tz=60&model=zit&scope=session", headers=AUTH).json()
        assert len(s["days"]) == 1 and s["previous"] is None
        assert len(client.get("/api/v1/stats?from=2026-11-01&to=2026-11-03", headers=AUTH).json()["days"]) == 3
        assert client.get("/api/v1/stats?range=decade", headers=AUTH).json()["error"]["code"] == "bad_params"
        assert client.get("/api/v1/stats?days=1", headers=AUTH).json()["totals"]["done"] == 1

        assert client.get("/api/v1/usage/settings", headers=AUTH).json()["record_usage"] is True
        res = client.put("/api/v1/usage/settings", json={"record_usage": False}, headers=AUTH).json()
        assert res == {"record_usage": False, "keep_days": KEEP_DAYS}
        assert client.delete("/api/v1/stats", headers=AUTH).status_code == 204
        assert client.get("/api/v1/stats?days=1", headers=AUTH).json()["totals"]["jobs"] == 0

        lan = TestClient(app, client=("192.168.1.20", 50000))
        assert lan.delete("/api/v1/stats", headers=AUTH).status_code == 400


def test_year_is_53_monday_first_weeks_ending_today() -> None:
    stats = Stats(None)
    now = time.time()
    stats.record(job(now))
    stats.record(job(now - 7 * DAY))
    stats.record(job(now, status="failed"))
    stats.record(job(now, model="other"))
    year = stats.summary(TODAY, TODAY, model="zit")["year"]  # the picked range doesn't narrow it
    today = int(now // DAY)
    assert (year["from"] // DAY + 3) % 7 == 0  # a Monday
    assert len(year["done"]) == today - year["from"] // DAY + 1 and 365 <= len(year["done"]) <= 371
    assert year["done"][-1] == 1 and year["done"][-8] == 1 and sum(year["done"]) == 2
