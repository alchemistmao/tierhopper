"""Provider adapter interface and shared helpers."""

from __future__ import annotations

import io
import tarfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from tierhopper.models import AttemptStatus, CreditSource, CreditUnit, GpuOffer, Provider
from tierhopper.spec import JobSpec

# Never shipped to a provider, on top of .gitignore-style defaults.
DEFAULT_EXCLUDES = {".git", ".venv", "venv", "__pycache__", "node_modules", ".env", "checkpoints", "results",
                    ".tierhopper", ".DS_Store", "kaggle.json", ".modal.toml"}
MAX_PACKAGE_BYTES = 20 * 2**20


class AdapterError(RuntimeError):
    pass


@dataclass
class ValidationResult:
    ok: bool
    detail: str


@dataclass
class CreditReading:
    remaining: float
    unit: CreditUnit
    source: CreditSource
    total: float | None = None
    resets_at: datetime | None = None


RUNNER_FILE = "_tierhopper_runner.py"
RUNNER_SOURCE = Path(__file__).resolve().parents[1] / "runner_script.py"


@dataclass
class AttemptPlan:
    attempt_id: str
    job_id: str
    spec: JobSpec
    package: bytes  # tar.gz of the job dir + the runner (see build_package)
    gpu: GpuOffer
    runner_config: dict[str, Any] = field(default_factory=dict)
    package_url: str | None = None  # presigned GET of the package in R2 (providers that download it)
    gpu_fallbacks: list[str] = field(default_factory=list)  # slower GPU types to accept if `gpu` has no capacity

    @property
    def requirements(self) -> str | None:
        return read_member(self.package, self.spec.requirements) if self.spec.requirements else None

    @property
    def command(self) -> str:
        return f"python {RUNNER_FILE}"


@dataclass
class AttemptState:
    status: AttemptStatus
    message: str | None = None
    gpu_seconds: float | None = None
    result: dict[str, Any] | None = None
    billed_usd: float | None = None  # what the provider says this attempt cost (credit or money)


class ProviderAdapter(Protocol):
    id: str

    def validate_credentials(self) -> ValidationResult: ...
    def credit_remaining(self, provider: Provider) -> CreditReading: ...
    def submit(self, plan: AttemptPlan) -> dict[str, Any]: ...
    def poll(self, ref: dict[str, Any]) -> AttemptState: ...
    def fetch_outputs(self, ref: dict[str, Any], dest: Path) -> list[Path]: ...
    def cancel(self, ref: dict[str, Any]) -> None: ...
    def max_session(self, provider: Provider) -> timedelta | None: ...


def pack_workdir(root: Path, extra_excludes: set[str] | None = None) -> bytes:
    """Tar+gzip the job directory, skipping secrets, VCS and artefact folders."""
    excludes = DEFAULT_EXCLUDES | (extra_excludes or set())
    ignore_file = root / ".tierhopperignore"
    if ignore_file.is_file():
        excludes |= {line.strip().rstrip("/") for line in ignore_file.read_text().splitlines()
                     if line.strip() and not line.startswith("#")}

    def _filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        parts = Path(info.name).parts
        if any(p in excludes or p.startswith(".env") for p in parts):
            return None
        return info

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for child in sorted(root.iterdir()):
            tar.add(child, arcname=child.name, filter=_filter)
    data = buf.getvalue()
    if len(data) > MAX_PACKAGE_BYTES:
        raise AdapterError(f"job package is {len(data) // 2**20} MB; limit is {MAX_PACKAGE_BYTES // 2**20} MB "
                           "(put data in a dataset/R2 or add a .tierhopperignore)")
    return data


def build_package(root: Path) -> bytes:
    """Job package: the job directory plus the TierHopper runner at the top level."""
    base = pack_workdir(root)
    buf = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(base), mode="r:gz") as src, tarfile.open(fileobj=buf, mode="w:gz") as dst:
        for member in src.getmembers():
            dst.addfile(member, src.extractfile(member) if member.isfile() else None)
        dst.add(RUNNER_SOURCE, arcname=RUNNER_FILE)
    return buf.getvalue()


def read_member(package: bytes, name: str) -> str | None:
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as tar:
        try:
            f = tar.extractfile(name)
        except KeyError:
            return None
        return f.read().decode() if f else None


def extract_tar(data: bytes, dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(dest, filter="data")
        return [dest / m.name for m in tar.getmembers() if m.isfile()]
