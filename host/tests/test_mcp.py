"""POST /mcp, the stdio bridge, and applying the bridge to Claude Desktop's config."""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

from app import mcp, mcp_stdio
from tests.test_api import TINY_PNG, _setup_test_app

AUTH = {"Authorization": "Bearer test-secret-key"}
LOCAL = ("127.0.0.1", 50000)


def _rpc(client: TestClient, method: str, params: dict | None = None, msg_id: int | None = 1, headers=AUTH):
    msg = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})}
    if msg_id is not None:
        msg["id"] = msg_id
    return client.post("/mcp", json=msg, headers=headers)


def test_mcp_initialize_list_and_call(tmp_path: Path) -> None:
    app, fake, _ = _setup_test_app(tmp_path)
    with TestClient(app) as client:
        app.state.providers["comfyui"].client = fake
        init = _rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}})
        assert init.json()["result"]["protocolVersion"] == "2025-06-18"
        assert init.json()["result"]["capabilities"] == {"tools": {}}
        assert _rpc(client, "initialize", {"protocolVersion": "1999-01-01"}).json()["result"]["protocolVersion"] == mcp.PROTOCOL_VERSIONS[0]
        assert _rpc(client, "notifications/initialized", msg_id=None).status_code == 202

        tools = _rpc(client, "tools/list").json()["result"]["tools"]
        assert [t["name"] for t in tools] == ["generate_image", "list_models", "list_styles"]

        res = _rpc(client, "tools/call", {"name": "generate_image", "arguments": {"prompt": "a fox", "style": "cartoon", "seed": 7}})
        image, line = res.json()["result"]["content"]
        assert image == {"type": "image", "data": base64.b64encode(TINY_PNG).decode(), "mimeType": "image/png"}
        assert line["type"] == "text" and line["text"].startswith("Model z-image-turbo")
        assert app.state.jobs.jobs  # it went through the job queue

        styles = json.loads(_rpc(client, "tools/call", {"name": "list_styles"}).json()["result"]["content"][0]["text"])
        assert {"id": "cartoon", "name": "Cartoon"} in styles
        assert _rpc(client, "tools/call", {"name": "list_models"}).json()["result"]["isError"] is False

        bad = _rpc(client, "tools/call", {"name": "generate_image", "arguments": {"prompt": "x", "model": "nope"}}).json()["result"]
        assert bad["isError"] and bad["content"][0]["text"].startswith("bad_params:")
        assert _rpc(client, "tools/call", {"name": "nope"}).json()["error"]["code"] == -32602
        assert _rpc(client, "resources/list").json()["error"]["code"] == -32601


def test_mcp_needs_the_api_key(tmp_path: Path) -> None:
    app, _, _ = _setup_test_app(tmp_path)
    with TestClient(app) as client:
        assert _rpc(client, "tools/list", headers={}).status_code == 401
        res = _rpc(client, "tools/list", headers={"Authorization": "Bearer wrong"})
        assert res.status_code == 403 and res.json()["error"]["code"] == "forbidden"


def test_stdio_bridge_forwards_and_reports_failures() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.headers["Authorization"] != "Bearer k":
                payload, status = {"error": {"code": "forbidden", "message": "Invalid API key."}}, 403
            elif "id" not in body:
                payload, status = None, 202
            else:
                payload, status = {"jsonrpc": "2.0", "id": body["id"], "result": {}}, 200
            data = json.dumps(payload).encode() if payload else b""
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        ping = '{"jsonrpc":"2.0","id":3,"method":"ping"}'
        assert json.loads(mcp_stdio.forward(ping, url, "k")) == {"jsonrpc": "2.0", "id": 3, "result": {}}
        assert mcp_stdio.forward('{"jsonrpc":"2.0","method":"notifications/initialized"}', url, "k") is None
        refused = json.loads(mcp_stdio.forward(ping, url, "bad"))
        assert refused["id"] == 3 and "forbidden" in refused["error"]["message"]
    finally:
        server.shutdown()
    down = json.loads(mcp_stdio.forward(ping, url, "k", timeout=2))
    assert "isn't running" in down["error"]["message"]


def test_claude_desktop_apply_and_restore(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "Claude" / "claude_desktop_config.json"
    monkeypatch.setattr(mcp, "claude_config_path", lambda: config)
    app, _, _ = _setup_test_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app, client=LOCAL) as client:
        url = "/api/v1/integrations/claude-desktop"
        assert client.get(url, headers=AUTH).json()["installed"] is False
        assert client.post(url, json={"key": "test-secret-key"}, headers=AUTH).json()["error"]["code"] == "not_found"

        config.parent.mkdir()
        original = '{"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}'
        config.write_text(original)
        assert client.post(url, json={"key": "made-up"}, headers=AUTH).json()["error"]["code"] == "bad_params"
        view = client.post(url, json={"key": "test-secret-key"}, headers=AUTH).json()
        assert view["applied"] and view["backup"]
        data = json.loads(config.read_text())
        assert data["theme"] == "dark" and data["mcpServers"]["other"] == {"command": "x"}
        server = data["mcpServers"]["glint-studio"]
        assert server["args"][-1].endswith("mcp_stdio.py") and server["env"]["GLINT_API_KEY"] == "test-secret-key"

        client.post(url, json={"key": "test-secret-key"}, headers=AUTH)  # again: the backup stays the original
        assert client.delete(url, headers=AUTH).json() == {**view, "applied": False, "backup": False}
        assert config.read_text() == original
        assert client.delete(url, headers=AUTH).json()["error"]["code"] == "not_found"

        config.write_text("{not json")  # a broken file is never overwritten
        assert client.post(url, json={"key": "test-secret-key"}, headers=AUTH).json()["error"]["code"] == "bad_params"
        assert config.read_text() == "{not json"
        # Only from this computer, and only with the main key.
        lan = TestClient(app, client=("192.168.1.20", 50000))
        assert lan.post(url, json={"key": "test-secret-key"}, headers=AUTH).status_code == 400
        assert client.get(url).status_code == 401
