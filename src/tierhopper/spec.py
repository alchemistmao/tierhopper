"""Job specification (`tierhopper.yaml`) parsing and validation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SPEC_FILENAME = "tierhopper.yaml"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GpuRequirement(_Strict):
    min_vram_gb: float = Field(default=0, ge=0)
    types: list[str] = Field(default_factory=list)
    count: int = Field(default=1, ge=1, le=8)


class CheckpointConfig(_Strict):
    dir: str = "checkpoints/"
    every_minutes: int = Field(default=10, ge=1, le=240)


class ShardConfig(_Strict):
    over: str | None = None
    count: int | None = Field(default=None, ge=1, le=256)
    max_parallel: int = Field(default=4, ge=1, le=64)

    @model_validator(mode="after")
    def _one_source(self) -> ShardConfig:
        if (self.over is None) == (self.count is None):
            raise ValueError("shard needs exactly one of `over` or `count`")
        return self


class ServeConfig(_Strict):
    engine: str = "vllm"
    model: str
    args: list[str] = Field(default_factory=list)

    @field_validator("engine")
    @classmethod
    def _known_engine(cls, v: str) -> str:
        if v != "vllm":
            raise ValueError("only `vllm` is supported")
        return v


class JobSpec(_Strict):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,62}$")
    project: str = Field(default="default", pattern=r"^[a-z0-9][a-z0-9._-]{0,62}$")
    entrypoint: str = Field(min_length=1)
    workdir: str = "."
    python: str = "3.11"
    requirements: str | None = None
    apt: list[str] = Field(default_factory=list)  # extra system packages (git, curl, wget are always there)
    gpu: GpuRequirement = Field(default_factory=GpuRequirement)
    estimate_hours: float | None = Field(default=None, gt=0)
    timeout_minutes: int = Field(default=60, ge=1, le=24 * 60)
    checkpoint: CheckpointConfig | None = None
    progress_file: str | None = None
    shard: ShardConfig | None = None
    serve: ServeConfig | None = None
    outputs: list[str] = Field(default_factory=list)
    allow_paid: bool = False

    @field_validator("apt")
    @classmethod
    def _package_names(cls, v: list[str]) -> list[str]:
        import re

        for name in v:
            if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]{0,63}", name):
                raise ValueError(f"invalid apt package name: {name!r}")
        return v

    def spec_hash(self) -> str:
        """Stable hash of the fields that affect runtime (used for history-based estimates)."""
        relevant = self.model_dump(exclude={"name", "estimate_hours", "allow_paid"})
        raw = json.dumps(relevant, sort_keys=True).encode()
        return hashlib.sha256(raw).hexdigest()[:16]


class SpecError(ValueError):
    pass


def load_spec(path: str | Path) -> tuple[JobSpec, Path]:
    """Load a spec from a file or a directory containing `tierhopper.yaml`.

    Returns the spec and the resolved job root directory (spec dir + `workdir`).
    """
    p = Path(path).expanduser().resolve()
    if p.is_dir():
        p = p / SPEC_FILENAME
    if not p.is_file():
        raise SpecError(f"spec not found: {p}")
    try:
        data = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as e:
        raise SpecError(f"invalid YAML in {p}: {e}") from e
    spec = parse_spec(data)
    root = (p.parent / spec.workdir).resolve()
    if not root.is_dir():
        raise SpecError(f"workdir does not exist: {root}")
    if spec.requirements and not (root / spec.requirements).is_file():
        raise SpecError(f"requirements file not found: {root / spec.requirements}")
    return spec, root


def parse_spec(data: dict) -> JobSpec:
    try:
        return JobSpec.model_validate(data)
    except ValueError as e:
        raise SpecError(str(e)) from e
