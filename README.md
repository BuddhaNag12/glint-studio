<a id="readme-top"></a>

<!-- PROJECT SHIELDS -->
[![Contributors][contributors-shield]][contributors-url]
[![Forks][forks-shield]][forks-url]
[![Stargazers][stars-shield]][stars-url]
[![Issues][issues-shield]][issues-url]
[![License: GPL-3.0][license-shield]][license-url]

<!-- PROJECT LOGO -->
<br />
<div align="center">
  <a href="https://github.com/BuddhaNag12/glint-studio">
    <img src="branding/png/glint-icon-dark-1024.png" alt="Glint Studio logo" width="80" height="80">
  </a>

  <h3 align="center">Glint Studio</h3>

  <p align="center">
    Run image-generation models on your own machine, set up for you.
    <br />
    <a href="docs/ROADMAP.md"><strong>Read the plan »</strong></a>
    <br />
    <br />
    <a href="https://github.com/BuddhaNag12/glint-studio/releases">Download</a>
    &middot;
    <a href="https://github.com/BuddhaNag12/glint-studio/issues/new?labels=bug">Report Bug</a>
    &middot;
    <a href="https://github.com/BuddhaNag12/glint-studio/issues/new?labels=enhancement">Request Feature</a>
  </p>
</div>

> [!WARNING]
> **Alpha.** Glint Studio works end to end on the machines it has been tested on, but expect rough
> edges. The installers aren't signed yet, and the only local model today needs about 12 GB of GPU
> memory. See [Known limits](#known-limits).

<!-- TABLE OF CONTENTS -->
<details>
  <summary>Table of Contents</summary>
  <ol>
    <li>
      <a href="#about-the-project">About The Project</a>
      <ul>
        <li><a href="#built-with">Built With</a></li>
      </ul>
    </li>
    <li>
      <a href="#getting-started">Getting Started</a>
      <ul>
        <li><a href="#prerequisites">Prerequisites</a></li>
        <li><a href="#installation">Installation</a></li>
        <li><a href="#known-limits">Known limits</a></li>
        <li><a href="#uninstall">Uninstall</a></li>
      </ul>
    </li>
    <li><a href="#usage">Usage</a></li>
    <li><a href="#development">Development</a></li>
    <li><a href="#roadmap">Roadmap</a></li>
    <li><a href="#contributing">Contributing</a></li>
    <li><a href="#license">License</a></li>
    <li><a href="#contact">Contact</a></li>
    <li><a href="#acknowledgments">Acknowledgments</a></li>
  </ol>
</details>

<!-- ABOUT THE PROJECT -->
## About The Project

Glint Studio is a free, open-source desktop app for running image-generation models locally, the way
LM Studio does for language models. It's for anyone who has tried to wire up ComfyUI, model files, an
API bridge and a tunnel by hand and got stuck: one install sets up the whole stack.

* **Standalone.** One installer sets up Python, the right PyTorch build for your GPU, ComfyUI and the
  host, then runs and restarts them for you. No terminal, no config file.
* **Lightweight.** No Python or PyTorch in the installer. The heavy engine (PyTorch + ComfyUI) installs
  only when you pick a local model, and frees GPU memory when idle.
* **Fits your machine.** It scans your GPU or Apple chip, says which models run great, slow or not at
  all, and downloads them in one click (resumable, each file checked against its sha256).
* **Bring your own API key.** No GPU? Add any OpenAI-compatible image API, or a remote ComfyUI, and use
  it the same way. Keys live in the OS keychain and never reach the browser.
* **Simple by default.** Generate with Quality, Shape and Variation; steps, CFG and the negative prompt
  sit behind "Advanced". Style presets (AI Art, Cartoon, Realistic, …) are plain prompt text you can
  edit, duplicate, import and export.
* **An API, not only a window.** An OpenAI-compatible `/v1/images/generations`, an async jobs API with
  progress and cancel, and opt-in LAN or one-click public-link access, always behind an API key.

After the first-run setup the app lives in the menu bar (the system tray on Windows and Linux).
**Open Dashboard** opens a page in your browser with what's running, images per day, GPU memory, the
models that fit your machine, and the address and key other apps use to connect.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

### Built With

* [![Tauri][Tauri-badge]][Tauri-url]
* [![Rust][Rust-badge]][Rust-url]
* [![React][React-badge]][React-url]
* [![TypeScript][TypeScript-badge]][TypeScript-url]
* [![Vite][Vite-badge]][Vite-url]
* [![Python][Python-badge]][Python-url]
* [![FastAPI][FastAPI-badge]][FastAPI-url]
* [![ComfyUI][ComfyUI-badge]][ComfyUI-url]

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- GETTING STARTED -->
## Getting Started

### Prerequisites

* **macOS** (Apple Silicon or Intel), **Windows** 10/11 (64-bit), or **Linux** (x86_64).
* **For local images:** an NVIDIA GPU with 12 GB or more of VRAM, or an Apple Silicon Mac with
  24 GB or more of memory. Smaller machines can still use a cloud provider with their own key.
* **Disk:** about 100 MB for cloud-only use; about 2 GB more for the local engine, plus about 21 GB for
  Z-Image Turbo.
* An internet connection for the first-run setup and model downloads. No admin rights are needed.

### Installation

1. Download the installer for your system from
   [Releases](https://github.com/BuddhaNag12/glint-studio/releases): `.dmg` for macOS, `.msi` or
   `-setup.exe` for Windows, `.AppImage` or `.deb` for Linux.
2. Open it. The builds aren't signed yet, so your system will warn you the first time:
   * **macOS:** move Glint Studio to Applications and open it. When macOS blocks it, open
     **System Settings → Privacy & Security** and click **Open Anyway**. Or run:
     ```sh
     xattr -dr com.apple.quarantine "/Applications/Glint Studio.app"
     ```
   * **Windows:** on the SmartScreen dialog, click **More info → Run anyway**.
   * **Linux:** make the AppImage executable (`chmod +x Glint*.AppImage`) and run it.
3. Follow the setup window. It scans your machine, then installs its own runtime (uv, Python 3.12, a
   virtual environment and the host) into the app-data folder. Choosing the local engine also installs
   PyTorch for your GPU and ComfyUI.
4. When setup is done, the dashboard opens in your browser. Go to **Models**, pick one marked
   **Runs great**, and download it.

### Known limits

* **One local model.** Only Z-Image Turbo (bf16, about 12 GB of VRAM) is in the catalog. Smaller fp8
  and GGUF variants for 8 GB GPUs and 16 GB Macs are being added.
* **The first setup can look stuck.** Large downloads (PyTorch, ComfyUI's requirements) don't show
  progress yet, and setup can't be stopped halfway.
* **Unsigned builds.** macOS and Windows warn on first open (see above). There's no auto-updater yet:
  download new versions from Releases.
* **Cloud providers:** OpenAI-compatible endpoints and remote ComfyUI only. Dedicated adapters
  (Replicate, fal.ai, Stability AI, Black Forest Labs, Google) aren't built yet.
* **Tested hardware:** Apple Silicon Macs. Windows + NVIDIA and Linux have passed CI builds but not
  yet a full run on real hardware.

### Uninstall

Quit Glint Studio from the menu bar, remove the app, then delete its data folder. That folder holds the
runtime, downloaded models, your images and settings, so it can be tens of GB:

| System | Data folder |
|---|---|
| macOS | `~/Library/Application Support/dev.glintstudio.app` |
| Windows | `%APPDATA%\dev.glintstudio.app` (the uninstaller's "Delete the application data" box removes it) |
| Linux | `~/.local/share/dev.glintstudio.app` |

The API key and provider keys stay in the OS keychain (Keychain Access on macOS, Credential Manager on
Windows) under `dev.glintstudio.app`; delete them there too.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- USAGE EXAMPLES -->
## Usage

**From the dashboard:** open **Generate**, type a prompt, pick a style and a shape, and press Generate.
Past images are in the History sidebar. **Connect** shows the address and API key for other apps, and
turns on LAN access or a public link (a Cloudflare quick tunnel).

**From any app or script:** the host speaks the OpenAI images API on `127.0.0.1:7788` (the port is on
the Connect page):

```sh
curl http://127.0.0.1:7788/v1/images/generations \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"prompt": "a lighthouse at dusk", "style": "cartoon"}'
```

The same body on `POST /api/v1/jobs` queues the job and answers at once with its `id`. Follow it with
`GET /api/v1/jobs/{id}` or the `WS /api/v1/events` stream, and cancel it with
`DELETE /api/v1/jobs/{id}`. The stream takes the key in the header, or (for browsers, which can't set
headers on a WebSocket) as the subprotocols `glint.v1, glint.key.<key>`.

Errors are JSON `{"error": {"code", "message"}}`, with one code per failure (`engine_down`,
`model_missing`, `bad_params`, `unauthorized`, `forbidden`, `timeout`, …), so clients can switch on
`code`.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- DEVELOPMENT -->
## Development

| Path | What |
|---|---|
| `host/` | Python FastAPI host: catalog, styles, jobs, providers, the dashboard API, and the ComfyUI bridge |
| `apps/desktop/` | Tauri v2 app: menu-bar icon, setup window (React) and Rust core; `src/dashboard/` is the browser dashboard the host serves |
| `catalog/` | Model, workflow and style definitions |
| `branding/` | The logo and app icons |

**Host** (needs [uv](https://docs.astral.sh/uv/)):

```sh
cd host
uv venv --python 3.12
uv pip install -e ".[dev]"
uv run --no-sync pytest -q
```

Run it against an existing ComfyUI: copy `host/.env.example` to `host/.env`, set `API_KEY`, then
`uv run --no-sync uvicorn app.main:app --port 8000`. With the host running, check it end to end
(health, images, jobs and events, cancel, legacy generate, auth; one PASS/FAIL line each) with
`uv run --no-sync python -m tools.smoke --api-key "$API_KEY"` from `host/`.

To add a remote image API, copy `host/providers.example.json` to `host/providers.json`, and set
`PROVIDERS_FILE=providers.json` and the key it names (e.g. `OPENAI_API_KEY`) in `host/.env`.

**Desktop** (needs Node 22+ and [Rust + the Tauri prerequisites](https://tauri.app/start/prerequisites/)):

```sh
cd apps/desktop
npm install
npm run tauri dev
```

On first run the app installs its runtime into the app-data folder, starts ComfyUI (if installed) on a
free localhost port and the host on 127.0.0.1:7788, with an API key kept in the OS keychain. Logs go to
`logs/host.log` and `logs/comfyui.log` there.

To work on the dashboard without the app, build it (`npm run build` in `apps/desktop`) and run the host
with `DASHBOARD_DIR=../apps/desktop/dist`. The dashboard needs a login link signed with the host's key:
`/login?exp=<unix time, at most 60 s ahead>&sig=<hex HMAC-SHA256 of "glint-dashboard-login:<exp>" with
the API key>`.

**Rust tests:** `cargo test` in `apps/desktop/src-tauri`. The real installs against the network run
in order: `cargo test real_install_and_host -- --ignored --nocapture`, then `real_engine`, then
`real_first_image` (downloads Z-Image Turbo, about 21 GB, and makes one image).

**Release:**

1. In a PR, set the new version in every file: `node scripts/bump-version.mjs 0.2.0`.
2. Merge it into `main`. **Release on merge** sees a version with no tag, tags `v0.2.0`, and builds the
   installers for macOS (Apple Silicon and Intel), Windows and Linux into a draft GitHub release.
3. Check the draft, then publish it.

Merges that keep the version build nothing. Pushing a tag like `v0.2.0` by hand also starts a build.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- ROADMAP -->
## Roadmap

- [x] Host: model catalog, style presets, job queue, OpenAI-compatible and async APIs, clear error codes
- [x] Desktop app with a one-click runtime install (uv, Python, PyTorch for your GPU, ComfyUI)
- [x] Hardware scan, model recommendations, resumable downloads checked by sha256, delete a model
- [x] Menu bar and browser dashboard: Generate, Styles, Models, usage history, Bench, Logs, Settings
- [x] LAN access, public link through a Cloudflare quick tunnel, API keys with scopes
- [x] Cloud providers: OpenAI-compatible endpoints and remote ComfyUI, keys in the OS keychain
- [ ] Smaller model variants (fp8, GGUF) for 8 GB GPUs and 16 GB Macs
- [ ] Setup screen with download progress, Stop, and a fast path for parts already installed
- [ ] "Erase all data" in Settings
- [ ] Dedicated cloud adapters: Replicate, fal.ai, Stability AI, Black Forest Labs, Google
- [ ] Import your own ComfyUI workflow
- [ ] Chat history and a workflow view on Generate
- [ ] Signed and notarized installers, and an auto-updater

See [docs/ROADMAP.md](docs/ROADMAP.md) for the full plan, and the
[open issues](https://github.com/BuddhaNag12/glint-studio/issues) for known bugs.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- CONTRIBUTING -->
## Contributing

Contributions are welcome: bug reports, hardware test results (especially Windows + NVIDIA, AMD and
Linux), new catalog models and code.

1. Fork the project
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Run the host and Rust tests (see [Development](#development))
4. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
5. Push to the branch (`git push origin feature/AmazingFeature`)
6. Open a pull request into `main`

### Top contributors

<a href="https://github.com/BuddhaNag12/glint-studio/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=BuddhaNag12/glint-studio" alt="Contributors" />
</a>

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- LICENSE -->
## License

Distributed under GPL-3.0-only, matching ComfyUI. See [`LICENSE`](LICENSE). Each model keeps its own
license, shown in the catalog and on the Models page.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- CONTACT -->
## Contact

Questions and ideas: [open an issue](https://github.com/BuddhaNag12/glint-studio/issues).

Project link: [https://github.com/BuddhaNag12/glint-studio](https://github.com/BuddhaNag12/glint-studio)

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- ACKNOWLEDGMENTS -->
## Acknowledgments

* [ComfyUI](https://github.com/comfyanonymous/ComfyUI), the engine behind local generation
* [Z-Image Turbo](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo) by Tongyi-MAI
* [uv](https://docs.astral.sh/uv/), which installs Python and the runtime
* [Tauri](https://tauri.app), [FastAPI](https://fastapi.tiangolo.com) and [React](https://react.dev)
* [Cloudflare quick tunnels](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/) for the public link
* [Phosphor Icons](https://phosphoricons.com) and [IBM Plex Sans](https://github.com/IBM/plex)
* [LM Studio](https://lmstudio.ai) and oMLX for the idea and the admin layout
* [Best-README-Template](https://github.com/othneildrew/Best-README-Template) for this README's layout

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- MARKDOWN LINKS & IMAGES -->
[contributors-shield]: https://img.shields.io/github/contributors/BuddhaNag12/glint-studio.svg?style=for-the-badge
[contributors-url]: https://github.com/BuddhaNag12/glint-studio/graphs/contributors
[forks-shield]: https://img.shields.io/github/forks/BuddhaNag12/glint-studio.svg?style=for-the-badge
[forks-url]: https://github.com/BuddhaNag12/glint-studio/network/members
[stars-shield]: https://img.shields.io/github/stars/BuddhaNag12/glint-studio.svg?style=for-the-badge
[stars-url]: https://github.com/BuddhaNag12/glint-studio/stargazers
[issues-shield]: https://img.shields.io/github/issues/BuddhaNag12/glint-studio.svg?style=for-the-badge
[issues-url]: https://github.com/BuddhaNag12/glint-studio/issues
[license-shield]: https://img.shields.io/github/license/BuddhaNag12/glint-studio.svg?style=for-the-badge
[license-url]: https://github.com/BuddhaNag12/glint-studio/blob/main/LICENSE
[Tauri-badge]: https://img.shields.io/badge/Tauri-24C8D8?style=for-the-badge&logo=tauri&logoColor=white
[Tauri-url]: https://tauri.app
[Rust-badge]: https://img.shields.io/badge/Rust-000000?style=for-the-badge&logo=rust&logoColor=white
[Rust-url]: https://www.rust-lang.org
[React-badge]: https://img.shields.io/badge/React-20232A?style=for-the-badge&logo=react&logoColor=61DAFB
[React-url]: https://react.dev
[TypeScript-badge]: https://img.shields.io/badge/TypeScript-3178C6?style=for-the-badge&logo=typescript&logoColor=white
[TypeScript-url]: https://www.typescriptlang.org
[Vite-badge]: https://img.shields.io/badge/Vite-646CFF?style=for-the-badge&logo=vite&logoColor=white
[Vite-url]: https://vite.dev
[Python-badge]: https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white
[Python-url]: https://www.python.org
[FastAPI-badge]: https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white
[FastAPI-url]: https://fastapi.tiangolo.com
[ComfyUI-badge]: https://img.shields.io/badge/ComfyUI-172117?style=for-the-badge
[ComfyUI-url]: https://github.com/comfyanonymous/ComfyUI
