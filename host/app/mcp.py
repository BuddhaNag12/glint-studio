"""MCP over streamable HTTP (POST /mcp), so Claude, Cursor and other MCP clients can make images.

Hand-rolled JSON-RPC rather than the `mcp` package: four methods, no new dependency. Requests get a
JSON answer, notifications a bare 202; there's no server-to-client stream, so GET /mcp is 405.
Also here: putting the bridge in Claude Desktop's config, and taking it back out.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import platform
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from app.auth import require_api_key, require_main_key
from app.dashboard import local_only
from app.errors import ApiError
from app.local_models import model_installed
from app.model_folders import model_roots
from app.schemas import ImagesRequest

# Newest first; a client asking for one of these gets it back, anything else gets the newest.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "glint-studio"

TOOLS = [
    {
        "name": "generate_image",
        "description": "Make an image from a text prompt on this computer's Glint Studio.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "What the image shows."},
                "model": {"type": "string", "description": "A model id from list_models. Default: the app's default model."},
                "style": {"type": "string", "description": "A style id from list_styles."},
                "size": {"type": "string", "description": "WIDTHxHEIGHT, e.g. 1024x1024."},
                "seed": {"type": "integer", "description": "Same seed and prompt, same image."},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "list_models",
        "description": "The image models ready to use: installed local models, and cloud models this key may use.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_styles",
        "description": "Style presets to pass as generate_image's style.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

router = APIRouter()


def _text(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}


async def _generate(request: Request, args: dict) -> dict:
    fields = {k: args[k] for k in ("prompt", "model", "style", "size", "seed") if args.get(k) is not None}
    try:
        body = ImagesRequest(**fields, response_format="b64_json")
    except ValidationError as exc:
        first = exc.errors()[0]
        raise ApiError("bad_params", f"{'.'.join(map(str, first['loc']))}: {first['msg']}") from exc
    job = request.app.state.submit_images(request, body)
    try:
        result = await request.app.state.jobs.wait(job)
    except ApiError:
        raise
    except Exception:  # noqa: BLE001 - the job keeps the failure as an error code
        raise job.api_error or ApiError("workflow_failed", "The job was cancelled.") from None
    seconds = (job.elapsed_ms or 0) / 1000
    line = f"Model {job.model}" + (f", seed {result.seed}" if result.seed is not None else "") + f", {seconds:.1f}s"
    images = [
        {"type": "image", "data": base64.b64encode(img.content).decode("ascii"), "mimeType": img.mime_type}
        for img in result.images
    ]
    return {"content": [*images, {"type": "text", "text": line}], "isError": False}


async def _list_models(request: Request) -> dict:
    cfg = request.app.state.settings
    cloud = "cloud" in request.state.key.scopes
    roots = await asyncio.to_thread(model_roots, cfg.models_dir, cfg.engine_file)

    def ready(m) -> bool:
        return cloud if m.cloud else (not m.variants or model_installed(roots, m))

    models = await asyncio.to_thread(lambda: [m for m in request.app.state.models.values() if ready(m)])
    rows = [{"id": m.id, "name": m.name, "cloud": m.cloud} for m in models]
    return _text(json.dumps(rows) if rows else "No models are installed yet. Download one in Glint Studio.")


def _list_styles(request: Request) -> dict:
    return _text(json.dumps([{"id": s.id, "name": s.name} for s in request.app.state.styles.values()]))


async def _call_tool(request: Request, params: dict) -> dict:
    name, args = params.get("name"), params.get("arguments") or {}
    try:
        if name == "generate_image":
            return await _generate(request, args)
        if name == "list_models":
            return await _list_models(request)
        if name == "list_styles":
            return _list_styles(request)
    except ApiError as exc:
        return _text(f"{exc.code}: {exc.message}", error=True)
    raise _RpcError(-32602, f"Unknown tool '{name}'.")


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


async def _handle(request: Request, method: str, params: dict) -> dict:
    if method == "initialize":
        asked = params.get("protocolVersion")
        return {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": request.app.version},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        return await _call_tool(request, params)
    raise _RpcError(-32601, f"Method '{method}' isn't supported.")


def _rpc_error(msg_id, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}, status_code=status)


@router.post("/mcp", dependencies=[Depends(require_api_key)])
async def mcp(request: Request) -> Response:
    try:
        msg = await request.json()
    except ValueError:
        return _rpc_error(None, -32700, "Parse error.", 400)
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method", ""), str):
        return _rpc_error(None, -32600, "Send one JSON-RPC 2.0 message.", 400)
    if "method" not in msg or "id" not in msg:
        return Response(status_code=202)  # a notification or a response: nothing to answer
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    try:
        result = await _handle(request, msg["method"], params)
    except _RpcError as exc:
        return _rpc_error(msg["id"], exc.code, exc.message)
    return JSONResponse({"jsonrpc": "2.0", "id": msg["id"], "result": result})


@router.get("/mcp", include_in_schema=False)
async def mcp_stream() -> Response:
    return Response(status_code=405, headers={"Allow": "POST"})


# --- Claude Desktop: it runs stdio servers only, so its config starts our bridge (mcp_stdio.py) ---

BRIDGE = Path(__file__).with_name("mcp_stdio.py")


def claude_config_path() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "Claude/claude_desktop_config.json"
    if platform.system() == "Darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    return Path.home() / ".config/Claude/claude_desktop_config.json"


def _backup(path: Path) -> Path:
    return path.with_name(path.name + ".glint-backup")


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, "utf-8")
    os.replace(tmp, path)  # Claude Desktop never reads half a file


def _read_config(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
    except ValueError:
        raise ApiError("bad_params", f"{path} isn't valid JSON, so it was left alone. Fix it, then try again.") from None
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise ApiError("bad_params", f"{path} isn't a Claude Desktop config, so it was left alone.")
    return data


def _desktop_view(path: Path) -> dict:
    try:
        applied = SERVER_NAME in _read_config(path).get("mcpServers", {})
    except ApiError:
        applied = False
    return {"path": str(path), "installed": path.parent.is_dir(), "applied": applied, "backup": _backup(path).is_file()}


class ApplyRequest(BaseModel):
    key: str


_desktop_guard = [Depends(require_main_key), Depends(local_only)]


@router.get("/api/v1/integrations/claude-desktop", dependencies=_desktop_guard)
async def claude_desktop(request: Request) -> dict:
    return _desktop_view(claude_config_path())


@router.post("/api/v1/integrations/claude-desktop", dependencies=_desktop_guard)
async def apply_claude_desktop(body: ApplyRequest, request: Request) -> dict:
    """Adds the glint-studio server; every other key stays. The first apply keeps a backup to restore."""
    if request.app.state.keys.find(body.key) is None:
        raise ApiError("bad_params", "That key isn't one of this app's keys.")
    path = claude_config_path()
    if not path.parent.is_dir():
        raise ApiError("not_found", "Claude Desktop isn't installed on this computer.")
    data = _read_config(path)
    backup = _backup(path)
    if not backup.is_file():
        _write(backup, path.read_text("utf-8") if path.is_file() else "{}\n")
    data.setdefault("mcpServers", {})[SERVER_NAME] = {
        # -I: the script's folder (app/) must not shadow the standard library's modules.
        "command": sys.executable,
        "args": ["-I", str(BRIDGE)],
        "env": {"GLINT_URL": request.app.state.settings.public_base_url, "GLINT_API_KEY": body.key},
    }
    _write(path, json.dumps(data, indent=2) + "\n")
    return _desktop_view(path)


@router.delete("/api/v1/integrations/claude-desktop", dependencies=_desktop_guard)
async def restore_claude_desktop(request: Request) -> dict:
    path = claude_config_path()
    backup = _backup(path)
    if not backup.is_file():
        raise ApiError("not_found", "There's no backup to restore.")
    os.replace(backup, path)
    return _desktop_view(path)
