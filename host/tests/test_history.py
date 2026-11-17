"""History of past images, saved Generate profiles, and the log line count."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app import history as history_mod
from app.comfy_client import ComfyImageOutput
from app.dashboard import _count_lines
from app.history import History, read_profiles
from app.jobs import Job
from app.providers.base import ProviderResult
from tests.test_api import TINY_PNG, _setup_test_app

AUTH = {"Authorization": "Bearer test-secret-key"}


def _job(prompt: str, created: float, key: str = "main", status: str = "done") -> Job:
    job = Job(f"{int(created):032x}", "m", prompt, "b64_json", True, key=key, params={"steps": 8})
    job.created, job.status = created, status
    job.result = ProviderResult([ComfyImageOutput("x.png", TINY_PNG, "image/png", ".png")], 42, "ref")
    return job


def test_history_lists_newest_first_with_search_paging_keys_and_a_cap(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(history_mod, "KEEP", 3)
    h = History(tmp_path / "history")
    for i, prompt in enumerate(["a red fox", "a lighthouse", "a blue fox", "a cat"]):
        h.record(_job(prompt, 1_700_000_000 + i, key="other" if prompt == "a cat" else "main"))
    h.record(_job("failed one", 1_800_000_000, status="failed"))  # only finished images are kept

    assert len(list((tmp_path / "history").glob("*.json"))) == 3  # the oldest went past the cap
    assert [i["prompt"] for i in h.list("", 10, None, None)] == ["a cat", "a blue fox", "a lighthouse"]
    assert [i["prompt"] for i in h.list("FOX", 10, None, None)] == ["a blue fox"]
    assert [i["prompt"] for i in h.list("", 10, None, "other")] == ["a cat"]
    first = h.list("", 1, None, "main")
    assert [i["prompt"] for i in h.list("", 10, first[0]["id"], "main")] == ["a lighthouse"]
    assert "key" not in first[0] and first[0]["seed"] == 42 and first[0]["steps"] == 8

    item = h.get(first[0]["id"], None)
    assert base64.b64decode(item["b64_json"]) == TINY_PNG
    for bad_id, key in ((first[0]["id"], "other"), ("../../etc/passwd", None)):
        try:
            h.get(bad_id, key)
            raise AssertionError("expected not_found")
        except history_mod.ApiError as exc:
            assert exc.code == "not_found"


def test_history_and_profiles_endpoints(tmp_path: Path) -> None:
    engine_file = tmp_path / "engine.json"
    engine_file.write_text(json.dumps({"preload": True, "generate_profiles": [{"name": ""}, {"name": "Old", "steps": 4}]}))
    app, fake_comfy, _ = _setup_test_app(tmp_path, stats_db=tmp_path / "data" / "stats.db", engine_file=engine_file)
    with TestClient(app, headers=AUTH) as client:
        app.state.providers["comfyui"].client = fake_comfy
        res = client.post("/v1/images/generations", json={"prompt": "a fox", "steps": 9, "negative_prompt": "blur"})
        assert res.status_code == 200

        page = client.get("/api/v1/history?q=fox").json()
        assert page["next"] is None
        [item] = page["items"]
        assert (item["prompt"], item["steps"], item["negative"], item["model"]) == ("a fox", 9, "blur", "z-image-turbo")
        assert client.get(f"/api/v1/history/{item['id']}").json()["b64_json"]
        assert client.get("/api/v1/history?before=nope").status_code == 422

        assert read_profiles(engine_file) == [{**history_mod.Profile(name="Old", steps=4).model_dump()}]
        mine = [{"name": "Phone", "shape": "phone", "steps": 6, "seed": 7}, {"name": "Wide", "negative": "text"}]
        saved = client.put("/api/v1/profiles", json={"profiles": mine}).json()["profiles"]
        assert [p["name"] for p in saved] == ["Phone", "Wide"] and saved[0]["seed"] == 7
        assert client.get("/api/v1/profiles").json()["profiles"] == saved
        assert json.loads(engine_file.read_text())["preload"] is True  # other settings kept
        dup = client.put("/api/v1/profiles", json={"profiles": [{"name": "A"}, {"name": "a"}]})
        assert dup.status_code == 422 and dup.json()["error"]["code"] == "bad_params"


def test_count_lines_counts_a_last_line_without_newline(tmp_path: Path) -> None:
    log = tmp_path / "host.log"
    log.write_bytes(b"one\ntwo\n")
    assert _count_lines(log) == 2
    log.write_bytes(b"one\ntwo\nthree")
    assert _count_lines(log) == 3
