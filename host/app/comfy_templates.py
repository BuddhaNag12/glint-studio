"""More from ComfyUI: the text-to-image workflow templates that come with the installed ComfyUI.

Discover lists them below the app's own models. Nobody maintains them for this app, so they are marked as not
tested here. Each template is read from the running ComfyUI (/templates and /object_info), turned from the
editor's format into the API format the host runs, and its model files are looked up on Hugging Face for size
and sha256 (no download). A template that can't be turned into a model (an input image, an unknown file folder,
a prompt that can't be found) is left out.

shortcut: steps, cfg and size reach the first sampler and latent only; two-pass templates keep their own second pass.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import platform
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
from pydantic import ValidationError

from app import net
from app.catalog import CatalogModel
from app.errors import ApiError
from app.workflow import LOADERS

logger = logging.getLogger(__name__)

GB = 1e9
PREFIX = "comfy-"
SUBGRAPH_IN, SUBGRAPH_OUT = -10, -20
MUTED, BYPASSED = 2, 4
WIDGET_TYPES = {"COMBO", "INT", "FLOAT", "STRING", "BOOLEAN"}
PROMPT_KEYS = ("text", "prompt", "t5xxl", "clip_l")
FOLDERS = {"clip": "text_encoders", "unet": "diffusion_models"}  # ComfyUI's old folder names
DESTS = {"checkpoints", "diffusion_models", "text_encoders", "vae", "loras"}
LOOKUPS = 8
PRECISIONS = ("fp8", "bf16", "fp16", "int8", "fp4")  # Hugging Face folder listings at once
APPLE_SILICON = sys.platform == "darwin" and platform.machine() == "arm64"


class Skip(Exception):
    """This template can't be offered; the message says why (logged, not shown)."""


def wanted(entry: dict) -> bool:
    """Local text-to-image templates: no paid API nodes, no input image."""
    return (
        entry.get("mediaType") == "image"
        and entry.get("openSource") is not False
        and not str(entry.get("name", "")).startswith("api_")
        and not (entry.get("io") or {}).get("inputs")
        and "Text to Image" in (entry.get("tags") or [])
    )


# --- The editor's format to the API format -------------------------------------------------------------------


