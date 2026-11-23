"""Turn a ComfyUI workflow template into a Glint Studio catalog model: workflow, mapping and model file.

    uv run --no-sync python -m tools.from_template flux_schnell --id flux-schnell    (from host/)

Reads the template and the node definitions from a running ComfyUI (--comfy, or COMFYUI_URL), so the
template list is whatever that ComfyUI ships. File sizes, hashes and the license come from Hugging Face
(set HF_TOKEN for gated repos). ComfyUI's own validator checks the converted workflow with the model files
left out, so nothing is downloaded. Review the model file before committing it:
the name, quality_rank, sizes and min_vram_gb are guesses."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from app.catalog import CatalogModel
from app.workflow import LOADERS, WorkflowMapping
from tools.hf_fill import hf_lookup, parse_url
from tools.inspect_workflow import _detect_mapping

CATALOG = Path(__file__).resolve().parents[2] / "catalog"
WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"}
MUTED, BYPASS = 2, 4
PRIMITIVES = {"PrimitiveInt", "PrimitiveFloat", "PrimitiveString", "PrimitiveStringMultiline", "PrimitiveBoolean"}
# Editor-only nodes, and PreviewAny, which only shows a value in the editor.
FRONTEND_ONLY = {"Note", "MarkdownNote", "Reroute", "PrimitiveNode", "PreviewAny"}
SUBGRAPH_IN = -10
MODEL_EXT = (".safetensors", ".gguf", ".ckpt", ".pt", ".pth", ".bin")
COMMERCIAL_OK = {"apache-2.0", "mit", "openrail", "openrail++", "creativeml-openrail-m", "bsd-3-clause"}


class TemplateError(ValueError):
    """The template can't become a catalog model; the message says why."""


# --- UI format → API format -------------------------------------------------------------------------


def _widget_names(spec: dict[str, Any]) -> list[tuple[str, bool, Any]]:
    """The node's widgets in the order the frontend stores their values: (name, has a control value, default)."""
    inputs = spec.get("input", {})
    order = spec.get("input_order") or {section: list(inputs.get(section, {})) for section in ("required", "optional")}
    names = []
    for section in ("required", "optional"):
        for name in order.get(section, []):
            kind, opts = ([*inputs[section][name], {}])[:2]
            opts = opts or {}
            if kind in ("COMFY_MATCHTYPE_V3", "COMFY_AUTOGROW_V3"):
                continue  # sockets, filled by links
            if isinstance(kind, str) and kind.startswith("COMFY_") and not opts.get("forceInput"):
                raise TemplateError(f"its {name} widget ({kind}) is a kind the importer can't read yet.")
            if (isinstance(kind, list) or kind in WIDGET_TYPES) and not opts.get("forceInput"):
                names.append((name, bool(opts.get("control_after_generate")), opts.get("default")))
                if opts.get("image_upload"):
                    names.append(("", False, None))  # the upload button keeps a value of its own
    return names


