"""FastAPI application exposing image generation over an authenticated HTTP API."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    status,
)
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from app.auth import (
    WS_PROTOCOL,
    build_signed_image_query,
    require_api_key,
    require_main_key,
    verify_api_key,
    verify_image_signature,
    ws_api_key,
    ws_protocol_key,
)
from app.bench import router as bench_router
from app.catalog import ID_PATTERN, CatalogModel, load_catalog, resolve_params, with_user_settings
from app.comfy_client import ComfyClient, ComfyImageOutput, infer_mime_and_ext
from app.config import Settings
from app.dashboard import Sessions
from app.dashboard import router as dashboard_router
from app.model_settings import router as model_settings_router
from app.mcp import router as mcp_router
from app.net_settings import router as net_settings_router
from app.engine import Engine
from app.history import History
from app.history import router as history_router
from app.engine_server import idle_minutes
from app.engine_server import router as engine_server_router
from app.errors import ApiError, api_error_handler
from app.jobs import Job, JobQueue
from app.keys import Keys
from app.local_models import Downloads
from app.model_folders import get_setting, model_roots, read_settings
from app.naming import extract_image_title, safe_header_value, title_to_filename_slug
from app.providers.base import ProviderRequest
from app.providers.comfyui import ComfyUIProvider
from app.providers.openai_compat import load_remote_providers
from app.schemas import (
    GeneratedImage,
    GenerateRequest,
    GenerateResponse,
    HealthResponse,
    ImageData,
    ImagesRequest,
    ImagesResponse,
)
from app.stats import Stats
from app.styles import Style, compose, load_styles
from app.workflow import (
    WorkflowNotFoundError,
    WorkflowValidationError,
    load_workflow_bundle,
)

logger = logging.getLogger(__name__)
IMAGE_FILENAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*\.(png|jpg|jpeg|webp)$")

# What `"workflow": "default"` on /api/v1/generate runs.
DEFAULT_MODEL = "z-image-turbo"
DEFAULT_STYLE = "lockscreen-wallpaper"
# /api/v1/generate keeps the original bridge's status codes and {detail} body so existing clients
# don't break; newer endpoints answer with {error:{code,message}} and app.errors' statuses.
LEGACY_STATUS = {"workflow_failed": 502, "model_missing": 502}
LEGACY_PATH = "/api/v1/generate"


def _prune_expired_output_files(output_dir: Path, ttl_seconds: int) -> None:
    """Delete locally cached URL-backed images older than the URL TTL."""
    if not output_dir.is_dir():
        return
    cutoff = time.time() - ttl_seconds
    for entry in output_dir.iterdir():
        if not entry.is_file() or not IMAGE_FILENAME_PATTERN.match(entry.name):
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)
        except OSError:
            continue


def _build_generated_image(
    settings: Settings,
    base: str,
    job_id: str,
    index: int,
    title_slug: str,
    image: ComfyImageOutput,
    response_format: str,
) -> GeneratedImage:
    """Persist image (when URL requested) and build URL/base64 representations."""
    include_url = response_format in ("url", "both")
    include_base64 = response_format in ("base64", "both")

    stored_filename = f"{title_slug}-{job_id[:8]}-{index}{image.extension}"
    image_url: str | None = None
    if include_url:
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        _prune_expired_output_files(settings.output_dir, settings.image_url_ttl_seconds)
        (settings.output_dir / stored_filename).write_bytes(image.content)
        base_url = f"{base}/api/v1/images/{stored_filename}"
        if settings.sign_image_urls:
            exp, sig = build_signed_image_query(
                stored_filename,
                settings.image_url_ttl_seconds,
                settings.effective_signing_secret,
            )
            image_url = f"{base_url}?exp={exp}&sig={sig}"
        else:
            image_url = base_url

    encoded_b64: str | None = None
    data_url: str | None = None
    if include_base64:
        encoded_b64 = base64.b64encode(image.content).decode("ascii")
        data_url = f"data:{image.mime_type};base64,{encoded_b64}"

    return GeneratedImage(
        filename=stored_filename,
        mime_type=image.mime_type,
        url=image_url,
        base64=encoded_b64,
        data_url=data_url,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application instance."""
    resolved_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        resolved_settings.output_dir.mkdir(parents=True, exist_ok=True)
        _prune_expired_output_files(
            resolved_settings.output_dir,
            resolved_settings.image_url_ttl_seconds,
        )
        resolved_settings.workflows_dir.mkdir(parents=True, exist_ok=True)
        # A broken built-in catalog file is a bug: fail at startup, not on the first request.
        models = load_catalog(resolved_settings.catalog_dir)
        styles = load_styles(resolved_settings.catalog_dir / "styles", resolved_settings.user_styles_dir)

        if not resolved_settings.is_api_key_configured:
            logger.warning(
                "API_KEY is missing or still set to the placeholder value; "
                "protected endpoints will reject requests until configured."
            )

        timeout = httpx.Timeout(resolved_settings.request_timeout_seconds, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as http_client:
            app.state.settings = resolved_settings
            remote_providers, remote_models = load_remote_providers(
                resolved_settings.providers_file, set(models), http_client
            )
            app.state.models = {**models, **remote_models}
            app.state.styles = styles
            comfy_client = ComfyClient(
                http_url=resolved_settings.comfyui_http_url,
                ws_url=resolved_settings.comfyui_ws_url,
                http_client=http_client,
                clear_history=resolved_settings.clear_comfy_history,
                cleanup_files=resolved_settings.cleanup_comfy_files,
                comfyui_dir=resolved_settings.comfyui_dir,
                comfyui_temp_dir=resolved_settings.comfyui_temp_dir,
                comfyui_output_dir=resolved_settings.comfyui_output_dir,
            )
            app.state.providers = {
                "comfyui": ComfyUIProvider(
                    comfy_client,
                    resolved_settings.catalog_dir / "workflows",
                    resolved_settings.request_timeout_seconds,
                ),
                **remote_providers,
            }
            app.state.stats = Stats(resolved_settings.stats_db, resolved_settings.engine_file)
            stats_db = resolved_settings.stats_db
            app.state.history = History(stats_db.parent / "history" if stats_db else None)
            engine_file = resolved_settings.engine_file
            app.state.engine = Engine(
                app.state.providers["comfyui"],
                app.state.models,
                roots=lambda: model_roots(resolved_settings.models_dir, engine_file),
                preload=lambda: read_settings(engine_file)["preload"],
                default_model=DEFAULT_MODEL,
                idle_minutes=lambda model_id: idle_minutes(engine_file, model_id),
                busy=lambda: any(j.status in ("queued", "running") for j in app.state.jobs.jobs.values()),
            )

            def on_finish(job: Job) -> None:
                app.state.stats.record(job)
                if get_setting(engine_file, "record_usage", True) is not False:  # the same privacy switch as usage
                    app.state.history.record(job)
                app.state.engine.finished(job)

            app.state.jobs = JobQueue(
                max_concurrent=resolved_settings.max_concurrent_jobs,
                queue_timeout_seconds=resolved_settings.queue_timeout_seconds,
                run_timeout_seconds=resolved_settings.request_timeout_seconds,
                keep_seconds=resolved_settings.image_url_ttl_seconds,  # finished jobs live as long as their URLs
                on_finish=on_finish,
            )
            app.state.keys = Keys(resolved_settings.keys_file, resolved_settings.api_key)
            app.state.sessions = Sessions(resolved_settings.api_key.strip())
            # Downloads get their own client: no overall timeout on a 20 GB file.
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as download_client:
                app.state.downloads = Downloads(
                    download_client, resolved_settings.models_dir or Path("models"), engine_file=resolved_settings.engine_file
                )
                watcher = asyncio.create_task(app.state.engine.watch())
                try:
                    yield
                finally:
                    watcher.cancel()
                    await app.state.engine.close()
                    await app.state.downloads.cancel()
                    if getattr(app.state, "bench", None):
                        app.state.bench.cancel()
                    await app.state.jobs.shutdown()
                    app.state.stats.db.close()

    app = FastAPI(
        title="Glint Studio Host",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_exception_handler(ApiError, api_error_handler)

    async def validation_error_handler(request: Request, exc: Exception) -> Response:
        # Everything answers with {error:{code,message}} except /api/v1/generate, which keeps
        # FastAPI's {detail:[...]} so existing clients don't break.
        assert isinstance(exc, RequestValidationError)
        if request.url.path == LEGACY_PATH:
            return await request_validation_exception_handler(request, exc)
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        message = f"{where}: {first.get('msg', 'invalid request')}" if where else first.get("msg", "")
        return await api_error_handler(request, ApiError("bad_params", message or "Invalid request."))

    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.include_router(dashboard_router)
    app.include_router(model_settings_router)
    app.include_router(engine_server_router)
    app.include_router(bench_router)
    app.include_router(mcp_router)
    app.include_router(history_router)
    app.include_router(net_settings_router)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key"],
        expose_headers=[
            "Content-Disposition",
            "X-Image-Title",
            "X-Job-Id",
            "X-Prompt-Id",
            "X-Seed",
            "X-Elapsed-Ms",
        ],
    )

    @app.get("/livez")
    async def liveness() -> dict:
        """The process is up and serving; says nothing about ComfyUI (the desktop app's supervisor polls this)."""
        return {"status": "ok"}

    @app.get("/health", response_model=HealthResponse)
    async def health_check(request: Request) -> JSONResponse:
        cfg: Settings = request.app.state.settings
        comfy: ComfyUIProvider = request.app.state.providers["comfyui"]
        default_workflow_present = DEFAULT_MODEL in request.app.state.models

        try:
            await comfy.health()
        except ApiError as exc:
            payload = HealthResponse(
                status="degraded",
                comfyui_connected=False,
                comfyui_url=cfg.comfyui_http_url,
                default_workflow_present=default_workflow_present,
                detail=exc.message,
            )
            return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=payload.model_dump())

        payload = HealthResponse(
            status="ok",
            comfyui_connected=True,
            comfyui_url=cfg.comfyui_http_url,
            default_workflow_present=default_workflow_present,
        )
        return JSONResponse(status_code=status.HTTP_200_OK, content=payload.model_dump())

    @app.get("/api/v1/models", dependencies=[Depends(require_api_key)])
    async def list_models(request: Request) -> dict:
        models: dict[str, CatalogModel] = request.app.state.models
        cloud = "cloud" in request.state.key.scopes  # a key that can't use cloud models doesn't see them
        return {"models": [m.model_dump(mode="json") for m in models.values() if cloud or not m.cloud]}

    @app.get("/api/v1/styles", dependencies=[Depends(require_api_key)])
    async def list_styles(request: Request) -> dict:
        """Every style; builtin: ships with the app (can be reset), edited: a user copy is in use."""
        cfg: Settings = request.app.state.settings
        styles: dict[str, Style] = request.app.state.styles
        user = cfg.user_styles_dir
        return {
            "styles": [
                s.model_dump(mode="json")
                | {
                    "builtin": (cfg.catalog_dir / "styles" / f"{s.id}.json").is_file(),
                    "edited": bool(user and (user / f"{s.id}.json").is_file()),
                }
                for s in styles.values()
            ]
        }

    def _user_style_path(request: Request, style_id: str) -> Path:
        cfg: Settings = request.app.state.settings
        if cfg.user_styles_dir is None:
            raise ApiError("bad_params", "Styles can't be saved: USER_STYLES_DIR isn't set.")
        if not re.fullmatch(ID_PATTERN, style_id):
            raise ApiError("bad_params", "A style id is lowercase letters, digits, - and _.")
        return cfg.user_styles_dir / f"{style_id}.json"

    def _reload_styles(request: Request) -> None:
        cfg: Settings = request.app.state.settings
        request.app.state.styles = load_styles(cfg.catalog_dir / "styles", cfg.user_styles_dir)

    @app.put("/api/v1/styles/{style_id}", dependencies=[Depends(require_main_key)])
    async def save_style(style_id: str, body: Style, request: Request) -> dict:
        """Saves a user copy: a new style, or an edit that replaces the built-in with the same id."""
        if body.id != style_id:
            raise ApiError("bad_params", "The style's id must match the URL.")
        path = _user_style_path(request, style_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body.model_dump(mode="json", exclude_defaults=True), indent=2) + "\n", encoding="utf-8")
        _reload_styles(request)
        return body.model_dump(mode="json")

    @app.delete("/api/v1/styles/{style_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_main_key)])
    async def delete_style(style_id: str, request: Request) -> None:
        """Deletes the user copy: a built-in goes back to its default, a user style is gone."""
        path = _user_style_path(request, style_id)
        if not path.is_file():
            raise ApiError("not_found", f"There's no saved style '{style_id}'.")
        path.unlink()
        _reload_styles(request)

    @app.post("/api/v1/providers/{provider_id}/check", dependencies=[Depends(require_main_key)])
    async def check_provider(provider_id: str, request: Request) -> dict:
        """Tests a remote provider's key; the desktop app's "Test key" button."""
        provider = request.app.state.providers.get(provider_id)
        if provider is None or provider_id == "comfyui":
            raise ApiError("not_found", f"No '{provider_id}' provider is set up.")
        await provider.check()
        return {"ok": True}

    def _submit(
        request: Request, job: ProviderRequest, style: Style | None, user_prompt: str, response_format: str,
        negative: str | None = None,
    ) -> Job:
        provider = request.app.state.providers.get(job.model.provider)
        if provider is None:
            raise ApiError("provider_down", f"No '{job.model.provider}' provider is set up.")
        key = request.state.key
        scope = "cloud" if job.model.cloud else "local"
        if scope not in key.scopes:
            where = "cloud models, which may spend credits" if scope == "cloud" else "models on this computer"
            raise ApiError("forbidden", f"This key can't use {where}. Allow it on the Connect page.")
        jobs: JobQueue = request.app.state.jobs
        if key.daily_cap is not None:
            made, _ = request.app.state.stats.key_usage().get(key.id, (0, 0))
            waiting = sum(j.key == key.id and j.status in ("queued", "running") for j in jobs.jobs.values())
            if made + waiting >= key.daily_cap:
                raise ApiError("daily_cap", f"This key's limit is {key.daily_cap} images a day. Try again later.")
        return jobs.submit(
            lambda on_progress, on_preview: provider.generate(job, on_progress, on_preview),
            model=job.model.id,
            prompt=user_prompt,
            response_format=response_format,
            local=job.model.provider == "comfyui",  # remote APIs don't share the local GPU
            style=style.id if style else None,
            key=key.id,
            params={"width": job.params.width, "height": job.params.height, "steps": job.params.steps,
                    "cfg": job.params.cfg, "negative": negative},
        )

    def _plan_images(request: Request, body: ImagesRequest) -> tuple[ProviderRequest, Style | None]:
        """Check an images request against the catalog: the model, style and final params."""
        models: dict[str, CatalogModel] = request.app.state.models
        styles: dict[str, Style] = request.app.state.styles

        model = models.get(body.model or DEFAULT_MODEL)
        if model is None:
            raise ApiError("bad_params", f"Unknown model '{body.model}'. Use one of: {', '.join(models)}.")
        model = with_user_settings(request.app.state.settings.engine_file, model)
        style = None
        if body.style:
            style = styles.get(body.style)
            if style is None:
                raise ApiError("bad_params", f"Unknown style '{body.style}'. Use one of: {', '.join(styles)}.")
        width = height = None
        if body.size and body.size != "auto":
            width, height = (int(v) for v in body.size.split("x"))

        params = resolve_params(
            model,
            style=style,
            quality=None if body.quality == "auto" else body.quality,
            width=width,
            height=height,
            steps=body.steps,
            cfg=body.cfg,
            n=body.n,
        )
        prompt = compose(model, body.prompt, style=style, negative=body.negative_prompt)
        return ProviderRequest(model, prompt, params, body.seed), style

    # The MCP tools (app.mcp) queue through the same checks as the OpenAI endpoint.
    app.state.submit_images = lambda request, body: _submit(
        request, *_plan_images(request, body), body.prompt, body.response_format, body.negative_prompt
    )

    def _base(request: Request) -> str:
        """The address the client used (127.0.0.1, a LAN address, or the tunnel's https name), so
        image links work for it. Uvicorn trusts X-Forwarded-Proto only from 127.0.0.1 (the tunnel)."""
        return str(request.base_url).rstrip("/")

    def _image_data(cfg: Settings, base: str, job: Job) -> list[ImageData]:
        """The finished job's images as OpenAI-style data; url files are written once, for the first asker."""
        if job.data is None and job.result is not None:
            if job.response_format == "b64_json":
                job.data = [
                    ImageData(b64_json=base64.b64encode(img.content).decode("ascii")) for img in job.result.images
                ]
            else:
                slug = title_to_filename_slug(extract_image_title(job.prompt))
                job.data = [
                    ImageData(url=_build_generated_image(cfg, base, job.id, i, slug, img, "url").url)
                    for i, img in enumerate(job.result.images, start=1)
                ]
        return job.data or []

    @app.post(
        "/v1/images/generations",
        response_model=ImagesResponse,
        response_model_exclude_none=True,
        dependencies=[Depends(require_api_key)],
    )
    async def images_generations(body: ImagesRequest, request: Request) -> ImagesResponse:
        """OpenAI-compatible image generation over every catalog and provider model."""
        jobs: JobQueue = request.app.state.jobs
        job = _submit(request, *_plan_images(request, body), body.prompt, body.response_format, body.negative_prompt)
        result = await jobs.wait(job)
        data = _image_data(request.app.state.settings, _base(request), job)
        return ImagesResponse(created=int(time.time()), data=data, model=job.model, seed=result.seed)

    @app.post("/api/v1/jobs", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_api_key)])
    async def create_job(body: ImagesRequest, request: Request) -> dict:
        """Queue a generation and answer at once; follow it with GET /api/v1/jobs/{id} or the events."""
        jobs: JobQueue = request.app.state.jobs
        job = _submit(request, *_plan_images(request, body), body.prompt, body.response_format, body.negative_prompt)
        return {"id": job.id, "status": job.status, "queue_position": jobs.queue_position(job)}

    def _own_job(request: Request, job_id: str) -> Job:
        """The main key sees every job; another key only its own."""
        job = request.app.state.jobs.get(job_id)
        key = request.state.key
        if not key.owner and job.key != key.id:
            raise ApiError("not_found", f"No job '{job_id}'.")
        return job

    @app.get("/api/v1/jobs/{job_id}", dependencies=[Depends(require_api_key)])
    async def get_job(job_id: str, request: Request) -> dict:
        jobs: JobQueue = request.app.state.jobs
        job = _own_job(request, job_id)
        view = jobs.view(job)
        if job.status == "done":
            view["seed"] = job.result.seed if job.result else None
            view["data"] = [d.model_dump(exclude_none=True) for d in _image_data(request.app.state.settings, _base(request), job)]
        return view

    @app.delete("/api/v1/jobs/{job_id}", dependencies=[Depends(require_api_key)])
    async def cancel_job(job_id: str, request: Request) -> dict:
        jobs: JobQueue = request.app.state.jobs
        job = _own_job(request, job_id)
        jobs.cancel(job)
        await job.done.wait()
        return jobs.view(job)

    @app.websocket("/api/v1/events")
    async def job_events(ws: WebSocket) -> None:
        """Every job's queued/started/progress/done/failed/cancelled events, as JSON messages."""
        key = ws_api_key(ws.app, ws.headers, ws.query_params.get("api_key"))
        if key is None:
            await ws.close(code=1008, reason="Missing or invalid API key.")
            return
        # A browser drops the connection unless the server echoes one of the protocols it offered.
        await ws.accept(subprotocol=WS_PROTOCOL if ws_protocol_key(ws.headers) else None)
        jobs: JobQueue = ws.app.state.jobs
        listener = jobs.subscribe()

        async def forward() -> None:
            while True:
                event = await listener.get()
                job = jobs.jobs.get(event["id"])
                if key.owner or (job is not None and job.key == key.id):
                    await ws.send_json(event)

        sender = asyncio.create_task(forward())
        try:
            # Clients don't send anything; this returns when they leave or the server shuts down,
            # so a closed tab doesn't hold a listener (or uvicorn's shutdown) until the next event.
            while (await ws.receive())["type"] != "websocket.disconnect":
                pass
        finally:
            sender.cancel()
            jobs.unsubscribe(listener)

    @app.post(
        "/api/v1/generate",
        response_model=None,
        responses={200: {"content": {"image/png": {}, "application/json": {}}}},
        dependencies=[Depends(verify_api_key)],
    )
    async def generate_image(
        body: GenerateRequest,
        request: Request,
        response: Response,
    ) -> Response | GenerateResponse:
        cfg: Settings = request.app.state.settings
        comfy: ComfyUIProvider = request.app.state.providers["comfyui"]
        jobs: JobQueue = request.app.state.jobs

        try:
            if body.workflow == "default":
                # The default catalog model through the default style.
                model: CatalogModel = with_user_settings(cfg.engine_file, request.app.state.models[DEFAULT_MODEL])
                style: Style | None = request.app.state.styles.get(DEFAULT_STYLE)
                params = resolve_params(
                    model, style=style, width=body.width, height=body.height, steps=body.steps, cfg=body.cfg
                )
                prompt = compose(model, body.prompt, style=style, negative=body.negative_prompt)
                job = _submit(
                    request, ProviderRequest(model, prompt, params, body.seed), style, body.prompt, "legacy"
                )
            else:
                # The user's own workflow from WORKFLOWS_DIR, run as-is with no catalog or style.
                workflow_template, mapping = load_workflow_bundle(cfg.workflows_dir, body.workflow)
                job = jobs.submit(
                    lambda on_progress, on_preview: comfy.run_workflow(workflow_template, mapping, body, on_progress, on_preview),
                    model=body.workflow,
                    prompt=body.prompt,
                    response_format="legacy",
                    local=True,
                )
            result = await jobs.wait(job)
        except WorkflowNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except WorkflowValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        except ApiError as exc:
            raise HTTPException(
                status_code=LEGACY_STATUS.get(exc.code, exc.status_code),
                detail=exc.message,
            ) from exc
        prompt_id, raw_images, used_seed = result.job_ref, result.images, result.seed

        job_id = job.id
        elapsed_ms = job.elapsed_ms or 0
        image_title = extract_image_title(body.prompt)
        title_slug = title_to_filename_slug(image_title)
        header_title = safe_header_value(image_title)

        if body.response_format == "image":
            primary_raw = raw_images[0]
            headers = {
                "Content-Disposition": f'inline; filename="{title_slug}{primary_raw.extension}"',
                "X-Image-Title": header_title,
                "X-Job-Id": job_id,
                "X-Prompt-Id": prompt_id,
                "X-Elapsed-Ms": str(elapsed_ms),
            }
            if used_seed is not None:
                headers["X-Seed"] = str(used_seed)
            return Response(
                content=primary_raw.content,
                media_type=primary_raw.mime_type,
                headers=headers,
            )

        response.headers["X-Image-Title"] = header_title
        generated = [
            _build_generated_image(cfg, _base(request), job_id, idx, title_slug, img, body.response_format)
            for idx, img in enumerate(raw_images, start=1)
        ]
        primary = generated[0]

        return GenerateResponse(
            id=job_id,
            prompt_id=prompt_id,
            workflow=body.workflow,
            seed=used_seed,
            images=generated,
            image_url=primary.url,
            image_base64=primary.base64,
            data_url=primary.data_url,
            elapsed_ms=elapsed_ms,
        )

    @app.get("/api/v1/images/{filename}")
    async def get_generated_image(
        filename: str,
        request: Request,
        exp: int | None = Query(default=None),
        sig: str | None = Query(default=None),
    ) -> FileResponse:
        if not IMAGE_FILENAME_PATTERN.match(filename):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid filename.")

        cfg: Settings = request.app.state.settings
        if cfg.sign_image_urls:
            if exp is None or not sig:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Missing image URL signature.",
                )
            try:
                secret = cfg.effective_signing_secret
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=str(exc),
                ) from exc
            if not verify_image_signature(filename, exp, sig, secret):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Invalid or expired image URL signature.",
                )

        base_dir = cfg.output_dir.resolve()
        file_path = (base_dir / filename).resolve()
        if not file_path.is_relative_to(base_dir) or not file_path.is_file():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found.")

        mime_type, _ = infer_mime_and_ext(filename)
        return FileResponse(
            file_path,
            media_type=mime_type,
            headers={"Cache-Control": "private, max-age=3600"},
        )

    return app


app = create_app()
