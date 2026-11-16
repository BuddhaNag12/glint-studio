# Glint Studio

A free, open-source desktop app for running image-generation models on your own machine,
the way LM Studio does for language models. It scans your hardware, recommends models that fit,
downloads them in one click, generates through editable style presets, and serves a local,
LAN or tunnelled API. No GPU? Plug in your own key for an external image API and use it the same way.

It's for anyone who has tried to wire up ComfyUI, models, an API bridge and a tunnel by hand and
got stuck: Glint Studio sets the whole stack up for you.

After the first-run setup it lives in the menu bar (the system tray on Windows and Linux). **Open
Dashboard** opens a page in your browser with what's running, images per day, GPU memory, the
models that fit your machine, and the address and key other apps use to connect.

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
| `apps/desktop/` | Tauri v2 app: menu-bar icon, setup window (React) and Rust core; `src/dashboard/` is the browser dashboard the host serves |
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
`GET /api/v1/jobs/{id}` or the `WS /api/v1/events` stream, and cancel it with `DELETE /api/v1/jobs/{id}`.
The stream takes the key in the header, or (for browsers, which can't set headers on a WebSocket) as the
subprotocols `glint.v1, glint.key.<key>`; `?api_key=` still works but puts the key in server logs.

Desktop (needs Node 22+ and [Rust + the Tauri prerequisites](https://tauri.app/start/prerequisites/)):

```bash
cd apps/desktop
npm install
npm run tauri dev
```

On first run the app installs its own runtime into the OS app-data folder (`dev.glintstudio.app`):
uv, Python 3.12, a venv, and the host's dependencies (about 50 MB, no admin rights). Choosing the
local engine also installs PyTorch for your GPU and ComfyUI v0.45.0 (about 2 GB of disk on an Apple
Silicon Mac; more with NVIDIA or AMD, whose PyTorch builds are larger). It then starts ComfyUI (if installed) on a free localhost port and the host on
127.0.0.1:7788 (or its previous port, or a free one if that's taken), with an API key kept in the OS keychain so
apps keep working across restarts; logs go to `logs/host.log` and `logs/comfyui.log` there.
When setup is done the window hides and the dashboard opens in your browser; from then on the app
starts in the menu bar. The dashboard's Models page suggests models that fit your GPU and downloads
them into `models/` there (resumable, each file checked against the catalog's sha256). Closing the
window keeps everything running; **Quit** in the menu stops it.

To work on the dashboard without the app, build it (`npm run build` in `apps/desktop`) and run the
host with `DASHBOARD_DIR=../apps/desktop/dist`. The dashboard needs a login link signed with the
host's key: `/login?exp=<unix time, at most 60 s ahead>&sig=<hex HMAC-SHA256 of
"glint-dashboard-login:<exp>" with the API key>`.
Rust tests: `cargo test` in `apps/desktop/src-tauri`; the real installs against the network are
`cargo test real_install_and_host -- --ignored --nocapture`, then `cargo test real_engine -- --ignored --nocapture`,
then `cargo test real_first_image -- --ignored --nocapture` (downloads Z-Image Turbo, about 21 GB, and makes one image).

## Release

1. In a PR, set the new version in every file: `node scripts/bump-version.mjs 0.2.0`.
2. Merge it into `main`. **Release on merge** sees a version with no tag, tags `v0.2.0`, and builds the installers
   for macOS (Apple Silicon and Intel), Windows and Linux into a draft GitHub release.
3. Check the draft, then publish it.

Merges that keep the version build nothing. Pushing a tag like `v0.2.0` by hand also starts a build.

The installers aren't signed yet, so macOS and Windows warn on first open.

## License

GPL-3.0-only, matching ComfyUI. Each model keeps its own license, shown in the catalog.
