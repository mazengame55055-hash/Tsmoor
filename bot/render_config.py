"""Render job description shared by the Discord UI and the generated Kaggle script."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

CODECS = {"libx264": "H.264", "libx265": "H.265 (HEVC)", "libsvtav1": "AV1", "prores_ks": "ProRes (MOV)"}
COLOR_TRANSFORMS = ("KEEP_ORIGINAL", "AgX", "Filmic", "Standard")
ENGINES = ("KEEP_ORIGINAL", "CYCLES", "EEVEE")


class ConfigError(ValueError):
    """Message is safe to show to the user."""


@dataclass
class RenderConfig:
    mode: str = "original"            # 'original' | 'custom'
    blend_slug: str = ""              # Kaggle dataset slug that holds the .blend
    blend_name: str = ""              # original file name (for display + exact match)
    audio_slug: str = ""              # optional dataset slug with an audio file
    audio_name: str = ""
    blend_label: str = ""             # what the user sees (may be non-ASCII); blend_name is the name on Kaggle
    audio_label: str = ""
    force_gpu: bool = True            # device choice only; never touches scene settings
    eevee_to_cycles: bool = False     # explicit opt-in: EEVEE scenes are unreliable on Kaggle

    # --- custom-only fields (ignored in original mode) ---
    frame_start: int = 1
    frame_end: int = 1
    frame_step: int = 1
    samples: int = 128
    fps: int = 24
    resolution_percentage: int = 100
    crf: int = 18
    engine: str = "KEEP_ORIGINAL"
    color_transform: str = "KEEP_ORIGINAL"
    codec: str = "libx264"
    denoise: bool = True

    def validate(self) -> None:
        if self.mode not in ("original", "custom"):
            raise ConfigError("وضع الرندر غلط.")
        if not self.blend_slug or not self.blend_name:
            raise ConfigError("لازم تختار ملف .blend.")
        if self.mode == "custom":
            if not (1 <= self.frame_start <= self.frame_end <= 100_000) or not (1 <= self.frame_step <= 1000):
                raise ConfigError("نطاق الفريمات غلط.")
            if not (1 <= self.samples <= 20_000):
                raise ConfigError("Samples لازم بين 1 و 20000.")
            if not (1 <= self.fps <= 240):
                raise ConfigError("FPS لازم بين 1 و 240.")
            if not (1 <= self.resolution_percentage <= 200):
                raise ConfigError("Resolution % لازم بين 1 و 200.")
            if not (0 <= self.crf <= 51):
                raise ConfigError("CRF لازم بين 0 و 51.")
            if self.codec not in CODECS:
                raise ConfigError("Codec غير مدعوم.")
            if self.color_transform not in COLOR_TRANSFORMS or self.engine not in ENGINES:
                raise ConfigError("اختيار غير مدعوم.")

    def to_json_dict(self) -> dict:
        self.validate()
        return asdict(self)

    def summary(self) -> str:
        if self.mode == "original":
            extra = " • 🔁 EEVEE→Cycles" if self.eevee_to_cycles else ""
            return "🎯 **Original** — الملف زي ما هو متسيّف (فريمات، Samples، FPS، محرك، ألوان)" + extra
        rng = (f"فريم {self.frame_start}" if self.frame_start == self.frame_end
               else f"{self.frame_start}–{self.frame_end} (خطوة {self.frame_step})")
        engine = "من الملف" if self.engine == "KEEP_ORIGINAL" else self.engine
        color = "من الملف" if self.color_transform == "KEEP_ORIGINAL" else self.color_transform
        return (f"🎛️ **Custom** — فريمات: {rng} • Samples: {self.samples} • FPS: {self.fps} • "
                f"Resolution: {self.resolution_percentage}% • Engine: {engine} • Color: {color} • "
                f"Codec: {CODECS[self.codec]} (CRF {self.crf})"
                + (" • 🔁 EEVEE→Cycles" if self.eevee_to_cycles else ""))


_FRAMES_RE = re.compile(r"^\s*(\d+)\s*(?:[-–:]\s*(\d+))?\s*(?:[-–:]\s*(\d+))?\s*$")


def parse_frames(text: str) -> tuple[int, int, int]:
    """'40' -> single frame | '1-96' -> range | '1-96-2' -> range with step."""
    m = _FRAMES_RE.match(text or "")
    if not m:
        raise ConfigError("صيغة الفريمات غلط. اكتب مثلاً `1-96` أو `40` أو `1-96-2`.")
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) else a
    step = int(m.group(3)) if m.group(3) else 1
    if a < 1 or b < a or step < 1:
        raise ConfigError("نطاق الفريمات غلط.")
    return a, b, step


def parse_int(text: str, name: str, lo: int, hi: int) -> int:
    try:
        v = int(str(text).strip().rstrip("%"))
    except ValueError:
        raise ConfigError(f"{name} لازم يكون رقم.")
    if not lo <= v <= hi:
        raise ConfigError(f"{name} لازم بين {lo} و {hi}.")
    return v
