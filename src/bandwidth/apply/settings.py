"""Applying settings, `config/apply.yaml` (SPEC FA-1, FA-6). Every key is optional."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..paths import CONFIG_DIR


class ApplySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    drafting: Literal["cloud_anonymised", "local"] = "cloud_anonymised"
    daily_cap: int = Field(5, ge=0, le=20)


def load_settings(path: Path = CONFIG_DIR / "apply.yaml") -> ApplySettings:
    if not path.exists():
        return ApplySettings()
    try:
        return ApplySettings.model_validate(yaml.safe_load(path.read_text()) or {})
    except ValidationError as e:
        raise ValueError(f"{path}: {e}") from None
