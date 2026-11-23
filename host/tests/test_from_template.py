import pytest

from app.catalog import CatalogModel
from tools.from_template import TemplateError, build_entry, model_files, to_api, _model_urls
from tools.inspect_workflow import _detect_mapping

MODEL_URL = "https://huggingface.co/org/repo/resolve/main/model-fp8.safetensors"

OBJECT_INFO = {
    "CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["model-fp8.safetensors"]]}}},
    "CLIPTextEncode": {"input": {"required": {"text": ["STRING", {"multiline": True}], "clip": ["CLIP"]}}},
    "EmptyLatentImage": {
        "input": {"required": {"width": ["INT", {"step": 16}], "height": ["INT", {"step": 16}], "batch_size": ["INT"]}}
    },
    "PrimitiveInt": {"input": {"required": {"value": ["INT"]}}},
    "KSampler": {
        "input": {
            "required": {
                "model": ["MODEL"],
                "seed": ["INT", {"control_after_generate": True}],
                "steps": ["INT"],
                "cfg": ["FLOAT"],
                "sampler_name": [["euler"]],
                "scheduler": [["simple"]],
                "positive": ["CONDITIONING"],
                "negative": ["CONDITIONING"],
                "latent_image": ["LATENT"],
                "denoise": ["FLOAT", {"default": 1.0}],
            }
        }
    },
    "LatentUpscale": {"input": {"required": {"samples": ["LATENT"], "scale": ["FLOAT"]}}},
    "VAEDecode": {"input": {"required": {"samples": ["LATENT"], "vae": ["VAE"]}}},
    "SaveImage": {"input": {"required": {"images": ["IMAGE"], "filename_prefix": ["STRING"]}}},
}

# Top level: a sampler subgraph (#10) fed by the loader, the prompt, a Reroute and a PrimitiveNode steps value;
# then a bypassed upscale, a decode and a save. A muted node and a note hang off to the side.
UI = {
    "nodes": [
        {"id": 1, "type": "CheckpointLoaderSimple", "widgets_values": ["model-fp8.safetensors"],
         "properties": {"models": [{"name": "model-fp8.safetensors", "url": MODEL_URL + "?download=true"}]}},
        {"id": 2, "type": "CLIPTextEncode", "widgets_values": ["a red fox"],
         "inputs": [{"name": "clip", "link": 2}]},
        {"id": 3, "type": "Reroute", "inputs": [{"name": "", "link": 3}]},
        {"id": 4, "type": "PrimitiveNode", "widgets_values": [6]},
        {"id": 10, "type": "sg-1", "widgets_values": [],
         "inputs": [{"name": "model", "link": 1}, {"name": "positive", "link": 4},
                    {"name": "clip", "link": 5}, {"name": "steps", "link": 6}]},
        {"id": 5, "type": "LatentUpscale", "mode": 4, "widgets_values": [1.5], "inputs": [{"name": "samples", "link": 7, "type": "LATENT"}]},
        {"id": 6, "type": "VAEDecode", "inputs": [{"name": "samples", "link": 8}, {"name": "vae", "link": 9}]},
        {"id": 7, "type": "SaveImage", "widgets_values": ["out"], "inputs": [{"name": "images", "link": 10}]},
        {"id": 8, "type": "CLIPTextEncode", "mode": 2, "widgets_values": ["unused"]},
        {"id": 9, "type": "Note", "widgets_values": ["read me"]},
    ],
    "links": [
        [1, 1, 0, 10, 0, "MODEL"], [2, 1, 1, 2, 0, "CLIP"], [3, 2, 0, 3, 0, "CONDITIONING"],
        [4, 3, 0, 10, 1, "CONDITIONING"], [5, 1, 1, 10, 2, "CLIP"], [6, 4, 0, 10, 3, "INT"],
        [7, 10, 0, 5, 0, "LATENT"], [8, 5, 0, 6, 0, "LATENT"], [9, 1, 2, 6, 1, "VAE"], [10, 6, 0, 7, 0, "IMAGE"],
    ],
    "definitions": {"subgraphs": [{
        "id": "sg-1",
        "inputs": [{"name": "model", "type": "MODEL"}, {"name": "positive", "type": "CONDITIONING"},
                   {"name": "clip", "type": "CLIP"}, {"name": "steps", "type": "INT"}],
        "outputs": [{"linkIds": [24]}],
        "nodes": [
            {"id": 20, "type": "PrimitiveInt", "widgets_values": [1024]},
            {"id": 21, "type": "EmptyLatentImage", "widgets_values": [512, 512, 1],
             "inputs": [{"name": "width", "link": 25}, {"name": "height", "link": 26}]},
            {"id": 22, "type": "CLIPTextEncode", "widgets_values": ["blurry"], "inputs": [{"name": "clip", "link": 27}]},
            {"id": 23, "type": "KSampler", "widgets_values": [42, "randomize", 20, 1, "euler", "simple"],
             "inputs": [{"name": "model", "link": 20}, {"name": "positive", "link": 21}, {"name": "negative", "link": 28},
                        {"name": "latent_image", "link": 22}, {"name": "steps", "link": 23}]},
        ],
        "links": [
            {"id": 20, "origin_id": -10, "origin_slot": 0, "target_id": 23, "target_slot": 0, "type": "MODEL"},
            {"id": 21, "origin_id": -10, "origin_slot": 1, "target_id": 23, "target_slot": 1, "type": "CONDITIONING"},
            {"id": 22, "origin_id": 21, "origin_slot": 0, "target_id": 23, "target_slot": 3, "type": "LATENT"},
            {"id": 23, "origin_id": -10, "origin_slot": 3, "target_id": 23, "target_slot": 4, "type": "INT"},
            {"id": 24, "origin_id": 23, "origin_slot": 0, "target_id": -20, "target_slot": 0, "type": "LATENT"},
            {"id": 25, "origin_id": 20, "origin_slot": 0, "target_id": 21, "target_slot": 0, "type": "INT"},
            {"id": 26, "origin_id": 20, "origin_slot": 0, "target_id": 21, "target_slot": 1, "type": "INT"},
            {"id": 27, "origin_id": -10, "origin_slot": 2, "target_id": 22, "target_slot": 0, "type": "CLIP"},
            {"id": 28, "origin_id": 22, "origin_slot": 0, "target_id": 23, "target_slot": 2, "type": "CONDITIONING"},
        ],
    }]},
}


