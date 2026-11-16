# Plan: Glint Studio, a desktop app for hosting local image models ("LM Studio for image generation")

## Context
Serving image generation from your own machine today means a hand-built stack: ComfyUI, a small API bridge in front of it, the right models in the right folders, and a tunnel. Each piece needs Python, a venv, the right PyTorch build and a tunnel CLI set up by hand, and many people get stuck before the first image. Glint Studio starts from a working FastAPI bridge of that kind, and a review of it found design gaps worth fixing in a general-purpose host.

The goal is a free, open-source desktop app for Windows, macOS and Linux that anyone can install like LM Studio. It will:
- stay lightweight: a small installer, and the heavy local engine only if you want it,
- scan the machine and recommend models that fit it,
- download models in one click,
- use external image APIs (OpenAI, Replicate, fal.ai, …) with your own API key, next to local models,
- generate images through built-in style presets (AI Art, Cartoon, Realistic, …) that the user can edit,
- let you customize models, endpoints, styles, workflows and defaults,
- serve a local or network API,
- open a remote tunnel in one click.

The bridge becomes the core of the app, and every gap in the review is fixed in the design.

**Decisions made:**
- Engine: a managed ComfyUI that the app installs and runs, **optional**.
- Providers: local ComfyUI, a remote ComfyUI, and external image APIs with the user's own keys, all behind one host API.
- Footprint: lightweight first. The installer has no Python and no PyTorch, and nothing heavy is installed until the user picks a local model.
- Distribution: free and open source (GPL-3.0, to match ComfyUI).
- Reach: desktop on all three OSes, a local/LAN API server, and a remote tunnel.