def _widget_values(node: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    names = _widget_names(spec)
    values = node.get("widgets_values") or []
    if isinstance(values, dict):
        return {name: values[name] for name, _, _ in names if name in values}
    out, i = {}, 0
    for name, control, default in names:
        if i >= len(values):  # an older template: widgets added since take their defaults
            if name and default is not None:
                out[name] = default
            continue
        if name:
            out[name] = values[i]
        i += 2 if control else 1  # the frontend stores "randomize"/"fixed" right after a seed
    if i < len(values):
        raise TemplateError(
            f"it has {len(values)} widget values but this ComfyUI's node has fewer widgets; "
            "the template may need a newer ComfyUI."
        )
    return out


class _Graph:
    """The top-level graph, or one subgraph instance with the node ids of its nodes prefixed "<instance>:"."""

    def __init__(self, data: dict[str, Any], prefix: str = "", instance: dict | None = None, parent: _Graph | None = None):
        self.data, self.prefix, self.instance, self.parent = data, prefix, instance, parent
        self.nodes = {n["id"]: n for n in data.get("nodes", [])}
        self.links: dict[int, tuple[int, int, str | None]] = {}
        for link in data.get("links", []):
            if isinstance(link, dict):  # subgraphs store links as objects, the top level as lists
                self.links[link["id"]] = (link["origin_id"], link["origin_slot"], link.get("type"))
            else:
                self.links[link[0]] = (link[1], link[2], link[5] if len(link) > 5 else None)


def to_api(ui: dict[str, Any], object_info: dict[str, Any]) -> dict[str, Any]:
    """Convert a UI-format workflow (what the ComfyUI editor saves) into the API format /prompt runs.
    Flattens subgraphs, drops notes and muted nodes, routes around bypassed nodes and reroutes,
    and swaps SaveImage for PreviewImage so the host gets the image without filling ComfyUI's output folder."""
    subgraphs = {s["id"]: s for s in (ui.get("definitions") or {}).get("subgraphs", [])}
    api: dict[str, Any] = {}

    def resolve(graph: _Graph, link_id: int) -> Any:
        """What feeds this link: [node key, slot], a plain value, or None (keep the node's own widget value)."""
        origin, slot, kind = graph.links[link_id]
        if origin == SUBGRAPH_IN:
            sg_input = graph.data["inputs"][slot]
            outer = next((i for i in graph.instance.get("inputs", []) if i["name"] == sg_input["name"]), None)
            if outer and outer.get("link") is not None:
                return resolve(graph.parent, outer["link"])
            values = graph.instance.get("widgets_values") or []
            # The instance keeps one value per subgraph input when its widgets were edited; else none at all.
            if len(values) == len(graph.data["inputs"]) and sg_input.get("type") in WIDGET_TYPES:
                return values[slot]
            return None
        node = graph.nodes[origin]
        if node.get("mode") == MUTED:
            raise TemplateError(f"{node['type']} #{origin} is muted but another node uses its output.")
        if node["type"] == "Reroute" or node.get("mode") == BYPASS:
            fed = [i for i in node.get("inputs", []) if i.get("link") is not None]
            if node["type"] != "Reroute":
                fed = [i for i in fed if i.get("type") == kind]
            if not fed:
                raise TemplateError(f"{node['type']} #{origin} is bypassed and has no {kind} input to pass on.")
            return resolve(graph, fed[0]["link"])
        if node["type"] == "PrimitiveNode":
            return node["widgets_values"][0]
        if node["type"] in subgraphs:
            inner = _Graph(subgraphs[node["type"]], f"{graph.prefix}{origin}:", node, graph)
            return resolve(inner, inner.data["outputs"][slot]["linkIds"][0])
        return [f"{graph.prefix}{origin}", slot]

    def emit(graph: _Graph) -> None:
        for node in graph.data.get("nodes", []):
            kind = node["type"]
            if node.get("mode", 0) in (MUTED, BYPASS) or kind in FRONTEND_ONLY:
                continue
            if kind in subgraphs:
                emit(_Graph(subgraphs[kind], f"{graph.prefix}{node['id']}:", node, graph))
                continue
            if kind not in object_info:
                raise TemplateError(f"this ComfyUI has no {kind} node (a custom node, or a newer ComfyUI).")
            saver = kind.startswith("SaveImage")
            try:
                inputs = {} if saver else _widget_values(node, object_info[kind])
            except TemplateError as exc:
                raise TemplateError(f"{kind} #{graph.prefix}{node['id']}: {exc}") from None
            for inp in node.get("inputs", []):
                if inp.get("link") is not None and (src := resolve(graph, inp["link"])) is not None:
                    inputs[inp["name"]] = src
            if saver:
                kind, inputs = "PreviewImage", {"images": inputs["images"]}
            api[f"{graph.prefix}{node['id']}"] = {
                "class_type": kind,
                "inputs": inputs,
                "_meta": {"title": node.get("title") or kind},
            }

    emit(_Graph(ui))
    # Fold plain value nodes (PrimitiveInt etc.) into their users, so width, steps or the prompt sit on the
    # node that reads them and the mapping finds them there.
    consts = {k: n["inputs"]["value"] for k, n in api.items()
              if n["class_type"] in PRIMITIVES and not isinstance(n["inputs"].get("value", []), list)}
    for node in api.values():
        for name, value in node["inputs"].items():
            if isinstance(value, list) and value[0] in consts:
                node["inputs"][name] = consts[value[0]]
    return {k: n for k, n in api.items() if k not in consts}


# --- API workflow → catalog entry ---------------------------------------------------------------------


def _model_urls(ui: dict[str, Any]) -> dict[str, str]:
    """File name → download URL, from the "models" list templates attach to their loader nodes."""
    graphs = [ui, *(ui.get("definitions") or {}).get("subgraphs", [])]
    return {
        m["name"].rsplit("/", 1)[-1]: m["url"].split("?")[0]
        for g in graphs
        for n in g.get("nodes", [])
        for m in (n.get("properties") or {}).get("models") or []
        if m.get("name") and m.get("url")
    }


def model_files(api: dict[str, Any], urls: dict[str, str]) -> list[dict[str, Any]]:
    """One catalog file entry per model file the workflow loads; sha256 and bytes are left to fill."""
    files: dict[str, dict[str, Any]] = {}
    for key, node in api.items():
        folder, input_key = LOADERS.get(node["class_type"], ("", ""))
        for name, value in node["inputs"].items():
            if not (isinstance(value, str) and value.endswith(MODEL_EXT)):
                continue
            if name != input_key:
                raise TemplateError(f"{node['class_type']} #{key} loads {value}; the app can't swap that loader's file yet.")
            base = value.rsplit("/", 1)[-1]
            if base not in urls:
                raise TemplateError(f"the template gives no download URL for {base}.")
            files[f"{folder}/{base}"] = {"url": urls[base], "sha256": "", "bytes": 0, "dest": f"{folder}/{base}"}
    if not files:
        raise TemplateError("the workflow loads no model files.")
    return list(files.values())


def _round(value: float, step: int) -> int:
    return max(step, round(value / step) * step)


def _sizes(width: int, height: int, step: int) -> dict[str, list[int]]:
    """Named shapes at about the template's pixel count."""
    pixels = width * height
    out = {"square": [_round(math.sqrt(pixels), step)] * 2}
    for name, ratio in (("portrait", 2 / 3), ("landscape", 3 / 2), ("phone", 13 / 28)):
        w = _round(math.sqrt(pixels * ratio), step)
        out[name] = [w, _round(w / ratio, step)]
    return out


def _precision(file_name: str) -> str:
    found = re.search(r"(bf16|fp16|fp8|fp4|int8|int4|nvfp4|q\d(?:_k)?(?:_[ms])?)", file_name.lower())
    return found[1] if found else "default"


def _license(hf: dict[str, Any], repo: str) -> dict[str, Any]:
    card = hf.get("cardData") or {}
    name = card.get("license_name") if card.get("license") == "other" else card.get("license")
    name = str(name or "unknown")
    return {"name": name, "url": f"https://huggingface.co/{repo}", "commercial_ok": name.lower() in COMMERCIAL_OK}


def build_entry(
    *,
    model_id: str,
    name: str,
    family: str,
    api: dict[str, Any],
    mapping: dict[str, Any],
    files: list[dict[str, Any]],
    hf: dict[str, dict[str, Any]],
    object_info: dict[str, Any],
) -> dict[str, Any]:
    """The catalog model JSON. `files` must already carry bytes and sha256; `hf` is repo → model info."""
    for key in ("positive_prompt", "width", "height", "output_node_id"):
        if key not in mapping:
            raise TemplateError(f"couldn't find the {key.replace('_', ' ')} in the workflow.")

    def value(field: str) -> Any:
        target = mapping.get(field)
        return api[target["node_id"]]["inputs"].get(target["input_key"]) if target else None

    width, height, steps, cfg = value("width"), value("height"), value("steps"), value("cfg")
    latent = object_info[api[mapping["width"]["node_id"]]["class_type"]]
    step = int(({**latent["input"].get("required", {}), **latent["input"].get("optional", {})}["width"][1] or {}).get("step", 8))
    step = max(step, 8)
    sizes = _sizes(width, height, step)

    weights = [f for f in files if f["dest"].startswith(("diffusion_models/", "checkpoints/"))] or files
    main = max(weights, key=lambda f: f["bytes"])
    repo = parse_url(main["url"])[0]
    limits: dict[str, Any] = {
        "sizes": sizes,
        "custom_size": True,
        "multiple_of": step,
        "max_pixels": 2 * width * height,
        "n_max": 1,
        "supports_negative": bool(mapping.get("negative_prompt")) and (cfg or 1) > 1,
        "supports_seed": "seed" in mapping,
    }
    quality: dict[str, Any] = {}
    if steps:
        limits["steps"] = [1, min(100, max(20, 4 * steps))]
        top = limits["steps"][1]
        quality = {
            "fast": {"steps": max(1, round(steps * 0.6))},
            "standard": {"steps": steps},
            "best": {"steps": min(top, round(steps * 1.5))},
        }
    if cfg is not None:
        limits["cfg"] = [min(1.0, float(cfg)), max(2.0, 2 * float(cfg))]

    return {
        "id": model_id,
        "name": name,
        "family": family,
        "provider": "comfyui",
        "license": _license(hf.get(repo, {}), repo),
        "quality_rank": 50,
        "workflow": model_id,
        "requires_hf_token": any(info.get("gated") for info in hf.values()),
        "variants": [
            {
                "precision": _precision(PurePosixPath(main["dest"]).name),
                "min_vram_gb": math.ceil(main["bytes"] / 1e9),
                "files": files,
            }
        ],
        "defaults": {"shape": "square", **({"steps": steps} if steps else {}), **({"cfg": cfg} if cfg is not None else {})},
        "quality": quality,
        "limits": limits,
    }


# --- Check with ComfyUI, no download --------------------------------------------------------------------

CHECK_FILE = "glint-check-not-a-model.safetensors"


def check_with_comfy(comfy: httpx.Client, api: dict[str, Any]) -> list[str]:
    """ComfyUI's own /prompt validation, minus the model files. They are renamed to a file that can't exist,
    so nothing ever queues and nothing has to be downloaded; any other complaint is a real problem."""
    probe = json.loads(json.dumps(api))
    for node in probe.values():
        for name, value in node["inputs"].items():
            if isinstance(value, str) and value.endswith(MODEL_EXT):
                node["inputs"][name] = CHECK_FILE
    resp = comfy.post("/prompt", json={"prompt": probe, "client_id": "glint-import-check"})
    if resp.status_code == 200:  # can't happen while a loader gets CHECK_FILE; never leave a run queued
        comfy.post("/queue", json={"delete": [resp.json().get("prompt_id")]})
        return []
    return _check_problems(resp.json())


def _check_problems(body: dict[str, Any]) -> list[str]:
    node_errors = body.get("node_errors") or {}
    problems = [
        f"{err.get('class_type')} #{node_id}: {e.get('message')} ({e.get('details')})"
        for node_id, err in node_errors.items()
        for e in err.get("errors", [])
        if not (e.get("type") == "value_not_in_list" and (e.get("extra_info") or {}).get("received_value") == CHECK_FILE)
    ]
    if not node_errors and body.get("error"):
        problems.append(str(body["error"].get("message") or body["error"]))
    return problems


# --- CLI -----------------------------------------------------------------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("template", help="Template name, as in ComfyUI's template browser URL (e.g. flux_schnell).")
    parser.add_argument("--id", help="Catalog id (default: from the template name).")
    parser.add_argument("--name", help="Display name (default: the template title).")
    parser.add_argument("--comfy", default=os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188"))
    parser.add_argument("--catalog", type=Path, default=CATALOG)
    parser.add_argument("--force", action="store_true", help="Overwrite existing catalog files.")
    args = parser.parse_args(argv)

    model_id = args.id or _slug(args.template)
    out = {
        "workflow": args.catalog / "workflows" / f"{model_id}.json",
        "mapping": args.catalog / "workflows" / f"{model_id}.mapping.json",
        "model": args.catalog / "models" / f"{model_id}.json",
    }
    if not args.force and (taken := [p for p in out.values() if p.exists()]):
        print(f"Error: {taken[0]} exists; pass --force to overwrite.", file=sys.stderr)
        return 1

    token = os.environ.get("HF_TOKEN")
    try:
        with httpx.Client(base_url=args.comfy, timeout=60, follow_redirects=True) as comfy:
            ui = comfy.get(f"/templates/{args.template}.json").raise_for_status().json()
            index = comfy.get("/templates/index.json").raise_for_status().json()
            object_info = comfy.get("/object_info").raise_for_status().json()
            api = to_api(ui, object_info)
            if problems := check_with_comfy(comfy, api):
                raise TemplateError("ComfyUI rejects the converted workflow:\n  " + "\n  ".join(problems))
        meta = next((t for c in index for t in c.get("templates", []) if t.get("name") == args.template), {})

        mapping = _detect_mapping(api)
        files = model_files(api, _model_urls(ui))
        with httpx.Client(headers={"Authorization": f"Bearer {token}"} if token else {}, timeout=30, follow_redirects=True) as hf:
            lookup = hf_lookup(hf)
            for f in files:
                f["bytes"], f["sha256"] = lookup(f["url"])
            repos = {parse_url(f["url"])[0] for f in files}
            info = {repo: hf.get(f"https://huggingface.co/api/models/{repo}").raise_for_status().json() for repo in repos}
        entry = build_entry(
            model_id=model_id,
            name=args.name or meta.get("title") or args.template,
            family=_slug((meta.get("models") or [model_id])[0]),
            api=api,
            mapping=mapping,
            files=files,
            hf=info,
            object_info=object_info,
        )
        CatalogModel.model_validate(entry)
        WorkflowMapping.model_validate(mapping)
    except (TemplateError, httpx.HTTPError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    for key, data in (("workflow", api), ("mapping", mapping), ("model", entry)):
        out[key].write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Wrote {out[key]}")
    total = sum(f["bytes"] for f in files) / 1e9
    print(
        f"\n{entry['name']}: {len(files)} files, {total:.1f} GB, license {entry['license']['name']}"
        f" (commercial use: {'yes' if entry['license']['commercial_ok'] else 'no'}).\n"
        "Check the name, quality_rank, min_vram_gb and sizes by hand before committing."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
