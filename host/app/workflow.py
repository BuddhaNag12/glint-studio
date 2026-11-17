"""ComfyUI API-format workflow loader, validator, and parameter injector."""

from __future__ import annotations

import copy
import json
import re
import secrets
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from app.schemas import GenerateRequest

WORKFLOW_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


class WorkflowNotFoundError(FileNotFoundError):
    """Raised when a workflow or mapping file does not exist."""


class WorkflowValidationError(ValueError):
    """Raised when a workflow or mapping structure is invalid."""


class NodeInputTarget(BaseModel):
    """Identifies one ComfyUI node ID and input field to override."""

    node_id: str
    input_key: str


class WorkflowMapping(BaseModel):
    """Maps API request fields onto ComfyUI workflow node inputs."""

    positive_prompt: NodeInputTarget
    negative_prompt: NodeInputTarget | None = None
    seed: NodeInputTarget | None = None
    steps: NodeInputTarget | None = None
    cfg: NodeInputTarget | None = None
    width: NodeInputTarget | None = None
    height: NodeInputTarget | None = None
    output_node_id: str | None = None


def _resolve_workflow_file(workflows_dir: Path, filename: str) -> Path:
    """Resolve a file inside workflows_dir and reject path escapes."""
    base_dir = workflows_dir.resolve()
    target = (base_dir / filename).resolve()
    if not target.is_relative_to(base_dir):
        raise WorkflowValidationError("Invalid workflow path.")
    return target


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    """Load and verify that a JSON file contains a JSON object."""
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorkflowValidationError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkflowValidationError(f"{label} must be a JSON object.")
    return data


def _is_ui_workspace_format(workflow: dict[str, Any]) -> bool:
    """Detect ComfyUI UI graph exports that cannot be sent to /prompt."""
    return isinstance(workflow.get("nodes"), list) and isinstance(workflow.get("links"), list)


def load_workflow_bundle(
    workflows_dir: Path,
    workflow_name: str,
) -> tuple[dict[str, Any], WorkflowMapping]:
    """Load an API-format ComfyUI workflow and its accompanying mapping config."""
    if not WORKFLOW_NAME_PATTERN.match(workflow_name):
        raise WorkflowValidationError(
            "Workflow name may only contain letters, numbers, underscores, and hyphens."
        )

    workflow_path = _resolve_workflow_file(workflows_dir, f"{workflow_name}.json")
    if not workflow_path.is_file():
        raise WorkflowNotFoundError(
            f"Workflow '{workflow_name}' not found at '{workflow_path}'. "
            "Export your ComfyUI graph using 'Save (API Format)' and place it there."
        )

    workflow_data = _load_json_object(workflow_path, f"Workflow '{workflow_name}.json'")
    if _is_ui_workspace_format(workflow_data):
        raise WorkflowValidationError(
            f"Workflow '{workflow_name}.json' is in ComfyUI UI format, not API Format. "
            "Enable Dev mode Options in ComfyUI Settings and click 'Save (API Format)'."
        )

    mapping_path = _resolve_workflow_file(workflows_dir, f"{workflow_name}.mapping.json")
    if not mapping_path.is_file():
        raise WorkflowValidationError(
            f"Mapping file '{workflow_name}.mapping.json' not found. "
            f"Run 'python tools/inspect_workflow.py {workflow_path}' to generate it."
        )

    mapping_data = _load_json_object(mapping_path, f"Mapping '{workflow_name}.mapping.json'")
    try:
        mapping = WorkflowMapping.model_validate(mapping_data)
    except ValidationError as exc:
        raise WorkflowValidationError(f"Invalid workflow mapping: {exc}") from exc

    return workflow_data, mapping


def _set_node_input(
    workflow: dict[str, Any],
    target: NodeInputTarget,
    value: Any,
    field_name: str,
) -> None:
    """Validate and assign a value to a specific ComfyUI node input."""
    node = workflow.get(target.node_id)
    if not isinstance(node, dict):
        raise WorkflowValidationError(
            f"Mapping '{field_name}' references missing node_id '{target.node_id}'."
        )
    inputs = node.get("inputs")
    if not isinstance(inputs, dict):
        raise WorkflowValidationError(
            f"Node '{target.node_id}' for '{field_name}' has no 'inputs' dictionary."
        )
    if target.input_key not in inputs:
        raise WorkflowValidationError(
            f"Node '{target.node_id}' for '{field_name}' has no input '{target.input_key}'."
        )
    inputs[target.input_key] = value


# The catalog folder each loader node reads, and the input that names the file.
LOADERS = {
    "UNETLoader": ("diffusion_models", "unet_name"),
    "CLIPLoader": ("text_encoders", "clip_name"),
    "VAELoader": ("vae", "vae_name"),
    "CheckpointLoaderSimple": ("checkpoints", "ckpt_name"),
}


def use_model_files(workflow: dict[str, Any], dests: list[str]) -> None:
    """Point each loader node at the file from its folder in `dests` (catalog "<folder>/<name>"),
    so one workflow serves every variant of a model.
    shortcut: one file per folder; a workflow with two loaders of a kind (dual CLIP) needs a mapping entry."""
    names = dict(d.split("/", 1) for d in dests)
    for node in workflow.values():
        folder, key = LOADERS.get(node.get("class_type"), ("", ""))
        if folder in names and key in node.get("inputs", {}):
            node["inputs"][key] = names[folder]


def prepare_workflow(
    workflow: dict[str, Any],
    mapping: WorkflowMapping,
    request: GenerateRequest,
) -> tuple[dict[str, Any], int | None]:
    """Return a deep-copied workflow payload populated with request parameters and the seed used."""
    payload = copy.deepcopy(workflow)

    if mapping.output_node_id is not None and mapping.output_node_id not in payload:
        raise WorkflowValidationError(
            f"Mapping 'output_node_id' references missing node_id '{mapping.output_node_id}'."
        )

    _set_node_input(payload, mapping.positive_prompt, request.prompt, "positive_prompt")

    if request.negative_prompt is not None and mapping.negative_prompt is not None:
        _set_node_input(
            payload,
            mapping.negative_prompt,
            request.negative_prompt,
            "negative_prompt",
        )

    used_seed: int | None = request.seed
    if mapping.seed is not None:
        if request.seed is None or request.seed < 0:
            used_seed = secrets.randbelow(2**32)
        else:
            used_seed = request.seed
        _set_node_input(payload, mapping.seed, used_seed, "seed")

    optional_overrides: list[tuple[str, NodeInputTarget | None, Any]] = [
        ("steps", mapping.steps, request.steps),
        ("cfg", mapping.cfg, request.cfg),
        ("width", mapping.width, request.width),
        ("height", mapping.height, request.height),
    ]
    for field_name, target, value in optional_overrides:
        if value is not None and target is not None:
            _set_node_input(payload, target, value, field_name)

    return payload, used_seed
