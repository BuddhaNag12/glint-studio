# Glint Studio

A free, open-source desktop app for running image-generation models on your own machine,
the way LM Studio does for language models. It scans your hardware, recommends models that fit,
downloads them in one click, generates through editable style presets, and serves a local,
LAN or tunnelled API.

It's for anyone who has tried to wire up ComfyUI, models, an API bridge and a tunnel by hand and
got stuck: Glint Studio sets the whole stack up for you.

Status: **Phase 0**. The repo is set up and the host is a port of a working ComfyUI bridge.
See `docs/ROADMAP.md` for the plan.

## Why this project

Most ComfyUI front-ends are remote controls. They assume you have already installed ComfyUI and
Python, downloaded the right model files into the right folders, and written a config file. This
project is the whole thing in one install:

- **Standalone.** One installer sets up Python, the right PyTorch build for your GPU, ComfyUI and the
  host, then runs and restarts them for you. No terminal, no config file.
- **Easy setup.** It scans your machine, recommends models that fit your GPU or Apple chip, and
  downloads the right variant (bf16, fp8 or GGUF) in one click.
- **Customizable.** Choose models from a catalog. Style presets (AI Art, Cartoon, Realistic, …) are
  plain prompt text you can edit, duplicate, import and export, with a live preview of the final prompt.
- **Simple by default.** Generate with Quality, Shape and Variation; samplers, steps and CFG sit
  behind "Advanced".
- **An API, not only a window.** An OpenAI-compatible `/v1/images/generations`, an async jobs API
  with progress and cancel, and opt-in LAN or one-click tunnel access, always behind an API key.

Not in v1: video, and spreading work across several machines.

## Layout

| Path | What |
|---|---|
| `host/` | Python FastAPI host in front of ComfyUI (ported bridge, 23 tests) |
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

Desktop (needs Node 22+ and [Rust + the Tauri prerequisites](https://tauri.app/start/prerequisites/)):

```bash
cd apps/desktop
npm install
npm run tauri dev
```

## License

GPL-3.0-only, matching ComfyUI. Each model keeps its own license, shown in the catalog.