def test_to_api_flattens_the_template():
    api = to_api(UI, OBJECT_INFO)
    assert set(api) == {"1", "2", "10:21", "10:22", "10:23", "6", "7"}  # no note, muted, reroute, primitive or bypassed node
    sampler = api["10:23"]["inputs"]
    assert sampler["seed"] == 42 and sampler["cfg"] == 1 and sampler["denoise"] == 1  # "randomize" skipped; denoise from its default
    assert sampler["steps"] == 6  # PrimitiveNode through the subgraph input
    assert sampler["model"] == ["1", 0] and sampler["positive"] == ["2", 0]  # through the Reroute
    assert sampler["latent_image"] == ["10:21", 0] and sampler["negative"] == ["10:22", 0]
    assert api["10:21"]["inputs"] == {"width": 1024, "height": 1024, "batch_size": 1}  # PrimitiveInt folded in
    assert api["6"]["inputs"]["samples"] == ["10:23", 0]  # around the bypassed upscale, out of the subgraph
    assert api["7"] == {"class_type": "PreviewImage", "inputs": {"images": ["6", 0]}, "_meta": {"title": "PreviewImage"}}


def test_entry_from_template_validates():
    api = to_api(UI, OBJECT_INFO)
    mapping = _detect_mapping(api)
    assert mapping["positive_prompt"]["node_id"] == "2" and mapping["negative_prompt"]["node_id"] == "10:22"
    assert mapping["width"]["node_id"] == "10:21" and mapping["output_node_id"] == "7"
    files = model_files(api, _model_urls(UI))
    assert files == [{"url": MODEL_URL, "sha256": "", "bytes": 0, "dest": "checkpoints/model-fp8.safetensors"}]
    files[0].update(sha256="a" * 64, bytes=5_500_000_000)
    hf = {"org/repo": {"cardData": {"license": "apache-2.0"}, "gated": False}}
    entry = build_entry(model_id="demo", name="Demo", family="demo", api=api, mapping=mapping,
                        files=files, hf=hf, object_info=OBJECT_INFO)
    model = CatalogModel.model_validate(entry)
    assert model.variants[0].precision == "fp8" and model.variants[0].min_vram_gb == 6
    assert entry["license"]["commercial_ok"] and entry["limits"]["multiple_of"] == 16
    assert entry["limits"]["sizes"]["square"] == [1024, 1024] and not entry["limits"]["supports_negative"]  # cfg 1


def test_zeroed_negative_is_not_mapped_to_the_positive():
    api = {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "a fox", "clip": ["9", 0]}},
        "2": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["1", 0]}},
        "3": {"class_type": "KSampler", "inputs": {"positive": ["1", 0], "negative": ["2", 0], "seed": 1}},
    }
    mapping = _detect_mapping(api)
    assert mapping["positive_prompt"]["node_id"] == "1" and "negative_prompt" not in mapping


def test_unreadable_templates_say_why():
    with pytest.raises(TemplateError, match="no Mystery node"):
        to_api({"nodes": [{"id": 1, "type": "Mystery"}], "links": []}, OBJECT_INFO)
    with pytest.raises(TemplateError, match="fewer widgets"):
        to_api({"nodes": [{"id": 1, "type": "PrimitiveInt", "widgets_values": [1, 2]}], "links": []}, OBJECT_INFO)


def test_comfy_check_ignores_only_the_missing_model_files():
    from tools.from_template import CHECK_FILE, _check_problems

    missing = {"type": "value_not_in_list", "message": "Value not in list", "extra_info": {"received_value": CHECK_FILE}}
    wrong = {"type": "required_input_missing", "message": "Required input is missing", "details": "shift"}
    assert _check_problems({"error": {"message": "invalid"}, "node_errors": {"1": {"class_type": "UNETLoader", "errors": [missing]}}}) == []
    problems = _check_problems({"node_errors": {"1": {"class_type": "UNETLoader", "errors": [missing]},
                                                "5": {"class_type": "ModelSampling", "errors": [wrong]}}})
    assert problems == ["ModelSampling #5: Required input is missing (shift)"]
    assert _check_problems({"error": {"message": "Prompt has no outputs"}, "node_errors": {}}) == ["Prompt has no outputs"]
