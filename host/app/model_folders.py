"""Model folders from a ComfyUI the user already has, linked so nothing downloads twice.

The host keeps engine.json ({"preload", "folders"}) and writes extra_model_paths.yaml beside it;
the desktop app passes the yaml to ComfyUI and restarts it when the yaml changes.
"""

from __future__ import annotations

import json
import os
import platform
import re
from collections.abc import Mapping
from pathlib import Path

# The catalog's folder names, and the older names ComfyUI also reads them from.
ALIASES = {
    "checkpoints": ["checkpoints"],
    "diffusion_models": ["diffusion_models", "unet"],
    "text_encoders": ["text_encoders", "clip"],
    "vae": ["vae"],
    "loras": ["loras"],
}
EXTRA = ["clip_vision", "controlnet", "embeddings", "upscale_models"]  # linked for users' own workflows
MODEL_SUFFIXES = {".safetensors", ".ckpt", ".pt", ".pth", ".gguf", ".bin", ".sft"}


def read_settings(engine_file: Path | None) -> dict:
    """Preload defaults to on when the app runs the host, off when it runs on its own."""
    data: dict = {}
    if engine_file:
        try:
            loaded = json.loads(engine_file.read_text())
            data = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            pass
    folders = [f for f in data.get("folders", []) if isinstance(f, str)]
    return {"preload": bool(data.get("preload", engine_file is not None)), "folders": folders}


def model_roots(models_dir: Path | None, engine_file: Path | None) -> list[Path]:
    """Where ComfyUI finds models: the app's models folder, then the linked ones."""
    return [models_dir or Path("models"), *(Path(f) for f in read_settings(engine_file)["folders"])]


def save_settings(engine_file: Path, preload: bool, folders: list[str]) -> None:
    """engine.json, then the yaml (removed when no folders are linked). Both written whole."""
    _write(engine_file, json.dumps({"preload": preload, "folders": folders}))
    yaml_file = engine_file.with_name("extra_model_paths.yaml")
    if folders:
        _write(yaml_file, extra_model_paths(folders))
    else:
        yaml_file.unlink(missing_ok=True)


def _write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)  # the app never reads half a file


def _quote(path: str) -> str:
    # Single quotes: backslashes in Windows paths stay as they are; a quote is doubled.
    return "'" + path.replace("'", "''") + "'"


def extra_model_paths(folders: list[str]) -> str:
    lines = ["# Written by Glint Studio: model folders linked from another ComfyUI."]
    for i, folder in enumerate(folders, 1):
        lines += [f"glint_{i}:", f"  base_path: {_quote(folder)}"]
        for category, names in ALIASES.items():
            if len(names) == 1:
                lines.append(f"  {category}: {names[0]}")
            else:
                lines += [f"  {category}: |", *(f"    {n}" for n in names)]
        lines += [f"  {name}: {name}" for name in EXTRA]
    return "\n".join(lines) + "\n"


def normalize(path: str) -> Path | None:
    """A ComfyUI folder or its models folder, as the models folder; None if it isn't a folder."""
    p = Path(path.strip().strip('"')).expanduser()
    if (p / "models").is_dir():
        p = p / "models"
    return p.resolve() if p.is_dir() else None


def count_models(folder: Path) -> int:
    """Model files one level inside the category folders, which is where ComfyUI lists them."""
    n = 0
    try:
        for sub in folder.iterdir():
            if sub.is_dir():
                n += sum(1 for f in sub.iterdir() if f.is_file() and f.suffix.lower() in MODEL_SUFFIXES)
    except OSError:
        pass
    return n


def _base_paths(text: str) -> list[str]:
    # base_path lines of a ComfyUI yaml, without a yaml parser.
    return [m.strip().strip("'\"") for m in re.findall(r"^\s*base_path:\s*(.+?)\s*$", text, re.M)]


def candidates(home: Path | None = None, env: Mapping[str, str] | None = None, system: str | None = None) -> list[str]:
    """Where ComfyUI Desktop, the portable build and a git checkout keep models, on this OS."""
    home = home or Path.home()
    env = os.environ if env is None else env
    system = system or platform.system()
    paths: list[Path] = [home / "Documents" / "ComfyUI", home / "ComfyUI"]
    config_dirs: list[Path] = []
    if system == "Windows":
        if env.get("APPDATA"):
            config_dirs.append(Path(env["APPDATA"]) / "ComfyUI")
        for root in [home / "Desktop", home / "Downloads", home / "Documents", home]:
            paths.append(root / "ComfyUI_windows_portable" / "ComfyUI")
        for drive in "CDEF":
            paths += [Path(f"{drive}:/ComfyUI_windows_portable/ComfyUI"), Path(f"{drive}:/ComfyUI")]
    elif system == "Darwin":
        config_dirs.append(home / "Library" / "Application Support" / "ComfyUI")
    else:
        config_dirs.append(Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "ComfyUI")
    # ComfyUI Desktop: config.json names the base folder, and its yaml may list more.
    for d in config_dirs:
        try:
            base = json.loads((d / "config.json").read_text()).get("basePath")
            if isinstance(base, str):
                paths.append(Path(base))
        except (OSError, ValueError, AttributeError):
            pass
        try:
            paths += [Path(p) for p in _base_paths((d / "extra_models_config.yaml").read_text())]
        except OSError:
            pass
    found: list[str] = []
    for p in paths:
        try:
            folder = normalize(str(p))
        except OSError:
            folder = None
        if folder and str(folder) not in found:
            found.append(str(folder))
    return found


def file_present(roots: list[Path], dest: str, size: int) -> bool:
    """`dest` is "<category>/<name>"; a file of that name and size in any root's category folder."""
    category, name = dest.split("/", 1)
    return any(
        (p := root / folder / name).is_file() and p.stat().st_size == size
        for root in roots
        for folder in ALIASES.get(category, [category])
    )
