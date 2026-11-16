"""Slice 4: the job queue, the jobs API, events, and ComfyUI progress and cancel."""

from __future__ import annotations

import asyncio
import base64
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.comfy_client import ComfyClient
from app.errors import ApiError
from app.jobs import JobQueue
from app.providers.base import ProviderResult
from tests.test_api import SAMPLE_WORKFLOW, TINY_PNG, _FakeWebSocket, _setup_test_app

AUTH = {"Authorization": "Bearer test-secret-key"}
RESULT = ProviderResult(images=[], seed=7, job_ref="ref")


def _queue(**kw) -> JobQueue:
    return JobQueue(**{"max_concurrent": 1, "queue_timeout_seconds": 5, "run_timeout_seconds": 5,
                       "keep_seconds": 60, **kw})


def _runner(gate: asyncio.Event | None = None, log: list | None = None, name: str = ""):
    async def run(on_progress):
        if log is not None:
            log.append(name)
        on_progress(0.5)
        if gate is not None:
            await gate.wait()
        return RESULT
    return run


def _submit(q: JobQueue, run, local: bool = True):
    return q.submit(run, model="m", prompt="p", response_format="b64_json", local=local)


async def test_local_jobs_run_in_order_one_at_a_time_and_cloud_jobs_skip_the_queue() -> None:
    q, gate, log = _queue(), asyncio.Event(), []
    first = _submit(q, _runner(gate, log, "a"))
    second = _submit(q, _runner(None, log, "b"))
    cloud = _submit(q, _runner(None, log, "cloud"), local=False)
    await asyncio.sleep(0.01)
    assert first.status == "running" and second.status == "queued"
    assert (q.queue_position(first), q.queue_position(second)) == (0, 1)
    assert cloud.status == "done"  # didn't wait behind the busy GPU
    gate.set()
    assert await q.wait(second) is RESULT
    assert log == ["a", "cloud", "b"]
    assert first.progress == 1.0 and first.elapsed_ms is not None


async def test_queue_wait_and_run_time_have_separate_timeouts() -> None:
    q = _queue(queue_timeout_seconds=0.05, run_timeout_seconds=0.05)
    stuck = _submit(q, _runner(asyncio.Event()))
    waiting = _submit(q, _runner())
    with pytest.raises(ApiError, match="in the queue") as err:
        await q.wait(waiting)
    assert err.value.code == "timeout" and waiting.elapsed_ms is None  # never ran
    with pytest.raises(ApiError, match="took over") as err:
        await q.wait(stuck)
    assert err.value.code == "timeout" and stuck.status == "failed"
    assert q.view(stuck)["error"]["code"] == "timeout"


async def test_cancel_drops_a_queued_job_and_stops_a_running_one() -> None:
    q, log = _queue(), []
    running = _submit(q, _runner(asyncio.Event(), log, "a"))
    queued = _submit(q, _runner(None, log, "b"))
    await asyncio.sleep(0.01)
    q.cancel(queued)
    q.cancel(running)
    await running.done.wait()
    await queued.done.wait()
    assert (running.status, queued.status) == ("cancelled", "cancelled")
    assert log == ["a"]  # the queued one never started
    with pytest.raises(ApiError):
        await q.wait(running)
    after = _submit(q, _runner())  # the slot was given back
    assert await q.wait(after) is RESULT


async def test_events_and_unexpected_errors() -> None:
    q = _queue()
    listener = q.subscribe()

    async def boom(on_progress):
        raise RuntimeError("disk full")

    job = _submit(q, boom)
    await job.done.wait()
    events = [listener.get_nowait()["event"] for _ in range(listener.qsize())]
    assert events == ["queued", "started", "failed"]
    assert q.view(job)["error"] == {"code": "workflow_failed", "message": "disk full"}
    with pytest.raises(RuntimeError):  # sync callers get the original exception
        await q.wait(job)