def _is_link(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 2 and isinstance(value[0], str)


def _widgets(spec_class: dict) -> list[tuple[str, bool]]:
    """(input name, has a control_after_generate value after it) for each widget, in widgets_values order."""
    out = []
    for section in ("required", "optional"):
        specs = (spec_class.get("input") or {}).get(section) or {}
        for name in (spec_class.get("input_order") or {}).get(section) or list(specs):
            spec = specs.get(name) or [None]
            opts = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            if (isinstance(spec[0], list) or spec[0] in WIDGET_TYPES) and not opts.get("forceInput"):
                out.append((name, bool(opts.get("control_after_generate"))))
    return out


class _Scope:
    """One graph: the root, or a subgraph as placed by one instance node (`owner`) in its parent scope."""

    def __init__(self, graph: dict, prefix: str, parent: _Scope | None = None, owner: dict | None = None):
        self.graph, self.prefix, self.parent, self.owner = graph, prefix, parent, owner
        self.nodes = {n["id"]: n for n in graph.get("nodes") or []}
        self.links = {}
        for link in graph.get("links") or []:
            if isinstance(link, list):  # the root keeps links as [id, origin, slot, target, slot, type]
                link = dict(zip(("id", "origin_id", "origin_slot", "target_id", "target_slot", "type"), link))
            self.links[link["id"]] = link


def to_api(ui: dict, object_info: dict) -> dict[str, dict]:
    """Flatten subgraphs and turn editor nodes into API nodes; notes, reroutes and muted nodes drop out."""
    subgraphs = {s["id"]: s for s in (ui.get("definitions") or {}).get("subgraphs") or []}
    api: dict[str, dict] = {}

    def resolve(scope: _Scope, link_id: Any, depth: int = 0) -> Any:
        """What feeds a link: [api id, slot], or a literal value, or None (nothing connected)."""
        link = scope.links.get(link_id)
        if link is None or depth > 50:
            return None
        origin, slot = link["origin_id"], link["origin_slot"]
        if origin == SUBGRAPH_IN:  # an input of this subgraph: whatever the instance gets there
            name = scope.graph["inputs"][slot]["name"]
            outer = next((i for i in scope.owner.get("inputs") or [] if i["name"] == name), None)
            return resolve(scope.parent, outer["link"], depth + 1) if outer and outer.get("link") is not None else None
        node = scope.nodes.get(origin)
        if node is None or node.get("mode") == MUTED:
            return None
        if node["type"] in subgraphs:  # an output of a nested subgraph: follow it inside
            inner = _Scope(subgraphs[node["type"]], f"{scope.prefix}{node['id']}:", scope, node)
            ids = inner.graph["outputs"][slot].get("linkIds") or []
            return resolve(inner, ids[0], depth + 1) if ids else None
        if node["type"] == "Reroute" or node.get("mode") == BYPASSED:
            ins = [i for i in node.get("inputs") or [] if i.get("link") is not None]
            if node["type"] != "Reroute":  # a bypassed node passes on its input of the same type
                out_type = (node.get("outputs") or [{}])[slot].get("type")
                ins = [i for i in ins if i.get("type") == out_type]
            return resolve(scope, ins[0]["link"], depth + 1) if ins else None
        if node["type"] == "PrimitiveNode":  # an editor-only value box
            return ("value", (node.get("widgets_values") or [None])[0])
        return [f"{scope.prefix}{origin}", slot]

    def add(scope: _Scope) -> None:
        for node in scope.nodes.values():
            if node.get("mode") in (MUTED, BYPASSED):
                continue
            if node["type"] in subgraphs:
                add(_Scope(subgraphs[node["type"]], f"{scope.prefix}{node['id']}:", scope, node))
                continue
            spec = object_info.get(node["type"])
            if spec is None:  # notes, reroutes, value boxes: editor-only
                continue
            inputs: dict[str, Any] = {}
            values = node.get("widgets_values")
            if isinstance(values, dict):
                inputs.update(values)
            elif isinstance(values, list):
                i = 0
                for name, control in _widgets(spec):
                    if i >= len(values):
                        break
                    inputs[name] = values[i]
                    i += 2 if control else 1
            for inp in node.get("inputs") or []:
                if inp.get("link") is None:
                    continue
                source = resolve(scope, inp["link"])
                if isinstance(source, tuple):
                    inputs[inp["name"]] = source[1]
                elif source is not None:
                    inputs[inp["name"]] = source
            for name, s in ((spec.get("input") or {}).get("required") or {}).items():
                if name not in inputs and len(s) > 1 and isinstance(s[1], dict) and "default" in s[1]:
                    inputs[name] = s[1]["default"]  # an input newer than the template
            if node["type"] == "SaveImageAdvanced":  # same images, but format options the host doesn't read
                node_type, inputs = "SaveImage", {k: inputs[k] for k in ("filename_prefix", "images") if k in inputs}
            else:
                node_type = node["type"]
            api[f"{scope.prefix}{node['id']}"] = {"class_type": node_type, "inputs": inputs}

    add(_Scope(ui, ""))
    return api


def _upstream(api: dict, start: str) -> list[str]:
    """`start` and every node it reads from, nearest first."""
    order, queue = [], [start]
    while queue:
        node_id = queue.pop(0)
        if node_id in order or node_id not in api:
            continue
        order.append(node_id)
        queue += [v[0] for v in api[node_id]["inputs"].values() if _is_link(v)]
    return order


def _check(api: dict, object_info: dict) -> None:
    for node_id, node in api.items():
        required = (object_info[node["class_type"]].get("input") or {}).get("required") or {}
        missing = [k for k in required if k not in node["inputs"]]
        if missing:
            raise Skip(f"{node['class_type']} ({node_id}) is missing {', '.join(missing)}")
        for value in node["inputs"].values():
            if _is_link(value) and value[0] not in api:
                raise Skip(f"{node['class_type']} ({node_id}) reads from a node that isn't there")


def _prompt(api: dict, ref: Any, stop: set[str] = frozenset()) -> dict | None:
    """The text box a conditioning link comes from. None through ConditioningZeroOut: that's "no negative"."""
    for node_id in _upstream(api, ref[0]) if _is_link(ref) else []:
        node = api[node_id]
        if node["class_type"] == "ConditioningZeroOut" or node_id in stop:
            return None
        texts = [k for k in PROMPT_KEYS if isinstance(node["inputs"].get(k), str) or _text_box(api, node["inputs"].get(k))]
        if len(texts) > 1:
            raise Skip(f"{node['class_type']} takes the prompt in {len(texts)} boxes")
        if texts:
            return _text_box(api, node["inputs"][texts[0]]) or {"node_id": node_id, "input_key": texts[0]}
    return None


def _text_box(api: dict, ref: Any) -> dict | None:
    """A prompt kept in a separate text node (PrimitiveStringMultiline) and linked in."""
    source = api.get(ref[0]) if _is_link(ref) else None
    if source and source["class_type"].startswith("PrimitiveString") and isinstance(source["inputs"].get("value"), str):
        return {"node_id": ref[0], "input_key": "value"}
    return None


def _scalar(api: dict, node_id: str, key: str) -> dict | None:
    """Where the number for `key` lives: on the node, or on the value node linked to it."""
    value = api[node_id]["inputs"].get(key)
    if _is_link(value):
        source = api[value[0]]["inputs"]
        return {"node_id": value[0], "input_key": "value"} if "value" in source and not _is_link(source["value"]) else None
    return {"node_id": node_id, "input_key": key} if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _value(api: dict, target: dict | None) -> Any:
    return api[target["node_id"]]["inputs"][target["input_key"]] if target else None


def mapping(api: dict) -> dict:
    """Which inputs take the prompt, seed, steps, cfg and size. The output is the first SaveImage."""
    out = next((k for k, n in api.items() if n["class_type"] == "SaveImage"), None)
    if out is None:
        raise Skip("no SaveImage node")
    found: dict[str, Any] = {"output_node_id": out}
    for node_id in _upstream(api, out):
        inputs = api[node_id]["inputs"]
        if "positive_prompt" not in found:
            for key in ("positive", "conditioning"):
                if (target := _prompt(api, inputs.get(key))) is not None:
                    found["positive_prompt"] = target
                    negative = _prompt(api, inputs.get("negative"), stop={target["node_id"]})
                    if negative:
                        found["negative_prompt"] = negative
                    break
        for key, names in (("seed", ("seed", "noise_seed")), ("steps", ("steps",)), ("cfg", ("cfg",))):
            for name in names:
                if key not in found and (target := _scalar(api, node_id, name)):
                    found[key] = target
        if "width" not in found and "Latent" in api[node_id]["class_type"] and "width" in inputs:
            w, h = _scalar(api, node_id, "width"), _scalar(api, node_id, "height")
            above = _upstream(api, inputs["width"][0]) if _is_link(inputs["width"]) else []
            picker = next((api[k] for k in above if "megapixels" in api[k]["inputs"]), None)
            if not (w and h) and picker and isinstance(picker["inputs"]["megapixels"], (int, float)):
                # a size picker (aspect + megapixels) feeds the latent: start square at its pixel count instead
                side = round(math.sqrt(picker["inputs"]["megapixels"]) * 1024 / 16) * 16
                inputs["width"] = inputs["height"] = side
                w, h = {"node_id": node_id, "input_key": "width"}, {"node_id": node_id, "input_key": "height"}
            if w and h:
                found["width"], found["height"] = w, h
    if "positive_prompt" not in found:
        raise Skip("no prompt box found")
    if "width" not in found:
        raise Skip("no image size found")
    return found


# --- Files and the catalog entry -----------------------------------------------------------------------------


def _model_links(ui: dict) -> dict[str, dict]:
    """Every file the template links to (in node properties), by file name."""
    graphs = [ui, *((ui.get("definitions") or {}).get("subgraphs") or [])]
    nodes = [n for g in graphs for n in g.get("nodes") or []]
    return {m["name"]: m for n in nodes for m in (n.get("properties") or {}).get("models") or []}


def files_used(ui: dict, api: dict) -> list[dict]:
    """The linked files a remaining node loads, with their models/ subfolder as `dest`."""
    links = _model_links(ui)
    names = {v for n in api.values() for v in n["inputs"].values() if isinstance(v, str) and v in links}
    out = []
    for name in sorted(names):
        link = links[name]
        folder = FOLDERS.get(link.get("directory"), link.get("directory"))
        if folder not in DESTS:
            raise Skip(f"{name} goes in models/{folder}")
        if not str(link.get("url", "")).startswith("https://huggingface.co/"):
            raise Skip(f"{name} isn't on Hugging Face")
        out.append({"url": link["url"], "dest": f"{folder}/{name}", "sha256": (link.get("hash") or "").lower()})
    if not out:
        raise Skip("no model files")
    kinds = [LOADERS[n["class_type"]][0] for n in api.values() if n["class_type"] in LOADERS]
    if len(kinds) != len(set(kinds)):  # shortcut: use_model_files sets one file per folder; base + refiner pairs wait
        raise Skip("two loaders of one kind")
    if APPLE_SILICON and any("fp8" in f["dest"].lower() for f in out):
        raise Skip("fp8 files don't run on Apple Silicon")
    return out


def _hf_parts(url: str) -> tuple[str, str, str]:
    """https://huggingface.co/<owner>/<repo>/resolve/<rev>/<path> → (owner/repo, rev, path)."""
    segs = urlsplit(url).path.strip("/").split("/")
    if len(segs) < 5 or segs[2] != "resolve":
        raise Skip(f"not a Hugging Face file link: {url}")
    return f"{segs[0]}/{segs[1]}", segs[3], unquote("/".join(segs[4:]).split("?")[0])


async def file_info(client: httpx.AsyncClient, urls: set[str], cache: dict[str, list]) -> None:
    """Size and sha256 of each file, from one Hugging Face folder listing per folder; adds to `cache` by URL."""
    folders: dict[tuple[str, str, str], list[str]] = {}
    for url in urls - cache.keys():
        try:
            repo, rev, path = _hf_parts(url)
        except Skip:
            continue
        folders.setdefault((repo, rev, path.rpartition("/")[0]), []).append(url)
    gate = asyncio.Semaphore(LOOKUPS)

    async def listing(repo: str, rev: str, folder: str, wanted_urls: list[str]) -> None:
        async with gate:
            try:
                tree = f"https://huggingface.co/api/models/{repo}/tree/{rev}/{folder}".rstrip("/")
                res = await client.get(tree, timeout=30.0, follow_redirects=True)  # Comfy-Org renames its repos
                res.raise_for_status()
                entries = {e.get("path"): e for e in res.json()}
            except (httpx.HTTPError, ValueError) as exc:  # gated, gone or offline: those templates wait for the next start
                logger.info("No file sizes for %s/%s: %s", repo, folder, exc)
                return
        for url in wanted_urls:
            lfs = (entries.get(_hf_parts(url)[2]) or {}).get("lfs") or {}
            if lfs.get("size") and lfs.get("oid"):
                cache[url] = [lfs["size"], lfs["oid"]]

    await asyncio.gather(*(listing(*key, u) for key, u in folders.items()))


def _shape(width: int, height: int, rw: float, rh: float) -> list[int]:
    k = math.sqrt(width * height / (rw * rh))
    return [max(16, round(rw * k / 16) * 16), max(16, round(rh * k / 16) * 16)]


# What a template is good at, read from ComfyUI's own description and tags.
CATEGORIES = {
    "photo": re.compile(r"(?<!non-)\b(photoreal|realis[mt])", re.I),  # not "non-photorealistic"
    "art": re.compile(r"\banime\b|illustration|artistic", re.I),
    "text": re.compile(r"text rendering|render(s|ing)? text|text-heavy|typograph", re.I),
}


def categories(template: dict) -> list[str]:
    about = " ".join([template.get("description") or "", *(template.get("tags") or [])])
    return [name for name, words in CATEGORIES.items() if words.search(about)]


def entry(template: dict, api: dict, found: dict, files: list[dict], info: dict[str, list]) -> dict:
    """The catalog entry: one variant, the template's own size, steps and cfg as defaults."""
    for f in files:
        size, sha = info[f["url"]]
        f["bytes"], f["sha256"] = size, f["sha256"] or sha
    width, height = int(_value(api, found["width"])), int(_value(api, found["height"]))
    steps, cfg = _value(api, found.get("steps")), _value(api, found.get("cfg"))
    steps = int(steps) if steps else None
    cfg = float(cfg) if cfg is not None else None
    biggest = max(f["bytes"] for f in files)
    return {
        "id": template_id(template["name"]),
        "name": template.get("title") or template["name"],
        "family": ((template.get("models") or [""])[0] or "").lower(),
        "description": re.split(r"(?<=\.)\s", (template.get("description") or "").strip(), maxsplit=1)[0],
        "categories": categories(template),
        "quality_rank": int(template.get("usage") or 0),
        "workflow": template_id(template["name"]),
        "variants": [
            {
                "precision": next((p for p in PRECISIONS if any(p in f["dest"] for f in files)), "model"),
                "min_vram_gb": round(biggest / GB * 1.1, 1),  # the largest file plus room to work, like oMLX
                "files": files,
            }
        ],
        "defaults": {"shape": "square", "steps": steps, "cfg": cfg},
        "limits": {
            "sizes": {
                "square": _shape(width, height, 1, 1),
                "portrait": _shape(width, height, 3, 4),
                "landscape": _shape(width, height, 4, 3),
                "phone": _shape(width, height, 9, 19.5),
            },
            "custom_size": True,
            "multiple_of": 16,
            "max_pixels": width * height * 2,
            "steps": [1, max(50, steps or 0)] if found.get("steps") else None,
            "cfg": [min(1.0, cfg or 1.0), max(10.0, cfg or 0.0)] if found.get("cfg") else None,
            "supports_negative": "negative_prompt" in found,
            "supports_seed": "seed" in found,
        },
    }


def template_id(name: str) -> str:
    return PREFIX + re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")


# --- Reading them from the running ComfyUI -------------------------------------------------------------------


class ComfyTemplates:
    """Adds ComfyUI's templates to `models` (the app's shared dict) once ComfyUI answers; never replaces one."""

    def __init__(self, comfy_url: str, engine_file: Path | None, models: dict[str, CatalogModel], client: httpx.AsyncClient):
        self.comfy_url = comfy_url.rstrip("/")
        self.engine_file = engine_file
        self.dir = engine_file.parent / "comfy-templates" if engine_file else None
        self.models = models
        self.client = client
        self.ids: set[str] = set()
        self._load()  # the last scan's models, so Discover has them before ComfyUI is up

    def _load(self) -> None:
        try:
            raw = json.loads((self.dir / "models.json").read_text(encoding="utf-8")) if self.dir else []
            saved = [CatalogModel.model_validate(m) for m in raw]
        except (OSError, ValueError) as exc:  # missing or written by another version: the next scan replaces it
            logger.info("No saved ComfyUI templates: %s", exc)
            return
        saved = [m for m in saved if m.id not in self.models and (self.workflows_dir / f"{m.workflow}.json").is_file()]
        self.models.update({m.id: m for m in saved})
        self.ids = {m.id for m in saved}

    @property
    def workflows_dir(self) -> Path | None:
        return self.dir and self.dir / "workflows"

    def _taken(self) -> tuple[set[str], set[str]]:
        """Ids, and the main model files, of the models the app already has."""
        files = {
            f.dest
            for m in self.models.values()
            if m.id not in self.ids
            for v in m.variants
            for f in v.files
            if f.dest.startswith(("diffusion_models/", "checkpoints/"))
        }
        return set(self.models) - self.ids, files

    async def _get(self, path: str) -> Any:
        res = await self.client.get(f"{self.comfy_url}{path}", timeout=60.0)
        res.raise_for_status()
        return res.json()

    async def scan(self) -> list[str]:
        """Reads every wanted template; returns the ids added. Raises httpx.HTTPError while ComfyUI is down."""
        if self.dir is None:
            return []
        index = await self._get("/templates/index.json")
        object_info = await self._get("/object_info")
        taken_ids, taken_files = self._taken()
        built: list[tuple[dict, dict, dict, list[dict]]] = []
        for template in (t for cat in index for t in cat.get("templates") or [] if wanted(t)):
            try:
                ui = await self._get(f"/templates/{template['name']}.json")
                api = to_api(ui, object_info)
                found = mapping(api)
                keep = set(_upstream(api, found["output_node_id"]))
                api = {k: v for k, v in api.items() if k in keep}
                _check(api, object_info)
                files = files_used(ui, api)
            except Skip as exc:
                logger.info("ComfyUI template %s left out: %s", template.get("name"), exc)
                continue
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
                logger.info("ComfyUI template %s left out: %r", template.get("name"), exc)
                continue
            if template_id(template["name"]) in taken_ids or any(f["dest"] in taken_files for f in files):
                continue  # the app has this model already, tested
            built.append((template, api, found, files))

        cache_file = self.dir / "files.json"
        try:
            info = json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
        own = net.client(net.read(self.engine_file))  # the proxy and CA file from Settings → Downloads
        try:
            await file_info(own or self.client, {f["url"] for *_, files in built for f in files}, info)
        finally:
            if own:
                await own.aclose()

        added: dict[str, CatalogModel] = {}
        (self.dir / "workflows").mkdir(parents=True, exist_ok=True)
        for template, api, found, files in built:
            if not all(f["url"] in info for f in files):
                continue
            try:
                model = CatalogModel.model_validate(entry(template, api, found, files, info))
            except ValidationError as exc:
                logger.info("ComfyUI template %s left out: %s", template["name"], exc.errors()[0].get("msg"))
                continue
            (self.dir / "workflows" / f"{model.workflow}.json").write_text(json.dumps(api), encoding="utf-8")
            (self.dir / "workflows" / f"{model.workflow}.mapping.json").write_text(json.dumps(found), encoding="utf-8")
            added[model.id] = model
        cache_file.write_text(json.dumps(info), encoding="utf-8")
        saved = [m.model_dump(mode="json") for m in added.values()]
        (self.dir / "models.json").write_text(json.dumps(saved), encoding="utf-8")
        for model_id in self.ids - added.keys():
            self.models.pop(model_id, None)
        self.models.update(added)
        self.ids = set(added)
        return sorted(added)

    async def run(self, wait: float = 5.0) -> None:
        """Waits for ComfyUI, then scans once."""
        while True:
            try:
                added = await self.scan()
                logger.info("Added %d ComfyUI templates", len(added))
                return
            except (httpx.HTTPError, ValueError):
                await asyncio.sleep(wait)
            except ApiError as exc:  # broken proxy settings: Settings → Downloads says so already
                logger.warning("ComfyUI templates not read: %s", exc.message)
                return