## Positioning
The product goals are **standalone, lightweight, customizable, easy to set up**. Existing ComfyUI front-ends are mostly remote controls for a ComfyUI you set up yourself. The nearest one found (2026-11-16) is [2Wild Image/Video Gen Studio](https://github.com/tonyd2wild/2Wild-Image-Video-Gen-Studio): a web page backed by a single Python file that drives a fleet of ComfyUI instances.

| | 2Wild | This app |
|---|---|---|
| Standalone | No: install ComfyUI, start it with `--listen`, then run `server.py` | Installs and supervises Python, PyTorch, ComfyUI and the host |
| Setup | Edit `config.json`, download models by hand, check nodes, join sharded checkpoints | Scan → recommended model → one click |
| Footprint | Light (one Python file), but needs a full ComfyUI install elsewhere | Small installer; cloud-only use needs no PyTorch; local engine installed on demand |
| Customizable | Two fixed model families, hard-wired graphs, no prompt styles | Model catalog, custom endpoints, editable styles, your own workflows |
| Cloud APIs | None | OpenAI-compatible and other image APIs with your own key |
| API | Web page only | OpenAI-compatible + async jobs API |
| Remote | Bring your own networking | LAN toggle, one-click tunnel, API keys in the keychain |

**Where it is ahead, and what we take from it:**
- It uses plain-language controls (Quality, Shape, Variation number) instead of sampler, steps and seed. **We adopt this in Phase 5.**
- Its web page is built for phones. **We adopt this in Phase 6:** the host serves a small mobile page.
- It runs several machines and frees VRAM when two jobs share a card. Not in v1; see "Later".
- It does video and turns a still into a video. Not in v1; see "Later".

## Architecture

Glint Studio lives in the menu bar (the system tray on Windows and Linux), like LM Studio's server
mode or Ollama. Day to day you use a **dashboard in your browser**, served by the host. The app's
own window is only for first-run setup, repair, and cloud keys.

```
 menu bar ▾  ● Running · Open Dashboard · Setup & Cloud Keys… · Quit
┌──────────── Desktop app (Tauri v2, Rust) ────────────┐
│ Setup window (React)     Rust core                   │
│  Onboarding → Setup      scan.rs      hardware probe │
│  Cloud keys              runtime.rs   uv → Python;   │
│                                       torch+ComfyUI  │
│                                       on demand      │
│                          supervisor.rs run, restart  │
│                          secrets.rs   OS keychain    │
│                          dashboard.rs tray + signed  │
│                                       login link     │
│                          network.rs   LAN, tunnel    │
└──────────────┬───────────────────────────────────────┘
               │ starts it; opens /login?exp&sig in the browser
        ┌──────▼──────────────────────┐  provider interface
        │ host (Python FastAPI)       │──┬──▶ local ComfyUI (127.0.0.1, optional)
        │ jobs · styles · catalog     │  ├──▶ remote ComfyUI (your GPU box)
        │ OpenAI + native + legacy API│  ├──▶ OpenAI-compatible endpoint (any base URL)
        │ dashboard: Overview, Models,│  └──▶ Replicate · fal.ai · Stability · BFL · Google
        │  Connect; job stats (SQLite)│
        │ recommendations, downloads  │
        └──────▲──────────────────────┘
               │ LAN (opt-in) / tunnel (opt-in), API key required
          other apps, phones, existing bridge clients
```

**Why this split:**
- The Rust core does everything that has to happen **before** Python exists: scan the machine, install Python, keep secrets, supervise processes, run the tunnel. It also owns the menu-bar icon.
- The Python host keeps the tested bridge code. Every backend, local or cloud, sits behind one provider interface, so styles, clamping, jobs, errors and the API work the same for all of them.
- The host also serves the dashboard and does everything that needs it running anyway: model recommendations (from the app's scan, written to `hardware.json`), resumable checked downloads, and job statistics. One codebase, testable with pytest.

**Dashboard security.** The host binds 127.0.0.1 (port 7788, or its last port, or a free one) and keeps one API key in the OS keychain, so apps keep working across restarts.
- The menu-bar app opens `/login?exp=…&sig=…`, signed with HMAC-SHA256 of the key and valid for 60 seconds, once.
- The host swaps it for an HttpOnly, SameSite=Strict session cookie. The page uses the cookie once to fetch the key, keeps it in memory, and calls the API like any other client.
- The host refuses any `Host` header but `127.0.0.1` and `localhost`, so a web page can't reach it through DNS rebinding.
- The events WebSocket takes the key as a subprotocol (`glint.v1, glint.key.<key>`), not in the URL, so it never lands in access logs.

## Repo structure

```
glint-studio/
  apps/desktop/
    src/                       React UI
      pages/ Onboarding, Setup, Providers (the setup window)
      dashboard/ Overview, Generate, Styles, Models, Connect (served by the host; dashboard.html entry)
    src-tauri/src/
      main.rs  scan.rs  runtime.rs  supervisor.rs  secrets.rs
      dashboard.rs  paths.rs  network.rs
  host/                        Python package (from comfy-api-bridge)
    app/
      main.py                  routes
      comfy_client.py          ported as-is
      workflow.py              ported as-is
      naming.py  auth.py       ported as-is
      jobs.py                  queue, progress, cancel, timeouts
      styles.py                prompt composition
      catalog.py               model/variant lookup, param clamping
      local_models.py          recommendations and downloads (resume, sha256)
      stats.py                 job history in SQLite, for the dashboard
      dashboard.py             login, the dashboard page and its API
      errors.py                {error:{code,message}} + status table
      providers/
        base.py                Provider interface + capabilities
        comfyui.py             local and remote ComfyUI (wraps comfy_client)
        openai_compat.py       any OpenAI-compatible images endpoint
        replicate.py  fal.py  stability.py  bfl.py  google.py
    tools/inspect_workflow.py  ported; powers "import my workflow"
    tests/                     ported 23 tests + new ones
  catalog/
    schema/model.schema.json  style.schema.json  provider.schema.json
    models/*.json              one file per model (local or cloud)
    providers/*.json           one file per provider: name, base URL, auth style, docs link
    workflows/<family>.json + .mapping.json
    styles/*.json              built-in presets
  .github/workflows/ci.yml  release.yml (tauri-action, 3 OSes)
```

**App data** goes in the OS app-data folder (`paths.rs`):
- `runtime/`: uv, Python, venv, and ComfyUI once the local engine is installed
- `models/`: wired into ComfyUI through `extra_model_paths.yaml`
- `outputs/`, `styles/` (user edits), `workflows/` (imported), `providers.json` (user endpoints and models, never keys), `config.json`
- `hardware.json` (the app's scan, for recommendations) and `stats.db` (job history, 90 days)

Secrets (provider API keys, network API keys, HF token) go in the OS keychain, never in files, logs or `localStorage`.

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

   | Code | Status | Meaning |
   |---|---|---|
   | `engine_down` | 503 | local or remote ComfyUI unreachable |
   | `model_missing` | 409 | model not downloaded or not on the server |
   | `workflow_failed` | 500 | ComfyUI rejected or failed the graph |
   | `bad_params` | 422 | outside the model's limits |
   | `unauthorized` | 401 | missing or wrong key for **this** host |
   | `forbidden` | 403 | key lacks the scope (for example cloud use) |
   | `timeout` | 504 | queue or run timeout |
   | `provider_auth` | 502 | the external provider rejected **your provider key** |
   | `provider_quota` | 429 | provider rate limit, quota or billing |
   | `provider_rejected` | 422 | provider refused the prompt (content policy) |
   | `provider_down` | 503 | provider unreachable or erroring |

   Clients switch on `code`, never on the status alone. A wrong key gets its own code, and a wrong **provider** key is never reported as the caller's auth failure.
2. **Negatives are additive.** Final negative = style negative + model negative + user negative. Nothing replaces anything. For models that don't accept negatives (most cloud APIs), the negative is not sent and the UI says so.
3. **Params are clamped to the model.** Each catalog model or variant declares `max_pixels`, the allowed `sizes`/aspects, a `steps` range, `n` max, and whether it supports negatives and seeds. Anything outside these gets `bad_params` before it reaches the GPU or a paid API.
4. **Jobs are asynchronous.**
   - `POST /api/v1/jobs` returns `{id, queue_position}`.
   - Progress arrives over WebSocket/SSE (ComfyUI `progress` events; polling status for providers that offer it).
   - A job can be cancelled with `DELETE /api/v1/jobs/{id}`, which calls ComfyUI `/interrupt` or the provider's cancel call where one exists.
   - A job has two timeouts: queue wait and run time.
   - `elapsed_ms` counts run time only.
   - Local jobs share one GPU queue; cloud jobs run in a separate queue so they don't wait behind the GPU.

   The synchronous endpoints wrap this queue.
5. **No secrets in the browser.** The UI gets a per-launch token from Tauri. Network clients use keychain-stored API keys. Provider keys never leave the Rust core and the host's memory.
6. **One title implementation**, the host's.

## Main pieces

### Lightweight by design
Targets, checked in CI where they can be:

| | Target |
|---|---|
| Installer | under 25 MB (Tauri uses the OS webview; no Python, PyTorch or uv inside). CI fails the build above it. |
| Core runtime | about 100 MB: uv + Python 3.12 + the host, **no PyTorch**. Enough for cloud providers and remote ComfyUI. |
| Local engine | PyTorch + ComfyUI (a few GB) plus models, installed only when the user picks a local model. |
| Idle | ComfyUI starts on the first local job and stops after an idle timeout (default 10 min, configurable), freeing VRAM and RAM. Host idle RAM under 150 MB. |

- No background downloads. The catalog refresh and update check are one small request each, and both can be turned off.
- A cloud-only user never downloads PyTorch, ComfyUI or a model.
- "Remove local engine" deletes PyTorch, ComfyUI and (optionally) models to get the space back.

### Providers: external APIs with your own key (`host/app/providers/`)
- **One interface.** Each provider implements `capabilities()`, `list_models()`, `generate(request, on_progress)`; cancelling the generate task is the cancel call. Styles, clamping, jobs, naming, outputs and the API sit above it, so every provider gets them for free.
- **Ships in this order:**
  1. `comfyui` local (the managed engine) and remote (any ComfyUI URL with an optional auth header, for example your own GPU PC).
  2. `openai_compat`: any OpenAI-compatible images endpoint, with a base URL and key. Covers OpenAI and the many hosts and self-hosted servers that copy that API, including another Glint Studio.
  3. Dedicated adapters for APIs with their own shape: Replicate, fal.ai, Stability AI, Black Forest Labs (Flux), Google. One file and one recorded-response test each; added one at a time after v1 if needed.
- **Keys.**
  - Entered on the Providers page and stored by `secrets.rs` in the OS keychain.
  - At host start, the Rust core hands keys to the host over the authenticated local channel. The host keeps them in memory only, and never writes, logs or returns them (the API shows only `has_key` and the last 4 characters).
  - "Test key" makes the cheapest call the provider allows (for example listing models) and maps the result to `provider_auth`, `provider_quota` or OK.
- **Models.** Cloud models are catalog entries with a `provider` field, `limits`, `supports_negative`, `supports_seed`, and no files. Users can add their own model IDs and endpoints; those live in `providers.json`.
- **Privacy and cost, said plainly.** Cloud models carry a "Cloud" badge. The first use of each provider says that prompts go to that provider and are billed to the user's account. Results are saved locally like any other output. The app shows no prices, since they change; it links to the provider's pricing page.
- **Sharing the server doesn't spend your credits by accident.** Network API keys (LAN, tunnel, phone pairing) can use local models only by default. Cloud use needs a `cloud` scope on that key, with an optional daily image cap. Without it a caller gets `forbidden`.

### Customization
Everything users can change is plain JSON in the app-data folder, so it can be backed up, shared or versioned. Settings has "Export all" and "Import".
- **Styles:** edit, duplicate, create, import and export (see Styles below).
- **Models and endpoints:** add a remote ComfyUI, an OpenAI-compatible endpoint, or a model ID for any provider.
- **Workflows:** import your own ComfyUI API-format workflow. `inspect_workflow.py` proposes the prompt/seed/size mapping, the user confirms it, and it shows up as a model.
- **Defaults:** default model and style, Quality, output folder and filename pattern, host port, idle timeout, and light or dark theme.

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

### Recommendations (`host/app/local_models.py`, pure function with unit tests)
- **Input:** a `HardwareProfile` and the catalog.
- **For each model:** pick the best variant that fits, in the order bf16 → fp8 → GGUF Q8 → Q4. "Fits" means: `min_vram ≤ vram_budget`, files × 1.2 ≤ free disk, and the backend is supported.
- **Output:** each model is tagged **Runs great**, **Runs slow** or **Won't fit**, with a one-line reason.
- **Ranking:** fit tier first, then catalog `quality_rank`. Models with permissive licenses are preferred as defaults.
- **Weak machines:** when nothing local runs great, the onboarding offers "Use a cloud provider" next to the best slow local option, so low-end laptops still get a good first image.
- **Starting tiers.** These are placeholder numbers to calibrate against real hardware benchmarks before release:

  | Hardware | Models |
  |---|---|
  | CPU / under 6 GB | SD1.5-LCM, SDXL-Turbo at 512 (or a cloud provider) |
  | 6–8 GB | SDXL, Z-Image Turbo fp8 or GGUF |
  | 12–16 GB | Z-Image Turbo bf16, Flux schnell fp8 |
  | 24 GB or more | Flux dev (license shown as non-commercial), Qwen-Image fp8 |

### Catalog (`catalog/models/*.json`)
Each model file has:
- `id`, `name`, `family`, `provider` (`comfyui` for local models), `license` (+ a URL and a `commercial_ok` flag), `quality_rank`
- `variants[]` (local models only): each with `precision`, `min_vram_gb`, and `files[]`. A file has a `url` (Hugging Face), `sha256`, `bytes`, and `dest` (`diffusion_models` | `text_encoders` | `vae` | `loras`).
- `workflow` (the template id, local models only), `defaults` (size, steps, cfg, sampler), and `limits` (see rule 3)
- `quality` presets: `fast`, `standard` and `best`, each a set of steps/cfg/sampler values (or the provider's own quality option) inside `limits`. These back the plain-language Quality control.
- `requires_hf_token`, for gated models

The catalog ships inside the app. It can also refresh from the repo's GitHub release, checked by sha256. On first run the app looks for existing ComfyUI or A1111 model folders and offers to link them, so the user doesn't download twice.

### Styles (`catalog/styles/*.json` + `host/app/styles.py`)
- **A style file has:** `{id, name, icon, prefix, suffix, negative, overrides{steps?, cfg?, aspect?}, models?[]}`.
- **Built-in styles:** AI Art, Cartoon, Realistic, Anime, 3D Render, Pixel Art, Watercolor, Cinematic, and Lock-screen Wallpaper.
- **Composition:** `positive = join(". ", prefix, user_prompt, suffix)`, with the negative built as in rule 2. The same composition is used for local and cloud models.
- **Editing:** the Styles page edits a copy in the user's `styles/` folder. Built-in styles show "Reset to default". Duplicate, New, and Import/Export JSON are also available.
- **Live preview:** a field shows the final composed prompt as the user types.
- **API:** `GET/PUT/DELETE /api/v1/styles`. `style` is a parameter on every generate endpoint.

### Host API
- **`POST /v1/images/generations`:** OpenAI-compatible (`prompt`, `size`, `n`, `response_format` = `b64_json` | `url`), plus the `model` and `style` extensions. Existing OpenAI clients work against it unchanged, whichever provider serves the model.
- **`POST /api/v1/jobs`, `GET` and `DELETE /api/v1/jobs/{id}`, `WS /api/v1/events`:** the native async API.
- **`POST /api/v1/generate`:** the legacy synchronous bridge contract, kept so clients of the original bridge work by changing only their base URL.
- **`GET /api/v1/models`** (local and cloud, each with `provider` and `available`), **`/api/v1/providers`** (configured providers, `has_key`, never the key), **`/api/v1/styles`, `/health`.**
- **Binding:** 127.0.0.1 by default with no key needed. "Serve on network" binds 0.0.0.0 and requires a key. The tunnel always requires a key. Cloud models over the network also need the key's `cloud` scope.

### Runtime install (`runtime.rs`)
Two tiers, so nothing heavy is installed unless it's used.
1. **Core (first run, about 100 MB):** download `uv` (pinned version, sha256-checked), `uv python install 3.12`, create the venv, install the host package. Cloud providers and remote ComfyUI work from here.
2. **Local engine (when the user picks a local model):** install the PyTorch wheel for the scanned backend, then ComfyUI at a pinned git tag (from a GitHub release tarball), plus pinned custom nodes such as ComfyUI-GGUF. Write `extra_model_paths.yaml`.
3. Show progress for each step, and make every step resumable and retryable.
4. "Repair runtime" wipes the venv and runs the steps again; "Remove local engine" uninstalls tier 2 only.

### Supervisor (`supervisor.rs`)
- Starts the host at app launch. Starts ComfyUI with `--listen 127.0.0.1 --port <free>` only when a local job arrives, and stops it after the idle timeout.
- Health-checks both, restarts them with backoff, captures logs to files, and shows them on the Server page.
- Stops everything when the app quits.

### Tunnel (`tunnel.rs`)
- **Default:** Cloudflare quick tunnel. It needs no account; `cloudflared` downloads on demand.
- **Options:** ngrok with a static domain, or Tailscale Funnel.
- **When it's up:** the UI shows the public URL and the API key, with copy buttons and curl/JS snippets. It warns that quick-tunnel URLs change on every restart.

## Roadmap
Each phase ends with a working milestone, and each can ship on its own.

**Phase 0 – Repo and port (about 2 days)** ✅ done 2026-11-16
- Create the monorepo.
- Port the bridge into `host/` with all 23 tests green.
- Set up CI: pytest, Rust tests, UI build, a 3-OS build.

**Phase 1 – Host core and provider interface, headless (about 1–2 weeks)** code done 2026-11-16; milestone not yet run against a real ComfyUI
- Catalog schema and loader, `styles.py`, `jobs.py`, `errors.py`, clamping.
- `providers/base.py`, with `comfyui` (local and remote) and `openai_compat` as the first two providers. Provider keys come from the environment in this phase.
- The OpenAI and native endpoints.
- The design-rule fixes above.
- Milestone: run the host against an existing ComfyUI; an existing bridge client generates through `/api/v1/generate` unchanged; curl `/v1/images/generations` with `style=cartoon` against both a ComfyUI model and an OpenAI-compatible model.

**Phase 2 – Desktop shell and lightweight runtime (about 2 weeks)**
- Tauri app, `scan.rs`, `runtime.rs` (both tiers), `supervisor.rs` (lazy start, idle stop), `secrets.rs`, and the Onboarding page: scan → choose local or cloud → install → ready.
- Installer size check in CI.
- Done so far: `scan.rs`, `supervisor.rs`, `runtime.rs` core tier (uv, Python, venv, host deps) and engine tier (PyTorch per backend, ComfyUI v0.45.0, started before the host and wired to it), the host started on a free port with a per-launch key, `secrets.rs` (provider keys in the OS keychain, passed to the host as env vars) with the Cloud provider page, and the Onboarding scan → choose → install → ready flow.
- Milestones:
  - On a clean machine, a cloud-only setup (core tier, no PyTorch) generates with a provider key, and the install stays near 100 MB.
  - On a clean Windows + NVIDIA machine and a clean Apple Silicon Mac, the local engine installs and ComfyUI and the host run without opening a terminal.

**Phase 3 – Menu bar, dashboard, recommendations, downloads (about 1–2 weeks)**
- The app moves to the menu bar; the setup window opens only on first run or from the menu.
- The host serves a browser dashboard:
  - **Overview:** engine and queue status, images per day (done and failed), GPU memory, live jobs, per-model typical and slowest times, errors, styles used.
  - **Models:** recommendations for this machine and one-click downloads (resume, sha256, cancel).
  - **Connect:** the address, the API key, and a curl example.
- Milestone: scan → open the dashboard → "Runs great" → one click → model ready → first image shows up on the Overview.
- Done so far (2026-11-16): all of the above in code. Model `variants` in the catalog (Z-Image Turbo bf16). Recommendations and downloads moved from Rust to the host (`local_models.py`), job stats (`stats.py`), the dashboard (`dashboard.py` + `apps/desktop/src/dashboard/`), and the menu-bar icon with the signed login link (`dashboard.rs`).
- Next: the first real local image with `cargo test real_first_image -- --ignored --nocapture`. It downloads Z-Image Turbo bf16 (about 21 GB) through the host, resuming any `.part` left in the test runtime. Then try Download in the real dashboard.
- Still to do: smaller variants (fp8, GGUF), which need the host to put file names into the workflow; the HF token; linking existing model folders; a monochrome menu-bar icon.

**Phase 4 – Providers and customization (about 1–2 weeks)**
- **Providers page:** add a provider, paste a key (keychain), "Test key", pick its models; add a remote ComfyUI or any OpenAI-compatible endpoint; add custom model IDs.
- Dedicated adapters, one at a time: Replicate, fal.ai, Stability AI, Black Forest Labs, Google.
- Separate cloud queue, provider cancel where supported, the `provider_*` error codes in the UI.
- **Workflow import:** load a ComfyUI API-format workflow, confirm the auto-mapping, use it as a model.
- Settings: defaults, output folder and filename pattern, idle timeout, theme, Export all / Import.
- Milestone: a user with no GPU adds an OpenAI-compatible key and a Replicate key, generates with the Cartoon style on both, and a wrong key shows "Provider rejected your key", not a login error.

**Phase 5 – Generate and Styles UI, in the dashboard (about 1–2 weeks)**
- **Generate page:** model picker (local and cloud, with the "Cloud" badge), style chips, prompt, queue with progress and cancel, gallery with metadata.
- **Plain-language controls by default:** Quality (Fast, Standard or Best, from the catalog `quality` presets), Shape (from the model's allowed sizes), and Variation number (the seed, hidden for models without seeds). Sampler, steps and CFG go under an "Advanced" toggle.
- **Styles page:** editor with live composed-prompt preview, reset, import and export.
- Done so far (2026-11-16): the Generate page (prompt, style chips, model with a cloud note, Shape, seed, gallery with progress, cancel and save) and the Styles page (edit, duplicate, new, reset to default, delete, live preview), on `PUT/DELETE /api/v1/styles/{id}`. Checked against a mock image provider.
- Also done (2026-11-16): Quality presets, an Advanced section with steps, CFG and the negative prompt (checked against the model's limits), style import and export as JSON, and jobs followed over the events socket instead of polling (the key goes in a WebSocket subprotocol, not the URL).
- Still to do: a sampler choice under Advanced (the catalog doesn't describe samplers yet).

**Phase 6 – Server and remote (about 1 week)**
- LAN toggle, API key management with scopes (`local` by default, `cloud` opt-in, optional daily cap), `tunnel.rs`, and the Connect page grows endpoint docs and snippets. A key that survives restarts, so other apps don't need a new one each launch (done 2026-11-16: keychain key and a stable port; rotating it is still to do).
- **Mobile web page:** when LAN or the tunnel is on, the host serves a small phone-friendly Generate page with the same plain-language controls.
  - To pair a phone, scan a QR code on the Server page. The QR holds the URL and a key just for that phone, which can be revoked.
  - The key stays on that phone. The desktop UI still never keeps a key in browser storage (rule 5).
- Milestone: a phone on cellular generates through the tunnel URL with a key, gets a 401 without one, and gets a 403 on a cloud model when its key has no `cloud` scope.
- Done 2026-11-16: the LAN toggle and the public link, on the Connect page.
  - The dashboard writes `network.json` through the host (`PUT /api/v1/network`, from this computer only). `network.rs` in the app applies it every 2 s.
  - **LAN:** restarts only the host, bound to 0.0.0.0 on the same port. ComfyUI keeps running. The change is refused while images are in progress. The setting is kept between launches.
  - **Public link:** a Cloudflare quick tunnel. cloudflared is downloaded on first use and checked against a pinned sha256. The link is read from its log and reported back through `tunnel.json`. It is never turned on at launch.
  - **Security:** the API needs the key from every address. The cookie pages (`/`, `/login`, `/api/v1/dashboard/connect`) still refuse any Host but `127.0.0.1` and `localhost`. Dashboard sessions are signed with the key, so they survive a host restart.
  - **Image links:** they use the address the caller used, and the tunnel gets https.

**Phase 7 – Release (about 1 week)**
- Signing: macOS notarization; Windows code signing (SignPath offers free signing for open-source projects).
- Linux AppImage and .deb, and the Tauri auto-updater.
- License notices: the app is GPL-3.0; the model licenses are shown in the catalog, and provider terms are linked on the Providers page.

**Later:**
- LoRAs, img2img/inpaint, batch.
- stable-diffusion.cpp as a lightweight local engine behind the same provider interface (no Python or PyTorch needed).
- Multi-GPU, then several machines in one queue, freeing VRAM when jobs share a card.
- Video, and turning a finished still into a video.

## Verification
- **Host:** `pytest` covers the ported 23 tests plus new ones:
  - style composition keeps every negative, and drops it (with a flag) for models without negatives
  - clamping rejects 4096² or 150 steps for a model
  - each failure maps to its own `code`, including the four `provider_*` codes
  - the OpenAI response shape is right
  - job cancel and both timeouts work
  - the legacy `/api/v1/generate` matches the old contract (headers `X-Image-Title`, `X-Seed`, `Content-Disposition`)
  - each provider adapter against recorded responses (`httpx.MockTransport`, no network in CI)
  - a provider key never appears in logs, errors or any API response
  - a network key without `cloud` scope gets `forbidden` on a cloud model
- **Recommendations:** pytest with fixture profiles (CPU-only, 8 GB NVIDIA, 24 GB NVIDIA, 16 and 64 GB Apple Silicon).
- **Rust:** unit tests for the parsers in `scan.rs` using captured `nvidia-smi`/`sysctl` output, and for the supervisor's idle-stop timer.
- **Footprint:** CI checks the installer size; a release checklist measures core-tier disk use and host idle RAM on each OS.
- **End to end, each phase:** a smoke script that waits for `/health`, curls `/v1/images/generations` and checks for a PNG, then curls the tunnel URL with and without a key. Run it in CI where possible, and by hand on real Windows+NVIDIA and Mac hardware. The recommendation thresholds are calibrated from these runs. Cloud providers are smoke-tested by hand with real keys, never in CI.
- **Compatibility:** point an existing bridge client at the host and generate through `/api/v1/generate` with no other change.
