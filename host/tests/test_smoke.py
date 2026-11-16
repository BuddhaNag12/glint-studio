"""tools.smoke's HTTP checks against the app in-process (the WebSocket check needs a real server)."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from tests.test_api import TINY_PNG, _setup_test_app
from tools import smoke


def test_image_kind_and_args() -> None:
    assert smoke.image_kind(TINY_PNG) == "png"
    assert smoke.image_kind(b"\xff\xd8\xff\xe0") == "jpeg"
    assert smoke.image_kind(b"RIFF\0\0\0\0WEBPVP8 ") == "webp"
    assert smoke.image_kind(b"<html>") is None
    args = smoke.parse_args(["--api-key", "k", "--model", "a", "--model", "b", "--skip-legacy"])
    assert args.model == ["a", "b"] and args.skip_legacy and args.timeout == 300
    assert smoke.events_url("https://h:9/") == "wss://h:9/api/v1/events"


def test_smoke_checks_pass_against_the_app(tmp_path) -> None:
    app, fake, _ = _setup_test_app(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer test-secret-key"}) as client:
        app.state.providers["comfyui"].client = fake
        assert smoke.wait_health(client, 5) == "ok"
        assert smoke.check_images(client, None).startswith("png")
        assert smoke.check_legacy(client).startswith("png")
        assert smoke.check_auth(client) == "401 unauthorized"
        fake.gate = asyncio.Event()  # never released: the job runs until cancelled
        assert smoke.check_cancel(client, 5) == "running -> cancelled"
