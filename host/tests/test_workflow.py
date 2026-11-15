"""Tests for ComfyUI workflow loading, validation, parameter injection, and inspection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.schemas import GenerateRequest
from app.workflow import (
    WorkflowNotFoundError,
    WorkflowValidationError,
    load_workflow_bundle,
    prepare_workflow,
)
from tools.inspect_workflow import _detect_mapping, inspect_workflow_file


def _sample_workflow() -> dict:
    return {
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": 111,
                "steps": 20,
                "cfg": 7.0,
                "positive": ["6", 0],
                "negative": ["7", 0],
            },
        },
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": 512, "height": 512, "batch_size": 1},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "old positive"},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "old negative"},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "test", "images": ["8", 0]},
        },
    }


def _sample_mapping() -> dict:
    return {
        "positive_prompt": {"node_id": "6", "input_key": "text"},
        "negative_prompt": {"node_id": "7", "input_key": "text"},
        "seed": {"node_id": "3", "input_key": "seed"},
        "steps": {"node_id": "3", "input_key": "steps"},
        "cfg": {"node_id": "3", "input_key": "cfg"},
        "width": {"node_id": "5", "input_key": "width"},
        "height": {"node_id": "5", "input_key": "height"},
        "output_node_id": "9",
    }


def test_load_and_prepare_workflow_injects_overrides_without_mutating_template(tmp_path: Path) -> None:
    (tmp_path / "default.json").write_text(json.dumps(_sample_workflow()), encoding="utf-8")
    (tmp_path / "default.mapping.json").write_text(json.dumps(_sample_mapping()), encoding="utf-8")

    template, mapping = load_workflow_bundle(tmp_path, "default")
    req = GenerateRequest(
        prompt="a cyberpunk lighthouse",
        negative_prompt="blurry",
        width=1024,
        height=768,
        steps=30,
        cfg=6.5,
        seed=999,
    )
    prepared, used_seed = prepare_workflow(template, mapping, req)

    assert used_seed == 999
    assert prepared["6"]["inputs"]["text"] == "a cyberpunk lighthouse"
    assert prepared["7"]["inputs"]["text"] == "blurry"
    assert prepared["5"]["inputs"]["width"] == 1024
    assert prepared["5"]["inputs"]["height"] == 768
    assert prepared["3"]["inputs"]["steps"] == 30
    assert prepared["3"]["inputs"]["cfg"] == 6.5
    assert prepared["3"]["inputs"]["seed"] == 999
    # Template must remain untouched.
    assert template["6"]["inputs"]["text"] == "old positive"


def test_prepare_workflow_randomizes_seed_when_omitted_or_negative(tmp_path: Path) -> None:
    (tmp_path / "default.json").write_text(json.dumps(_sample_workflow()), encoding="utf-8")
    (tmp_path / "default.mapping.json").write_text(json.dumps(_sample_mapping()), encoding="utf-8")
    template, mapping = load_workflow_bundle(tmp_path, "default")

    prepared_none, seed_none = prepare_workflow(template, mapping, GenerateRequest(prompt="cat"))
    prepared_neg, seed_neg = prepare_workflow(
        template, mapping, GenerateRequest(prompt="cat", seed=-1)
    )

    assert isinstance(seed_none, int) and 0 <= seed_none < 2**32
    assert isinstance(seed_neg, int) and 0 <= seed_neg < 2**32
    assert prepared_none["3"]["inputs"]["seed"] == seed_none
    assert prepared_neg["3"]["inputs"]["seed"] == seed_neg


def test_load_workflow_rejects_path_traversal_and_ui_format(tmp_path: Path) -> None:
    with pytest.raises(WorkflowValidationError):
        load_workflow_bundle(tmp_path, "../secret")

    with pytest.raises(WorkflowNotFoundError):
        load_workflow_bundle(tmp_path, "missing")

    (tmp_path / "ui_graph.json").write_text(
        json.dumps({"nodes": [], "links": []}), encoding="utf-8"
    )
    with pytest.raises(WorkflowValidationError, match="UI format"):
        load_workflow_bundle(tmp_path, "ui_graph")


def test_prepare_workflow_rejects_invalid_mapping_node(tmp_path: Path) -> None:
    (tmp_path / "default.json").write_text(json.dumps(_sample_workflow()), encoding="utf-8")
    bad_mapping = _sample_mapping()
    bad_mapping["positive_prompt"] = {"node_id": "999", "input_key": "text"}
    (tmp_path / "default.mapping.json").write_text(json.dumps(bad_mapping), encoding="utf-8")

    template, mapping = load_workflow_bundle(tmp_path, "default")
    with pytest.raises(WorkflowValidationError, match="999"):
        prepare_workflow(template, mapping, GenerateRequest(prompt="test"))


def test_inspect_workflow_detects_sampler_and_writes_mapping(tmp_path: Path) -> None:
    wf_path = tmp_path / "custom.json"
    wf_path.write_text(json.dumps(_sample_workflow()), encoding="utf-8")

    detected = _detect_mapping(_sample_workflow())
    assert detected == _sample_mapping()

    exit_code = inspect_workflow_file(wf_path, write_mapping=True, force=False)
    assert exit_code == 0
    assert (tmp_path / "custom.mapping.json").is_file()


def test_repo_default_workflow_retains_wallpaper_meta_and_uses_preview_image() -> None:
    workflows_dir = Path(__file__).resolve().parent.parent / "workflows"
    template, mapping = load_workflow_bundle(workflows_dir, "default")

    assert template["9"]["class_type"] == "PreviewImage"
    assert template["9"]["inputs"]["images"] == ["65", 0]
    assert mapping.output_node_id == "9"

    prepared, used_seed = prepare_workflow(
        template,
        mapping,
        GenerateRequest(prompt="Snow leopard resting on a misty Himalayan ridge at golden hour"),
    )
    assert isinstance(used_seed, int)
    assert prepared["68"]["inputs"]["width"] == 624
    assert prepared["68"]["inputs"]["height"] == 1344
    assert prepared["70"]["inputs"]["steps"] == 6
    assert prepared["72"]["class_type"] == "StringConcatenate"
    assert "9:19.5 aspect ratio" in prepared["72"]["inputs"]["string_a"]
    assert prepared["72"]["inputs"]["string_b"] == (
        "Snow leopard resting on a misty Himalayan ridge at golden hour"
    )
    assert prepared["73"]["inputs"]["string_a"] == ["72", 0]
    assert "upper 25 percent" in prepared["73"]["inputs"]["string_b"]
    assert "f/2.2" in prepared["73"]["inputs"]["string_b"]
    assert prepared["67"]["inputs"]["text"] == ["73", 0]
    assert "tourists" in prepared["71"]["inputs"]["text"]
