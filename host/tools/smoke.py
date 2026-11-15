"""End-to-end smoke check of a running host: python -m tools.smoke --api-key ... (from host/)."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from websockets.sync.client import connect as ws_connect

PROMPT = "a lighthouse at dusk"
TERMINAL = {"done", "failed", "cancelled"}


def image_kind(data: bytes) -> str | None:
    """'png', 'jpeg' or 'webp' from the magic bytes, else None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Smoke-test a running host end to end.")
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument("--api-key", default=os.environ.get("API_KEY"))
    p.add_argument("--model", action="append", help="repeatable; default: the host default model")
    p.add_argument("--timeout", type=float, default=300.0, help="seconds per check")
    p.add_argument("--skip-legacy", action="store_true", help="skip POST /api/v1/generate")
    return p.parse_args(argv)


def body(model: str | None) -> dict:
    return {"prompt": PROMPT, "style": "cartoon", **({"model": model} if model else {})}


def b64_image(data: list[dict]) -> str:
    kind = image_kind(base64.b64decode(data[0]["b64_json"]))
    assert kind, "data[0].b64_json is not a PNG/JPEG/WEBP"
    return kind


def wait_health(client: httpx.Client, timeout: float) -> str:
    deadline, said_degraded = time.monotonic() + timeout, False
    while True:
        try:
            res = client.get("/health")
            if res.status_code == 200:
                return "ok"
            if res.status_code == 503 and not said_degraded:
                print(f"  host is up but degraded (ComfyUI not reachable): {res.json().get('detail')}; waiting")
                said_degraded = True
        except httpx.TransportError:
            pass
        assert time.monotonic() < deadline, "no 200 from /health before the timeout"
        time.sleep(2)


def check_images(client: httpx.Client, model: str | None) -> str:
    res = client.post("/v1/images/generations", json=body(model))
    assert res.status_code == 200, f"HTTP {res.status_code}: {res.text[:200]}"
    j = res.json()
    return f"{b64_image(j['data'])}, model {j.get('model')}, seed {j.get('seed')}"


def events_url(base_url: str, api_key: str) -> str:
    u = urlsplit(base_url)
    scheme = "wss" if u.scheme == "https" else "ws"
    return urlunsplit((scheme, u.netloc, "/api/v1/events", f"api_key={quote(api_key, safe='')}", ""))


def check_jobs(client: httpx.Client, ws_url: str, model: str | None, timeout: float) -> str:
    events: list[str] = []
    with ws_connect(ws_url, max_size=None) as ws:  # subscribe before queueing so no event is missed
        res = client.post("/api/v1/jobs", json=body(model))
        assert res.status_code == 202, f"HTTP {res.status_code}: {res.text[:200]}"
        job_id = res.json()["id"]
        deadline = time.monotonic() + timeout
        while not events or events[-1] not in TERMINAL:
            msg = json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic())))
            if msg.get("id") == job_id:
                events.append(msg["event"])
    job = client.get(f"/api/v1/jobs/{job_id}").json()
    assert job["status"] == "done", f"job ended {job['status']}: {job.get('error')}"
    progress = events.count("progress")
    return f"{b64_image(job['data'])}, events {'>'.join(dict.fromkeys(events))}, " + (
        f"{progress} progress events" if progress else "NO progress events"
    )


def check_cancel(client: httpx.Client, timeout: float) -> str:
    job_id = client.post("/api/v1/jobs", json=body(None)).json()["id"]
    deadline = time.monotonic() + timeout
    while (status := client.get(f"/api/v1/jobs/{job_id}").json()["status"]) != "running":
        assert status == "queued", f"job went {status} before it was seen running"
        assert time.monotonic() < deadline, "job never started running"
        time.sleep(0.2)
    status = client.delete(f"/api/v1/jobs/{job_id}").json()["status"]
    assert status == "cancelled", f"after DELETE the job is {status}"
    return "running -> cancelled"


def check_legacy(client: httpx.Client) -> str:
    res = client.post("/api/v1/generate", json={"prompt": PROMPT, "workflow": "default", "response_format": "image"})
    assert res.status_code == 200, f"HTTP {res.status_code}: {res.text[:200]}"
    kind = image_kind(res.content)
    assert kind, "body is not a PNG/JPEG/WEBP"
    missing = [h for h in ("X-Image-Title", "X-Seed", "X-Job-Id") if h not in res.headers]
    assert not missing, f"missing headers {missing}"
    return f"{kind}, title {res.headers['X-Image-Title']!r}, seed {res.headers['X-Seed']}"


def check_auth(client: httpx.Client) -> str:
    req = client.build_request("POST", "/api/v1/jobs", json=body(None))
    del req.headers["Authorization"]
    res = client.send(req)
    assert res.status_code == 401, f"HTTP {res.status_code}, want 401"
    assert res.json()["error"]["code"] == "unauthorized", res.text[:200]
    return "401 unauthorized"


def run(secret: str, name: str, fn, *args) -> bool:
    start = time.monotonic()
    try:
        note, ok = fn(*args), True
    except Exception as exc:  # any failure is a FAIL line, never a traceback
        note, ok = f"{type(exc).__name__}: {exc}".replace(secret, "***"), False  # the ws URL carries the key
    print(f"{'PASS' if ok else 'FAIL'}  {name:<28} {time.monotonic() - start:6.1f}s  {note}", flush=True)
    return ok


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.api_key:
        print("No API key: pass --api-key or set API_KEY.", file=sys.stderr)
        return 2
    key = args.api_key
    headers = {"Authorization": f"Bearer {key}"}
    ws_url = events_url(args.base_url, key)
    results = []
    with httpx.Client(base_url=args.base_url, headers=headers, timeout=args.timeout) as client:
        results.append(run(key, "health", wait_health, client, args.timeout))
        if not results[0]:
            return 1
        for model in args.model or [None]:
            label = model or "default model"
            results.append(run(key, f"images ({label})", check_images, client, model))
            results.append(run(key, f"jobs+events ({label})", check_jobs, client, ws_url, model, args.timeout))
        results.append(run(key, "cancel", check_cancel, client, args.timeout))
        if not args.skip_legacy:
            results.append(run(key, "legacy generate", check_legacy, client))
        results.append(run(key, "auth", check_auth, client))
    print(f"{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
