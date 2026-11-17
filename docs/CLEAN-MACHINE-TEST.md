# Clean-machine test

This runs the Phase 1 and Phase 2 milestones from [ROADMAP.md](ROADMAP.md) on real hardware. Each run starts
from a computer that has never had Glint Studio, Python or ComfyUI set up by hand. A fresh user account
works if you can't wipe the machine.

You need 30–60 minutes per machine, mostly waiting on downloads. Write each result in the table at the end.

## The machines

| Run | Machine | Path | Proves |
|---|---|---|---|
| A | Any Mac or PC | Cloud only | Phase 2: cloud setup generates; the install stays near 100 MB |
| B | Windows 10/11 with an NVIDIA card | Local engine | Phase 2: the engine installs and runs without a terminal |
| C | Apple Silicon Mac, 16 GB or more | Local engine | Phase 2: the same on macOS |
| D | Run B or C, afterwards | Local engine | Phase 1: an existing bridge client and `style=cartoon` against ComfyUI |

Run A needs an API key from an OpenAI-compatible image provider. You'll be billed a few cents.

## Before you start

- Get the installer for the machine from the latest release on GitHub.
- macOS: the build isn't signed yet. The first time, right-click Glint Studio in Applications and choose **Open**.
- Windows: if SmartScreen appears, choose **More info → Run anyway**.
- Don't open a terminal until step 6. Steps 1–5 must work without one.
- Delete any earlier data folder:
  - macOS: `~/Library/Application Support/dev.glintstudio.app`
  - Windows: `%APPDATA%\dev.glintstudio.app`
  - Linux: `~/.local/share/dev.glintstudio.app`

## Steps

1. **Install and open.** The app opens on **Welcome to Glint Studio** and scans the computer.
   - Check: **This computer** shows the right processor, memory, free disk and graphics card.
   - Check: **Runs models on** names the right chip, for example "NVIDIA GPU (CUDA)" or "Apple Silicon GPU (Metal)".
2. **Choose a path** under **How do you want to make images?**
   - Run A: **With a cloud provider**, then paste the provider key.
   - Runs B and C: **On this computer**. It should be marked **Recommended**.
3. **Install.** Watch each step finish. Note how long the install takes and any step that retries or fails.
   - Check: no terminal or console window ever appears.
   - Check: the menu-bar (or tray) icon appears, and its first line turns to **Server: running on …**.
4. **Get a model** (runs B and C only). Open **Dashboard** from the menu bar, go to Models → Discover, and
   download one model marked **Runs great**. Note its name and the download time.
5. **Generate in the app.** Menu bar → **Generate Images**. Make one image with the **Cartoon** style.
   - Check: an image appears and shows up in the dashboard history.
   - Note how long it took, first run and second run.
6. **Run the smoke test.** First, open **Connect** from the menu bar, make a new key and copy it. Then open a
   terminal and run the commands for your machine. They use Glint Studio's own Python, so nothing else needs
   to be installed.

   macOS:
   ```bash
   curl -fsSLo smoke.py https://raw.githubusercontent.com/BuddhaNag12/glint-studio/main/host/tools/smoke.py
   ~/Library/Application\ Support/dev.glintstudio.app/runtime/venv/bin/python smoke.py --api-key PASTE_KEY
   ```

   Windows (PowerShell):
   ```powershell
   curl.exe -fsSLo smoke.py https://raw.githubusercontent.com/BuddhaNag12/glint-studio/main/host/tools/smoke.py
   & "$env:APPDATA\dev.glintstudio.app\runtime\venv\Scripts\python.exe" smoke.py --api-key PASTE_KEY
   ```

   - Run A: add `--skip-legacy --skip-cancel`. Those two checks need the local engine.
   - Runs B and C: add nothing. Every line should say PASS.
   - The output ends with a line like `7/7 passed`. Copy the whole output into the results.
7. **Measure the install.** Run A should be near 100 MB.
   - macOS: `du -sh ~/Library/Application\ Support/dev.glintstudio.app`
   - Windows (PowerShell): `"{0:N0} MB" -f ((gci -r -force "$env:APPDATA\dev.glintstudio.app" | measure Length -sum).Sum / 1MB)`

   For runs B and C, also note the size of `models` inside that folder on its own.
8. **Restart.** Quit from the menu bar and open the app again.
   - Check: no onboarding this time. The server comes back up and generating still works.

## Run D: an existing bridge client

On machine B or C, after its run passes:

1. Point a client that used the old bridge at `http://127.0.0.1:7788`, with the key from step 6.
2. Generate through `POST /api/v1/generate` without changing anything else in the client.
   - Check: the image arrives, along with the headers `X-Image-Title` and `X-Seed`.
3. Then make a cartoon-style image through the OpenAI-compatible endpoint, with the same key:
   ```bash
   curl -s http://127.0.0.1:7788/v1/images/generations -H "Authorization: Bearer PASTE_KEY" \
     -H "Content-Type: application/json" -d '{"prompt":"a red fox","style":"cartoon"}' | head -c 300
   ```
   - Check: the reply starts with `{"created":` and has a `b64_json` image in it.

## If something fails

- Copy the log files from the data folder's `logs/` subfolder: `host.log` and `comfyui.log`.
- Take a screenshot of the install screen. The install steps aren't written to a log file.
- Note the step, the exact message on screen, and whether quitting and reopening the app (menu bar → **Settings…**) fixed it.
- Don't share the key. The smoke script hides it in its output, but check any log before you post it.

## Results

| Run | Date | Machine (OS, CPU, RAM, GPU/VRAM) | Install time | Install size | Model | First / second image | Smoke | Notes |
|---|---|---|---|---|---|---|---|---|
| A | | | | | — | | | |
| B | | | | | | | | |
| C | | | | | | | | |
| D | | | | — | — | — | — | |

When a run passes, tick its milestone in [ROADMAP.md](ROADMAP.md). The image times go into the recommendation
thresholds in `recommend.rs`.
