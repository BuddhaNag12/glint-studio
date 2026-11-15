# Plan: Glint Studio, a desktop app for hosting local image models ("LM Studio for image generation")

## Context
Serving image generation from your own machine today means a hand-built stack: ComfyUI, a small API bridge in front of it, the right models in the right folders, and a tunnel. Each piece needs Python, a venv, the right PyTorch build and a tunnel CLI set up by hand, and many people get stuck before the first image. Glint Studio starts from a working FastAPI bridge of that kind, and a review of it found design gaps worth fixing in a general-purpose host.

The goal is a free, open-source desktop app for Windows, macOS and Linux that anyone can install like LM Studio. It will:
- scan the machine and recommend models that fit it,
- download models in one click,
- generate images through built-in style presets (AI Art, Cartoon, Realistic, …) that the user can edit,
- serve a local or network API,
- open a remote tunnel in one click.

The bridge becomes the core of the app, and every gap in the notes is fixed in the design.

**Decisions made:**
- Engine: a managed ComfyUI that the app installs and runs.
- Distribution: free and open source (GPL-3.0, to match ComfyUI).
- Reach: desktop on all three OSes, a local/LAN API server, and a remote tunnel.

## Positioning
The product goals are **standalone, customizable, easy to set up**. Existing ComfyUI front-ends are mostly remote controls for a ComfyUI you set up yourself. The nearest one found (2026-11-16) is [2Wild Image/Video Gen Studio](https://github.com/tonyd2wild/2Wild-Image-Video-Gen-Studio): a web page backed by a single Python file that drives a fleet of ComfyUI instances.

| | 2Wild | This app |
|---|---|---|
| Standalone | No: install ComfyUI, start it with `--listen`, then run `server.py` | Installs and supervises Python, PyTorch, ComfyUI and the host |
| Setup | Edit `config.json`, download models by hand, check nodes, join sharded checkpoints | Scan → recommended model → one click |
| Customizable | Two fixed model families, hard-wired graphs, no prompt styles | Model catalog, editable style presets, later your own workflows |
| API | Web page only | OpenAI-compatible + async jobs API |
| Remote | Bring your own networking | LAN toggle, one-click tunnel, API keys in the keychain |

**Where it is ahead, and what we take from it:**
- It uses plain-language controls (Quality, Shape, Variation number) instead of sampler, steps and seed. **We adopt this in Phase 4.**
- Its web page is built for phones. **We adopt this in Phase 5:** the host serves a small mobile page.
- It runs several machines and frees VRAM when two jobs share a card. Not in v1; see "Later".
- It does video and turns a still into a video. Not in v1; see "Later".

## Architecture

```
┌──────────── Desktop app (Tauri v2, Rust) ────────────┐
│ React UI (Vite)          Rust core                   │
│  Onboarding / Discover   scan.rs      hardware probe │
│  Generate / Styles       recommend.rs catalog × HW   │
│  Server / Settings       runtime.rs   uv → Python,   │
│                                       torch, ComfyUI │
│                          download.rs  resumable+sha  │
│                          supervisor.rs  run, restart │
│                          tunnel.rs    cloudflared/   │
│                                       ngrok/tailscale│
└──────────────┬───────────────────────────────────────┘
               │ http://127.0.0.1:<port> (per-launch UI token)
        ┌──────▼──────────────────────┐      ┌───────────┐
        │ host (Python FastAPI)       │─WS──▶│ ComfyUI   │
        │ = evolved comfy-api-bridge  │◀REST─│ 127.0.0.1 │
        │ jobs · styles · catalog     │      └───────────┘
        │ OpenAI + native + legacy API│
        └──────▲──────────────────────┘
               │ LAN (opt-in) / tunnel (opt-in), API key required
          other apps, phones, existing bridge clients
```

**Why this split:**
- The Rust core does everything that has to happen **before** Python exists: scan the machine, install Python, supervise processes, run the tunnel.
- The Python host keeps the tested bridge code and runs in the same uv venv as ComfyUI.
- The UI only ever talks to the host.

## Repo structure

```
glint-studio/
  apps/desktop/
    src/                       React UI
      pages/ Onboarding, Discover, Generate, Styles, Server, Settings
      api/host.ts              typed client for the host API
    src-tauri/src/
      main.rs  scan.rs  recommend.rs  runtime.rs
      download.rs  supervisor.rs  tunnel.rs  paths.rs
  host/                        Python package (from comfy-api-bridge)
    app/
      main.py                  routes
      comfy_client.py          ported as-is
      workflow.py              ported as-is
      naming.py  auth.py       ported as-is
      jobs.py                  queue, progress, cancel, timeouts
      styles.py                prompt composition
      catalog.py               model/variant lookup, param clamping
      errors.py                {error:{code,message}} + status table
    tools/inspect_workflow.py  ported; powers "import my workflow"
    tests/                     ported 23 tests + new ones
  catalog/
    schema/model.schema.json  style.schema.json
    models/*.json              one file per model
    workflows/<family>.json + .mapping.json
    styles/*.json              built-in presets
  .github/workflows/ci.yml  release.yml (tauri-action, 3 OSes)
```

**App data** goes in the OS app-data folder (`paths.rs`):
- `runtime/`: uv, Python, venv, ComfyUI
- `models/`: wired into ComfyUI through `extra_model_paths.yaml`
- `outputs/`, `styles/` (user edits), `config.json`

Secrets (API keys, HF token) go in the OS keychain, never in files or `localStorage`.

## What was ported from the original bridge
| From | Becomes | Note |
|---|---|---|
| `app/comfy_client.py` | `host/app/comfy_client.py` | Keeps WebSocket-before-queue order, `SaveImageWebsocket` support, history and temp cleanup |
| `app/workflow.py` | `host/app/workflow.py` | Keeps path-traversal guard, UI-format detection, `deepcopy` injection |
| `app/naming.py` | `host/app/naming.py` | Titles and filenames; the duplicate TypeScript copy goes away |
| `app/auth.py` | `host/app/auth.py` | Constant-time key check, HMAC-signed image URLs |
| `scripts/inspect_workflow.py` | `host/tools/` | Auto-mapping for user-imported workflows |
| `workflows/default.json` | `catalog/workflows/z-image-turbo.json` | Its StringConcatenate nodes (72/73) are removed; the host composes prompts |
| Nodes 72/73/71 text | `catalog/styles/lockscreen-wallpaper.json` | Becomes the first built-in style |
| `scripts/start_bridge.sh/.ps1` tunnel logic | `tunnel.rs` | Same provider order and URL regexes; the ngrok skip-warning header stays documented for callers |
| the 23 bridge tests | `host/tests/` | Must stay green after the port |

## Design rules taken from the bridge review
1. **One error code per failure.** Every error is JSON `{error:{code,message}}`:

   | Code | Status |
   |---|---|
   | `engine_down` | 503 |
   | `model_missing` | 409 |
   | `workflow_failed` | 500 |
   | `bad_params` | 422 |
   | `unauthorized` | 401 |
   | `forbidden` | 403 |
   | `timeout` | 504 |

   Clients switch on `code`, never on the status alone. A wrong key gets its own code.
2. **Negatives are additive.** Final negative = style negative + model negative + user negative. Nothing replaces anything.
3. **Params are clamped to the model.** Each catalog variant declares `max_pixels`, the allowed `sizes`/aspects, a `steps` range, and whether it supports negatives. Anything outside these gets `bad_params` before it reaches the GPU.
4. **Jobs are asynchronous.**
   - `POST /api/v1/jobs` returns `{id, queue_position}`.
   - Progress arrives over WebSocket/SSE (ComfyUI `progress` events).
   - A job can be cancelled with `DELETE /api/v1/jobs/{id}`, which calls ComfyUI `/interrupt`.
   - A job has two timeouts: queue wait and run time.
   - `elapsed_ms` counts run time only.

   The synchronous endpoints wrap this queue.
5. **No secrets in the browser.** The UI gets a per-launch token from Tauri. Network clients use keychain-stored API keys.
6. **One title implementation**, the host's.

## Main pieces

### System scan (`scan.rs`)
- **What it reads:** OS and arch; CPU; RAM; free disk on the data volume; GPUs with vendor, name, VRAM and driver.
  - NVIDIA: `nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv`, or NVML.
  - Apple Silicon: `sysctl hw.memsize` and the chip name. The VRAM budget is roughly 60% of unified memory.
  - AMD on Linux: `rocm-smi`.
  - Windows, other vendors: DXGI adapter listing.
  - CPU, RAM and disk: the `sysinfo` crate.
- **What it outputs:** `HardwareProfile { backend: cuda|mps|rocm|xpu|cpu, vram_budget_gb, ram_gb, disk_free_gb, warnings[] }`.
- **The backend picks the PyTorch wheel:**
  - cuda: NVIDIA, after checking the driver is new enough
  - mps: Apple Silicon
  - rocm: AMD on Linux
  - xpu: Intel Arc
  - cpu: anything else, with a "slow" warning

  AMD on Windows is "experimental: CPU fallback" in v1.

### Recommendations (`recommend.rs`, pure function with unit tests)
- **Input:** a `HardwareProfile` and the catalog.
- **For each model:** pick the best variant that fits, in the order bf16 → fp8 → GGUF Q8 → Q4. "Fits" means: `min_vram ≤ vram_budget`, files × 1.2 ≤ free disk, and the backend is supported.
- **Output:** each model is tagged **Runs great**, **Runs slow** or **Won't fit**, with a one-line reason.
- **Ranking:** fit tier first, then catalog `quality_rank`. Models with permissive licenses are preferred as defaults.
- **Starting tiers.** These are placeholder numbers to calibrate against real hardware benchmarks before release:

  | Hardware | Models |
  |---|---|
  | CPU / under 6 GB | SD1.5-LCM, SDXL-Turbo at 512 |
  | 6–8 GB | SDXL, Z-Image Turbo fp8 or GGUF |
  | 12–16 GB | Z-Image Turbo bf16, Flux schnell fp8 |
  | 24 GB or more | Flux dev (license shown as non-commercial), Qwen-Image fp8 |

### Catalog (`catalog/models/*.json`)
Each model file has:
- `id`, `name`, `family`, `license` (+ a URL and a `commercial_ok` flag), `quality_rank`
- `variants[]`: each with `precision`, `min_vram_gb`, and `files[]`. A file has a `url` (Hugging Face), `sha256`, `bytes`, and `dest` (`diffusion_models` | `text_encoders` | `vae` | `loras`).
- `workflow` (the template id), `defaults` (size, steps, cfg, sampler), and `limits` (see rule 3)
- `quality` presets: `fast`, `standard` and `best`, each a set of steps/cfg/sampler values inside `limits`. These back the plain-language Quality control.
- `requires_hf_token`, for gated models

The catalog ships inside the app. It can also refresh from the repo's GitHub release, checked by sha256. On first run the app looks for existing ComfyUI or A1111 model folders and offers to link them, so the user doesn't download twice.

### Styles (`catalog/styles/*.json` + `host/app/styles.py`)
- **A style file has:** `{id, name, icon, prefix, suffix, negative, overrides{steps?, cfg?, aspect?}, models?[]}`.
- **Built-in styles:** AI Art, Cartoon, Realistic, Anime, 3D Render, Pixel Art, Watercolor, Cinematic, and Lock-screen Wallpaper.
- **Composition:** `positive = join(". ", prefix, user_prompt, suffix)`, with the negative built as in rule 2.
- **Editing:** the Styles page edits a copy in the user's `styles/` folder. Built-in styles show "Reset to default". Duplicate, New, and Import/Export JSON are also available.
- **Live preview:** a field shows the final composed prompt as the user types.
- **API:** `GET/PUT/DELETE /api/v1/styles`. `style` is a parameter on every generate endpoint.

### Host API
- **`POST /v1/images/generations`:** OpenAI-compatible (`prompt`, `size`, `n`, `response_format` = `b64_json` | `url`), plus the `model` and `style` extensions. Existing OpenAI clients work against it unchanged.
- **`POST /api/v1/jobs`, `GET` and `DELETE /api/v1/jobs/{id}`, `WS /api/v1/events`:** the native async API.
- **`POST /api/v1/generate`:** the legacy synchronous bridge contract, kept so clients of the original bridge work by changing only their base URL.
- **`GET /api/v1/models`, `/api/v1/styles`, `/health`.**
- **Binding:** 127.0.0.1 by default with no key needed. "Serve on network" binds 0.0.0.0 and requires a key. The tunnel always requires a key.

### Runtime install (`runtime.rs`)
1. Bundle the `uv` binary (about 30 MB).
2. On first run, `uv python install 3.12` and create the venv.
3. Install the PyTorch wheel for the scanned backend.
4. Install ComfyUI at a pinned git tag (from a GitHub release tarball), plus pinned custom nodes such as ComfyUI-GGUF, plus the host package.
5. Write `extra_model_paths.yaml`.
6. Show progress for each step, and make every step resumable and retryable.
7. "Repair runtime" wipes the venv and runs the steps again.

### Supervisor (`supervisor.rs`)
- Starts ComfyUI with `--listen 127.0.0.1 --port <free>`, then the host.
- Health-checks both, restarts them with backoff, captures logs to files, and shows them on the Server page.
- Stops everything when the app quits.

### Tunnel (`tunnel.rs`)
- **Default:** Cloudflare quick tunnel. It needs no account; `cloudflared` downloads on demand.
- **Options:** ngrok with a static domain, or Tailscale Funnel.
- **When it's up:** the UI shows the public URL and the API key, with copy buttons and curl/JS snippets. It warns that quick-tunnel URLs change on every restart.

## Roadmap
Each phase ends with a working milestone, and each can ship on its own.

**Phase 0 – Repo and port (about 2 days)**
- Create the monorepo.
- Port the bridge into `host/` with all 23 tests green.
- Set up CI: pytest, Rust tests, UI tests, a 3-OS build.

**Phase 1 – Host core, headless (about 1 week)**
- Catalog schema and loader, `styles.py`, `jobs.py`, `errors.py`, clamping.
- The OpenAI and native endpoints.
- The design-rule fixes above.
- Milestone: run the host against an existing ComfyUI; an existing bridge client generates through `/api/v1/generate` unchanged; curl `/v1/images/generations` with `style=cartoon`.

**Phase 2 – Desktop shell and runtime (about 2 weeks)**
- Tauri app, `scan.rs`, `runtime.rs`, `supervisor.rs`, and the Onboarding page: scan → install → ready.
- Milestone: on a clean Windows + NVIDIA machine and a clean Apple Silicon Mac, install the app and get ComfyUI and the host running without opening a terminal.

**Phase 3 – Catalog, recommendations, downloads (about 1–2 weeks)**
- `recommend.rs`, `download.rs` (resume, sha256, HF token in the keychain), the Discover page, and linking existing model folders.
- Milestone: scan → "Recommended for you" → one click → model ready.

**Phase 4 – Generate and Styles UI (about 1–2 weeks)**
- **Generate page:** model picker, style chips, prompt, queue with progress and cancel, gallery with metadata.
- **Plain-language controls by default:** Quality (Fast, Standard or Best, from the catalog `quality` presets), Shape (from the model's allowed sizes), and Variation number (the seed). Sampler, steps and CFG go under an "Advanced" toggle.
- **Styles page:** editor with live composed-prompt preview, reset, import and export.

**Phase 5 – Server and remote (about 1 week)**
- LAN toggle, API key management, `tunnel.rs`, and the Server page with endpoint docs and snippets.
- **Mobile web page:** when LAN or the tunnel is on, the host serves a small phone-friendly Generate page with the same plain-language controls.
  - To pair a phone, scan a QR code on the Server page. The QR holds the URL and a key just for that phone, which can be revoked.
  - The key stays on that phone. The desktop UI still never keeps a key in browser storage (rule 5).
- Milestone: a phone on cellular generates through the tunnel URL with a key, and gets a 401 without one.

**Phase 6 – Release (about 1 week)**
- Signing: macOS notarization; Windows code signing (SignPath offers free signing for open-source projects).
- Linux AppImage and .deb, and the Tauri auto-updater.
- License notices: the app is GPL-3.0; the model licenses are shown in the catalog.

**Later:**
- Import your own ComfyUI workflow (uses `inspect_workflow.py` auto-mapping).
- LoRAs, img2img/inpaint, batch.
- stable-diffusion.cpp as a lightweight second engine behind the same host API.
- Multi-GPU, then several machines in one queue, freeing VRAM when jobs share a card.
- Video, and turning a finished still into a video.

## Verification
- **Host:** `pytest` covers the ported 23 tests plus new ones:
  - style composition keeps every negative
  - clamping rejects 4096² or 150 steps for a model
  - each failure maps to its own `code`
  - the OpenAI response shape is right
  - job cancel and both timeouts work
  - the legacy `/api/v1/generate` matches the old contract (headers `X-Image-Title`, `X-Seed`, `Content-Disposition`)
- **Rust:** unit tests for `recommend.rs` with fixture profiles (CPU-only, 8 GB NVIDIA, 24 GB NVIDIA, 16 and 64 GB Apple Silicon), and for the parsers in `scan.rs` using captured `nvidia-smi`/`sysctl` output.
- **End to end, each phase:** a smoke script that waits for `/health`, curls `/v1/images/generations` and checks for a PNG, then curls the tunnel URL with and without a key. Run it in CI where possible, and by hand on real Windows+NVIDIA and Mac hardware. The recommendation thresholds are calibrated from these runs.
- **Compatibility:** point an existing bridge client at the host and generate through `/api/v1/generate` with no other change.
