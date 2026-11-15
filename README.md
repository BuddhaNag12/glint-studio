# Glint Studio

A free, open-source desktop app for running image-generation models on your own machine,
the way LM Studio does for language models. It scans your hardware, recommends models that fit,
downloads them in one click, generates through editable style presets, and serves a local,
LAN or tunnelled API. No GPU? Plug in your own key for an external image API and use it the same way.

It's for anyone who has tried to wire up ComfyUI, models, an API bridge and a tunnel by hand and
got stuck: Glint Studio sets the whole stack up for you.

Status: **Phase 1** in progress. The host is a port of a working ComfyUI bridge, now with a model
catalog, style presets and clear error codes.
See `docs/ROADMAP.md` for the plan.

## Why this project

Most ComfyUI front-ends are remote controls. They assume you have already installed ComfyUI and
Python, downloaded the right model files into the right folders, and written a config file. This
project is the whole thing in one install:

- **Standalone.** One installer sets up Python, the right PyTorch build for your GPU, ComfyUI and the
  host, then runs and restarts them for you. No terminal, no config file.
- **Lightweight.** A small installer with no Python or PyTorch inside. The heavy local engine
  (PyTorch + ComfyUI) installs only when you pick a local model, starts on the first job and stops
  when idle to free VRAM. Cloud-only use stays around 100 MB.
- **Easy setup.** It scans your machine, recommends models that fit your GPU or Apple chip, and
  downloads the right variant (bf16, fp8 or GGUF) in one click.
- **Bring your own API key.** Use external image APIs next to local models: any OpenAI-compatible
  endpoint, a remote ComfyUI, and adapters for Replicate, fal.ai, Stability AI, Black Forest Labs and
  Google. Keys live in the OS keychain and never reach the UI; cloud models are clearly marked, and
  shared server keys can't spend your credits unless you allow it.
- **Customizable.** Choose models from a catalog or add your own endpoints and model IDs. Import your
  own ComfyUI workflows. Style presets (AI Art, Cartoon, Realistic, …) are plain prompt text you can
  edit, duplicate, import and export, with a live preview of the final prompt. Settings export as JSON.
- **Simple by default.** Generate with Quality, Shape and Variation; samplers, steps and CFG sit
  behind "Advanced".
- **An API, not only a window.** An OpenAI-compatible `/v1/images/generations`, an async jobs API
  with progress and cancel, and opt-in LAN or one-click tunnel access, always behind an API key.

Not in v1: video, and spreading work across several machines.

## Layout

| Path | What |
|---|---|
| `host/` | Python FastAPI host: catalog, styles, providers, and the ported ComfyUI bridge |
| `apps/desktop/` | Tauri v2 app: React UI + Rust core (scaffold only for now) |
| `catalog/` | Model, workflow and style definitions (Phase 1) |

## Develop

Host (needs [uv](https://docs.astral.sh/uv/)):

```bash
cd host
uv venv --python 3.12
uv pip install -e ".[dev]"
uv run --no-sync pytest -q
```

Run it against an existing ComfyUI: copy `host/.env.example` to `host/.env`, set `API_KEY`,
then `uv run --no-sync uvicorn app.main:app --port 8000`.

With the host running, check it end to end (health, images, jobs and events, cancel, legacy generate,
auth; one PASS/FAIL line each, exit 1 on any failure): from `host/`, run
`uv run --no-sync python -m tools.smoke --api-key "$API_KEY"` (add `--model <id>` to try other models).

To add a remote image API, copy `host/providers.example.json` to `host/providers.json`, set
`PROVIDERS_FILE=providers.json` and the key it names (e.g. `OPENAI_API_KEY`) in `host/.env`. Then:

```bash
curl http://127.0.0.1:8000/v1/images/generations -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" -d '{"prompt": "a lighthouse at dusk", "style": "cartoon"}'
```

The same body on `POST /api/v1/jobs` queues the job and answers at once with its `id`. Follow it with
`GET /api/v1/jobs/{id}` or the `WS /api/v1/events` stream (key in the header or `?api_key=`), and
cancel it with `DELETE /api/v1/jobs/{id}`.

Desktop (needs Node 22+ and [Rust + the Tauri prerequisites](https://tauri.app/start/prerequisites/)):

```bash
cd apps/desktop
npm install
npm run tauri dev
```

## License

GPL-3.0-only, matching ComfyUI. Each model keeps its own license, shown in the catalog.