def test_jobs_api_queues_reports_and_formats_the_result(tmp_path) -> None:
    app, fake, _ = _setup_test_app(tmp_path)
    with TestClient(app) as client:
        app.state.providers["comfyui"].client = fake
        with client.websocket_connect("/api/v1/events?api_key=test-secret-key") as ws:
            res = client.post("/api/v1/jobs", headers=AUTH, json={"prompt": "a fox", "style": "cartoon"})
            assert res.status_code == 202
            job_id = res.json()["id"]
            events = []
            while not events or events[-1]["event"] != "done":
                events.append(ws.receive_json())
        assert [e["event"] for e in events] == ["queued", "started", "progress", "done"]
        assert events[2]["progress"] == 0.5 and {e["id"] for e in events} == {job_id}

        # The dashboard offers the key as a subprotocol, and the host must echo glint.v1 back.
        with client.websocket_connect("/api/v1/events", subprotocols=["glint.v1", "glint.key.test-secret-key"]) as ws:
            assert ws.accepted_subprotocol == "glint.v1"
        bad = ["glint.v1", "glint.key.wrong"]
        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/api/v1/events", subprotocols=bad) as ws:
            ws.receive_json()

        body = client.get(f"/api/v1/jobs/{job_id}", headers=AUTH).json()
        assert body["status"] == "done" and body["queue_position"] == 0 and body["error"] is None
        assert body["data"] == [{"b64_json": base64.b64encode(TINY_PNG).decode("ascii")}]

        missing = client.get("/api/v1/jobs/nope", headers=AUTH)
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
        bad = client.post("/api/v1/jobs", headers=AUTH, json={"prompt": ""})
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "bad_params"
        assert client.get(f"/api/v1/jobs/{job_id}").status_code == 401


def test_delete_interrupts_a_running_comfyui_job(tmp_path) -> None:
    app, fake, _ = _setup_test_app(tmp_path)
    with TestClient(app) as client:
        app.state.providers["comfyui"].client = fake
        fake.gate = asyncio.Event()  # never released: the job runs until cancelled
        job_id = client.post("/api/v1/jobs", headers=AUTH, json={"prompt": "a fox"}).json()["id"]
        deadline = time.time() + 2
        while client.get(f"/api/v1/jobs/{job_id}", headers=AUTH).json()["status"] != "running":
            assert time.time() < deadline
        res = client.delete(f"/api/v1/jobs/{job_id}", headers=AUTH)
        assert res.json()["status"] == "cancelled"
        assert fake.interrupted == 1


def test_events_reject_a_missing_key(tmp_path) -> None:
    from starlette.websockets import WebSocketDisconnect

    app, _, _ = _setup_test_app(tmp_path)
    with TestClient(app) as client, pytest.raises(WebSocketDisconnect) as err:
        with client.websocket_connect("/api/v1/events?api_key=wrong"):
            pass
    assert err.value.code == 1008


def test_sync_endpoints_share_the_queue_ids(tmp_path) -> None:
    app, fake, _ = _setup_test_app(tmp_path)
    with TestClient(app) as client:
        app.state.providers["comfyui"].client = fake
        res = client.post("/api/v1/generate", headers=AUTH, json={"prompt": "a fox", "response_format": "url"})
        job_id = res.json()["id"]
        assert client.get(f"/api/v1/jobs/{job_id}", headers=AUTH).json()["status"] == "done"


async def test_comfy_client_reports_progress_and_stops_the_prompt_when_cancelled(monkeypatch) -> None:
    posts: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append((request.url.path, json.loads(request.content or b"{}")))
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "p1"})
        return httpx.Response(200, json={})

    progress_msgs = [
        json.dumps({"type": "progress", "data": {"prompt_id": "p1", "value": 3, "max": 6}}),
        json.dumps({"type": "progress", "data": {"prompt_id": "other", "value": 1, "max": 2}}),
    ]

    class Hanging(_FakeWebSocket):
        async def recv(self):
            try:
                return next(self._messages)
            except StopIteration:
                await asyncio.Event().wait()  # ComfyUI still sampling

    monkeypatch.setattr("app.comfy_client.websockets.connect", lambda url, max_size=None: Hanging(progress_msgs))
    seen: list[float] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ComfyClient("http://c", "ws://c", http)
        task = asyncio.create_task(client.generate_images(SAMPLE_WORKFLOW, on_progress=seen.append))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert seen == [0.5]
    assert ("/queue", {"delete": ["p1"]}) in posts and ("/interrupt", {"prompt_id": "p1"}) in posts

