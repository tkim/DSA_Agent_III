"""
Optional media generation (image + 3D) via Lemonade 11.0.

Lemonade 11.0 added image generation and a Trellis.2 image-to-3D pipeline. This
module exposes them as *agent tools* so a query like "draw the architecture" can
trigger generation. It is EXPERIMENTAL and off the critical path: endpoints are
build-dependent and confirmed in Phase-0 Gate C.

To enable, register TOOL_SCHEMAS/TOOL_EXECUTORS from here into an agent (or a
dedicated media agent). Left unregistered by default so the core agent has no
dependency on unconfirmed endpoints.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import urllib.error
import urllib.request

from core import config


def _post(path: str, payload: dict) -> dict:
    url = f"{config.native_base_url()}{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {config.LEMONADE_API_KEY}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=config.REQUEST_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def generate_image(prompt: str) -> dict:
    """Generate an image from a text prompt. Returns {path} or {error}."""
    try:
        data = _post("/v1/images/generations", {"prompt": prompt})
        b64 = (data.get("data") or [{}])[0].get("b64_json")
        if not b64:
            return {"error": "No image data returned", "raw": data}
        out = os.path.join(tempfile.gettempdir(), "dsa_iii_image.png")
        with open(out, "wb") as fh:
            fh.write(base64.b64decode(b64))
        return {"path": out}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"error": f"image generation failed: {exc}"}


def generate_3d(image_path: str) -> dict:
    """Generate a 3D model from an input image (Trellis.2). Returns {path} or {error}."""
    try:
        with open(image_path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("ascii")
        data = _post("/api/v1/generate3d", {"image": b64})
        return data if "error" in data else {"result": data}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"error": f"3D generation failed: {exc}"}


# OpenAI-format tool schemas, ready to register into an agent when enabled.
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": "Generate an image from a text prompt (e.g. an architecture diagram).",
            "parameters": {
                "type": "object",
                "properties": {"prompt": {"type": "string"}},
                "required": ["prompt"],
            },
        },
    },
]

TOOL_EXECUTORS = {"generate_image": generate_image}
