"""Inspect a ComfyUI API-format workflow JSON and generate a starter mapping file."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SAMPLER_CLASSES = {"KSampler", "KSamplerAdvanced", "SamplerCustom", "SamplerCustomAdvanced"}
LATENT_CLASSES = {"EmptyLatentImage", "EmptySD3LatentImage"}
OUTPUT_CLASSES = {"SaveImage", "PreviewImage"}
PROMPT_INPUT_KEYS = ("text", "t5xxl", "clip_l", "prompt")


def _is_link(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 2 and isinstance(value[0], str)


def _trace_prompt_target(
    workflow: dict[str, Any],
    start_ref: Any,
    visited: set[str] | None = None,
) -> dict[str, str] | None:
    """Walk upstream through conditioning links to find a text prompt node."""
    if not _is_link(start_ref):
        return None
    node_id = start_ref[0]
    seen = visited if visited is not None else set()
    if node_id in seen or node_id not in workflow:
        return None
    seen.add(node_id)

    node = workflow[node_id]
    if not isinstance(node, dict):
        return None
    inputs = node.get("inputs")
    if not isinstance(inputs, dict):
        return None

    for key in PROMPT_INPUT_KEYS:
        if isinstance(inputs.get(key), str):
            return {"node_id": node_id, "input_key": key}

    for nested_key in ("conditioning", "positive", "negative", "guider"):
        if nested_key in inputs:
            found = _trace_prompt_target(workflow, inputs[nested_key], seen)
            if found:
                return found

    for value in inputs.values():
        if _is_link(value):
            found = _trace_prompt_target(workflow, value, seen)
            if found:
                return found
    return None


def _detect_mapping(workflow: dict[str, Any]) -> dict[str, Any]:
    """Heuristically infer prompt, sampler, latent size, and output mappings."""
    mapping: dict[str, Any] = {}

    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type") or "")
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}

        if class_type in SAMPLER_CLASSES:
            if "positive_prompt" not in mapping and "positive" in inputs:
                pos = _trace_prompt_target(workflow, inputs["positive"])
                if pos:
                    mapping["positive_prompt"] = pos
            if "negative_prompt" not in mapping and "negative" in inputs:
                neg = _trace_prompt_target(workflow, inputs["negative"])
                if neg:
                    mapping["negative_prompt"] = neg
            for seed_key in ("seed", "noise_seed"):
                if "seed" not in mapping and seed_key in inputs and not _is_link(inputs[seed_key]):
                    mapping["seed"] = {"node_id": node_id, "input_key": seed_key}
            for scalar_key in ("steps", "cfg"):
                if scalar_key not in mapping and scalar_key in inputs and not _is_link(inputs[scalar_key]):
                    mapping[scalar_key] = {"node_id": node_id, "input_key": scalar_key}

        if class_type in LATENT_CLASSES:
            for dim_key in ("width", "height"):
                if dim_key not in mapping and dim_key in inputs and not _is_link(inputs[dim_key]):
                    mapping[dim_key] = {"node_id": node_id, "input_key": dim_key}

        if class_type == "SaveImage" and "output_node_id" not in mapping:
            mapping["output_node_id"] = node_id

    if "positive_prompt" not in mapping:
        for node_id, node in workflow.items():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
            title = str((node.get("_meta") or {}).get("title") or "").lower()
            for key in PROMPT_INPUT_KEYS:
                if isinstance(inputs.get(key), str):
                    if "negative" in title and "negative_prompt" not in mapping:
                        mapping["negative_prompt"] = {"node_id": node_id, "input_key": key}
                    elif "positive_prompt" not in mapping:
                        mapping["positive_prompt"] = {"node_id": node_id, "input_key": key}
                    break

    if "output_node_id" not in mapping:
        for node_id, node in workflow.items():
            if isinstance(node, dict) and str(node.get("class_type") or "") in OUTPUT_CLASSES:
                mapping["output_node_id"] = node_id
                break

    return mapping


def inspect_workflow_file(workflow_path: Path, write_mapping: bool, force: bool) -> int:
    """Inspect workflow_path, print node details, and optionally write a mapping JSON."""
    if not workflow_path.is_file():
        print(f"Error: file not found: {workflow_path}", file=sys.stderr)
        return 1

    try:
        data = json.loads(workflow_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"Error: invalid JSON in {workflow_path}: {exc}", file=sys.stderr)
        return 1

    if not isinstance(data, dict) or (isinstance(data.get("nodes"), list) and isinstance(data.get("links"), list)):
        print(
            "Error: this file looks like ComfyUI UI workspace JSON, not API Format.\n"
            "Enable 'Dev mode Options' in ComfyUI Settings and click 'Save (API Format)'.",
            file=sys.stderr,
        )
        return 1

    print(f"Workflow nodes in {workflow_path}:")
    for node_id, node in data.items():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type", "Unknown")
        title = (node.get("_meta") or {}).get("title", "")
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        scalars = {k: v for k, v in inputs.items() if not _is_link(v)}
        title_part = f" ({title})" if title else ""
        print(f"  [{node_id}] {class_type}{title_part} -> editable inputs: {list(scalars.keys())}")

    suggested = _detect_mapping(data)
    print("\nSuggested mapping:")
    print(json.dumps(suggested, indent=2))

    if write_mapping:
        mapping_path = workflow_path.with_suffix(".mapping.json")
        if mapping_path.exists() and not force:
            print(f"\nSkipped writing {mapping_path} (already exists; pass --force to overwrite).")
        else:
            mapping_path.write_text(json.dumps(suggested, indent=2) + "\n", encoding="utf-8")
            print(f"\nWrote mapping to {mapping_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect an exported ComfyUI API-format workflow JSON and generate a mapping file."
    )
    parser.add_argument("workflow_path", type=Path, help="Path to exported ComfyUI API-format JSON.")
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Only print node details without writing <workflow>.mapping.json.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing <workflow>.mapping.json file.",
    )
    args = parser.parse_args()
    return inspect_workflow_file(args.workflow_path, write_mapping=not args.no_write, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
