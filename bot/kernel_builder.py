"""Turns a RenderConfig into the folder that `kaggle kernels push` uploads."""
from __future__ import annotations

import json
from pathlib import Path

from .render_config import RenderConfig

TEMPLATE_PATH = Path(__file__).with_name("kernel_job.py")
CONFIG_LINE = "CONFIG = None  # @@CONFIG@@"


def render_script_text(cfg: RenderConfig) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    if CONFIG_LINE not in template:
        raise RuntimeError("kernel_job.py lost its CONFIG placeholder line")
    # json.dumps twice => a valid Python string literal holding the JSON (no quoting bugs possible)
    literal = json.dumps(json.dumps(cfg.to_json_dict()))
    return template.replace(CONFIG_LINE, f"CONFIG = json.loads({literal})")


def write_script(folder: Path, cfg: RenderConfig) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "render.py"
    path.write_text(render_script_text(cfg), encoding="utf-8")
    return path


def dataset_slugs(cfg: RenderConfig) -> list[str]:
    return [s for s in (cfg.blend_slug, cfg.audio_slug) if s]
