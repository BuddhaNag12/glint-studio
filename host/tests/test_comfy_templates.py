"""More from ComfyUI: a template in the editor's format becomes a model the host can run."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from app.comfy_templates import ComfyTemplates, categories
from app.workflow import load_workflow_bundle

HF = "https://huggingface.co/org/repo/resolve/main/split_files"


def _spec(required: dict, optional: dict | None = None) -> dict:
    optional = optional or {}
    return {"input": {"required": required, "optional": optional}, "input_order": {"required": list(required), "optional": list(optional)}}


OBJECT_INFO = {
    "UNETLoader": _spec({"unet_name": [["m.safetensors"], {}], "weight_dtype": [["default"], {}]}),
    "CLIPLoader": _spec({"clip_name": [["te.safetensors"], {}], "type": [["lumina2"], {}]}, {"device": [["default"], {}]}),
    "VAELoader": _spec({"vae_name": [["vae.safetensors"], {}]}),
    "PrimitiveStringMultiline": _spec({"value": ["STRING", {"multiline": True}]}),
    "CLIPTextEncode": _spec({"text": ["STRING", {}], "clip": ["CLIP", {}]}),
    "ConditioningZeroOut": _spec({"conditioning": ["CONDITIONING", {}]}),
    "EmptySD3LatentImage": _spec({"width": ["INT", {}], "height": ["INT", {}], "batch_size": ["INT", {}]}),
    "KSampler": _spec(
        {
            "model": ["MODEL", {}],
            "seed": ["INT", {"control_after_generate": True}],
            "steps": ["INT", {}],
            "cfg": ["FLOAT", {}],
            "sampler_name": [["euler"], {}],
            "scheduler": [["simple"], {}],
            "positive": ["CONDITIONING", {}],
            "negative": ["CONDITIONING", {}],
            "latent_image": ["LATENT", {}],
            "denoise": ["FLOAT", {"default": 1.0}],
        }
    ),
    "VAEDecode": _spec({"samples": ["LATENT", {}], "vae": ["VAE", {}]}),
    "SaveImage": _spec({"images": ["IMAGE", {}], "filename_prefix": ["STRING", {}]}),
}


def _link(i, a, a_slot, b, b_slot):
    return {"id": i, "origin_id": a, "origin_slot": a_slot, "target_id": b, "target_slot": b_slot, "type": "*"}


def _file(name: str, folder: str) -> dict:
    return {"properties": {"models": [{"name": name, "url": f"{HF}/{folder}/{name}", "directory": folder}]}}


# The editor's format: a note and a SaveImage at the top, the rest inside a subgraph (node 5). The prompt sits in
# a separate text node, the negative is the positive zeroed out, and KSampler's seed carries "randomize" after it.
TEMPLATE = {
    "nodes": [
        {"id": 9, "type": "SaveImage", "inputs": [{"name": "images", "link": 1}], "widgets_values": ["out"]},
        {"id": 5, "type": "sg", "inputs": [], "widgets_values": []},
        {"id": 7, "type": "MarkdownNote", "widgets_values": ["read me"]},
    ],
    "links": [[1, 5, 0, 9, 0, "IMAGE"]],
    "definitions": {
        "subgraphs": [
            {
                "id": "sg",
                "inputs": [],
                "outputs": [{"name": "IMAGE", "linkIds": [18]}],
                "nodes": [
                    {"id": 1, "type": "UNETLoader", "widgets_values": ["m.safetensors", "default"], **_file("m.safetensors", "diffusion_models")},
                    {"id": 2, "type": "CLIPLoader", "widgets_values": ["te.safetensors", "lumina2", "default"], **_file("te.safetensors", "clip")},
                    {"id": 3, "type": "VAELoader", "widgets_values": ["vae.safetensors"], **_file("vae.safetensors", "vae")},
                    {"id": 4, "type": "PrimitiveStringMultiline", "widgets_values": ["a cat"]},
                    {"id": 10, "type": "CLIPTextEncode", "inputs": [{"name": "clip", "link": 11}, {"name": "text", "link": 12}], "widgets_values": [""]},
                    {"id": 11, "type": "ConditioningZeroOut", "inputs": [{"name": "conditioning", "link": 13}]},
                    {"id": 12, "type": "EmptySD3LatentImage", "widgets_values": [1024, 768, 1]},
                    {
                        "id": 13,
                        "type": "KSampler",
                        "inputs": [
                            {"name": "model", "link": 14},
                            {"name": "positive", "link": 15},
                            {"name": "negative", "link": 16},
                            {"name": "latent_image", "link": 17},
                        ],
                        "widgets_values": [42, "randomize", 8, 1, "euler", "simple"],  # older than denoise
                    },
                    {"id": 14, "type": "VAEDecode", "inputs": [{"name": "samples", "link": 19}, {"name": "vae", "link": 20}]},
                ],
                "links": [
                    _link(11, 2, 0, 10, 0),
                    _link(12, 4, 0, 10, 1),
                    _link(13, 10, 0, 11, 0),
                    _link(14, 1, 0, 13, 0),
                    _link(15, 10, 0, 13, 1),
                    _link(16, 11, 0, 13, 2),
                    _link(17, 12, 0, 13, 3),
                    _link(19, 13, 0, 14, 0),
                    _link(20, 3, 0, 14, 1),
                    _link(18, 14, 0, -20, 0),
                ],
            }
        ]
    },
}

INDEX = [
    {
        "templates": [
            {
                "name": "tiny_t2i",
                "title": "Tiny",
                "mediaType": "image",
                "tags": ["Anime", "Text to Image"],
                "description": "Clean text rendering in 1.5 seconds. Marketing copy.",
                "models": ["Tiny"],
                "usage": 5,
            },
            {"name": "api_paid", "title": "Paid", "mediaType": "image", "tags": ["Text to Image"]},
            {"name": "tiny_edit", "title": "Edit", "mediaType": "image", "tags": ["Image Edit"]},
        ]
    }
]
SIZES = {"m.safetensors": 6_000_000_000, "te.safetensors": 8_000_000_000, "vae.safetensors": 300_000_000}


def _serve(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if request.url.host == "huggingface.co":
        folder = path.rsplit("/", 1)[1]
        return httpx.Response(
            200,
            json=[
                {"path": f"split_files/{folder}/{n}", "lfs": {"size": b, "oid": f"{i}" * 64}}
                for i, (n, b) in enumerate(SIZES.items())
            ],
        )
    body = {"/templates/index.json": INDEX, "/object_info": OBJECT_INFO, "/templates/tiny_t2i.json": TEMPLATE}.get(path)
    return httpx.Response(200, json=body) if body is not None else httpx.Response(404)


def test_a_template_becomes_a_model_with_its_files_sized(tmp_path: Path) -> None:
    engine_file = tmp_path / "engine.json"
    engine_file.write_text("{}")
    models: dict = {}

    async def scan() -> ComfyTemplates:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_serve)) as client:
            templates = ComfyTemplates("http://comfy.test", engine_file, models, client)
            assert await templates.scan() == ["comfy-tiny_t2i"]  # the API and edit templates are left out
            return templates

    templates = asyncio.run(scan())
    model = models["comfy-tiny_t2i"]
    assert model.name == "Tiny" and model.license is None and model.quality_rank == 5
    assert model.description == "Clean text rendering in 1.5 seconds." and model.categories == ["art", "text"]
    assert [f.dest for f in model.variants[0].files] == ["diffusion_models/m.safetensors", "text_encoders/te.safetensors", "vae/vae.safetensors"]
    assert model.variants[0].min_vram_gb == 8.8  # the largest file and a tenth more
    assert model.defaults.steps == 8 and model.defaults.cfg == 1.0
    assert model.limits.supports_negative is False and model.limits.supports_seed is True  # the negative is zeroed out

    workflow, mapping = load_workflow_bundle(templates.workflows_dir, model.workflow)
    assert sorted(workflow) == ["5:1", "5:10", "5:11", "5:12", "5:13", "5:14", "5:2", "5:3", "5:4", "9"]  # no note
    assert workflow["9"]["inputs"]["images"] == ["5:14", 0]  # through the subgraph's output
    assert workflow["5:13"]["inputs"] == {
        "model": ["5:1", 0],
        "seed": 42,
        "steps": 8,
        "cfg": 1,
        "sampler_name": "euler",
        "scheduler": "simple",
        "denoise": 1.0,  # newer than the template: its default
        "positive": ["5:10", 0],
        "negative": ["5:11", 0],
        "latent_image": ["5:12", 0],
    }
    assert mapping.positive_prompt.node_id == "5:4" and mapping.positive_prompt.input_key == "value"
    assert mapping.width.node_id == "5:12" and mapping.seed.input_key == "seed"
    assert json.loads((engine_file.parent / "comfy-templates" / "files.json").read_text())  # no lookup next start

    # The next start has it before ComfyUI is up; a model the app ships itself wins.
    later: dict = {}
    assert ComfyTemplates("http://comfy.test", engine_file, later, httpx.AsyncClient()).ids == {"comfy-tiny_t2i"}
    assert later["comfy-tiny_t2i"] == model
    builtin = {"comfy-tiny_t2i": "built-in"}
    assert ComfyTemplates("http://comfy.test", engine_file, builtin, httpx.AsyncClient()).ids == set()


def test_categories_come_from_the_description_and_tags() -> None:
    assert categories({"description": "A non-photorealistic image.", "tags": ["Anime"]}) == ["art"]
    assert categories({"description": "Photoreal people, realistic light."}) == ["photo"]
    assert categories({"description": "Fast images.", "tags": ["Text to Image"]}) == []
