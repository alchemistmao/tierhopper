from datetime import UTC, datetime, timedelta
from pathlib import Path

from tierhopper.adapters.base import AdapterError, AttemptState, CreditReading, ValidationResult
from tierhopper.models import AttemptStatus, CreditSource, CreditUnit, EndReason, ProviderStatus
from tierhopper.service import TierHopper
from tierhopper.store import MemoryStore

EXAMPLE = str(Path(__file__).resolve().parents[1] / "examples" / "hello-gpu")


class FakeAdapter:
    def __init__(self, name, script, resets_in_days, fail_submit=False):
        self.id = name
        self.script = list(script)  # statuses returned by successive polls
        self.resets_in_days = resets_in_days
        self.fail_submit = fail_submit
        self.submitted = 0

    def validate_credentials(self):
        return ValidationResult(True, "ok")

    def credit_remaining(self, provider, used_usd=0.0):
        return CreditReading(10.0, CreditUnit.USD, CreditSource.ESTIMATE,
                             resets_at=datetime.now(UTC) + timedelta(days=self.resets_in_days))

    def submit(self, plan):
        if self.fail_submit:
            raise AdapterError("boom")
        self.submitted += 1
        return {"id": plan.attempt_id}

    def poll(self, ref):
        status = self.script.pop(0) if self.script else AttemptStatus.RUNNING
        return AttemptState(status, gpu_seconds=60 if status.terminal else None, result={})

    def fetch_outputs(self, ref, dest, result=None):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "result.json").write_text("{}")
        return [dest / "result.json"]

    def cancel(self, ref):
        pass

    def max_session(self, provider):
        return None


class FakeBlobs:
    def __init__(self):
        self.objects = {}

    def put(self, key, data):
        self.objects[key] = data

    def get(self, key):
        return self.objects.get(key)

    def exists(self, key):
        return key in self.objects

    def presign_get(self, key, ttl):
        return f"get://{key}"

    def presign_put(self, key, ttl):
        return f"put://{key}"


def make(tmp_path, modal, kaggle, blobs=None, same_speed=False):
    """same_speed=True leaves Modal with only its T4, so tie-break rules can be tested."""
    store = MemoryStore()
    adapters = {"modal": modal, "kaggle": kaggle}
    th = TierHopper(store, adapter_factory=adapters.__getitem__, results_dir=tmp_path / "results",
                    packages_dir=tmp_path / "pkgs", blobs=blobs)
    for pid in ("modal", "kaggle"):
        p = store.get_provider(pid)
        update = {"status": ProviderStatus.ACTIVE}
        if same_speed and pid == "modal":
            update["gpu_catalog"] = [g for g in p.gpu_catalog if g.type == "T4"]
        store.upsert_provider(p.model_copy(update=update))
    return th, store


def test_fastest_free_gpu_goes_first(tmp_path):
    """Kaggle's credit expires sooner, but Modal has faster GPUs the free credit can pay for."""
    modal = FakeAdapter("modal", [], resets_in_days=20)
    kaggle = FakeAdapter("kaggle", [], resets_in_days=2)
    th, store = make(tmp_path, modal, kaggle)
    out = th.submit_job(EXAMPLE)
    assert out["providers"] == ["modal"]
    assert [s["provider"] for s in out["plan"]] == ["modal", "kaggle"]
    gpu = next(iter(store.attempts.values())).gpu_type
    assert gpu == "H100" and out["estimate"]["speed_vs_t4"] == 10  # 0.05 T4-hours cost cents on the fastest GPU


def test_fast_gpu_must_fit_the_remaining_credit():
    from tierhopper.models import CreditSnapshot, GpuOffer
    from tierhopper.routing import pick_gpu
    from tierhopper.spec import parse_spec
    from tierhopper.store import MemoryStore as Store

    store = Store()
    from tierhopper.registry import sync_registry

    sync_registry(store)
    modal = store.get_provider("modal")
    spec = parse_spec({"name": "big", "entrypoint": "a", "gpu": {"min_vram_gb": 12}, "timeout_minutes": 600})
    pricey = [GpuOffer(type="H100", vram_gb=80, usd_per_hour=100.0), GpuOffer(type="T4", vram_gb=16, usd_per_hour=0.59)]
    ten = CreditSnapshot(provider_id="modal", remaining=10, unit="usd", source="estimate")

    # 10 T4-hours: the H100 would need 1 h x $100, the T4 10 h x $0.59 -> only the T4 fits $10
    assert pick_gpu(modal, pricey, ten, spec, None, t4_hours=10).type == "T4"
    # real catalog, 20 T4-hours: H100 = 2 h x $3.95 (+10%) = $8.69 fits $10 -> fastest wins
    assert pick_gpu(modal, modal.gpu_catalog, ten, spec, None, t4_hours=20).type == "H100"
    # 400 T4-hours fit nowhere: take the most work per dollar, so the job gets furthest before hopping
    best = pick_gpu(modal, modal.gpu_catalog, ten, spec, None, t4_hours=400)
    assert best.type == max(modal.gpu_catalog, key=lambda g: {"T4": 1, "L4": 1.5, "A10": 2, "L40S": 4,
                            "A100-40GB": 5, "A100-80GB": 5.5, "H100": 10}[g.type] / g.usd_per_hour).type
    # paid providers never trade money for speed
    runpod = store.get_provider("runpod")
    assert pick_gpu(runpod, runpod.gpu_catalog, None, spec, None, t4_hours=20).type == min(
        runpod.gpu_catalog, key=lambda g: g.usd_per_hour).type


def test_expiring_credit_breaks_ties_between_equal_gpus(tmp_path):
    modal = FakeAdapter("modal", [], resets_in_days=20)
    kaggle = FakeAdapter("kaggle", [], resets_in_days=2)
    th, _ = make(tmp_path, modal, kaggle, same_speed=True)
    out = th.submit_job(EXAMPLE)
    assert out["providers"] == ["kaggle"]
    assert [s["provider"] for s in out["plan"]] == ["kaggle", "modal"]


def test_success_downloads_results(tmp_path):
    modal = FakeAdapter("modal", [AttemptStatus.RUNNING, AttemptStatus.SUCCEEDED], resets_in_days=1)
    kaggle = FakeAdapter("kaggle", [], resets_in_days=5)
    th, _ = make(tmp_path, modal, kaggle)
    job_id = th.submit_job(EXAMPLE)["job_id"]
    assert th.job_status(job_id)["status"] == "running"
    status = th.job_status(job_id)
    assert status["status"] == "done"
    assert (tmp_path / "results" / job_id / "result.json").exists()
    assert status["stops"][0]["gpu_minutes"] == 1.0


def test_failure_hops_to_next_provider(tmp_path):
    modal = FakeAdapter("modal", [AttemptStatus.FAILED], resets_in_days=1)
    kaggle = FakeAdapter("kaggle", [AttemptStatus.SUCCEEDED], resets_in_days=5)
    th, _ = make(tmp_path, modal, kaggle)
    job_id = th.submit_job(EXAMPLE)["job_id"]
    th.job_status(job_id)  # modal fails -> hop to kaggle
    status = th.job_status(job_id)
    assert [s["provider"] for s in status["stops"]] == ["modal", "kaggle"]
    assert status["status"] == "done"


def test_submit_error_falls_through(tmp_path):
    modal = FakeAdapter("modal", [], resets_in_days=1, fail_submit=True)
    kaggle = FakeAdapter("kaggle", [], resets_in_days=5)
    th, _ = make(tmp_path, modal, kaggle)
    assert th.submit_job(EXAMPLE)["providers"] == ["kaggle"]


def test_dry_run_and_inactive_providers(tmp_path):
    store = MemoryStore()
    th = TierHopper(store, adapter_factory=lambda n: None, results_dir=tmp_path, blobs=None)
    out = th.submit_job(EXAMPLE, dry_run=True)
    assert out["plan"] == []
    assert "not connected" in out["not_used"]["modal"]
    assert "no adapter" in out["not_used"]["lightning"] or "not connected" in out["not_used"]["lightning"]


def _results_tar(text="ok"):
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = text.encode()
        info = tarfile.TarInfo("results/out.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_session_limit_resumes_from_checkpoint_same_provider_allowed(tmp_path, monkeypatch):
    from tierhopper import ingest

    monkeypatch.setattr(ingest, "ingest_secret", lambda: "s3cret-value")
    blobs = FakeBlobs()
    kaggle = FakeAdapter("kaggle", [AttemptStatus.FAILED, AttemptStatus.SUCCEEDED], resets_in_days=1)
    modal = FakeAdapter("modal", [], resets_in_days=9)
    th, store = make(tmp_path, modal, kaggle, blobs=blobs, same_speed=True)
    job_id = th.submit_job(EXAMPLE)["job_id"]
    assert blobs.exists(f"pkg/{job_id}.tar.gz")
    first = next(iter(store.attempts.values()))
    token = ingest.token_for(first.id, "s3cret-value")

    assert th.handle_heartbeat({"attempt_id": first.id, "kind": "started"}, "bad")["ok"] is False
    th.handle_heartbeat({"attempt_id": first.id, "kind": "checkpoint", "progress": 0.4}, token)
    th.handle_heartbeat({"attempt_id": first.id, "kind": "exit", "reason": "session_limit", "seconds": 60}, token)
    assert store.get_job(job_id).progress == 0.4

    th.job_status(job_id)  # kaggle reports FAILED, but the runner said session_limit -> hop, no failure
    attempts = sorted(store.attempts.values(), key=lambda a: a.started_at)
    assert [a.end_reason for a in attempts[:1]] == [EndReason.SESSION_LIMIT]
    assert len(attempts) == 2 and attempts[1].provider_id == "kaggle"  # expiring credit first, not excluded
    assert attempts[1].progress_start == 0.4

    blobs.put(f"results/{job_id}/0/outputs.tar.gz", _results_tar("done"))
    status = th.job_status(job_id)
    assert status["status"] == "done"
    assert (tmp_path / "results" / job_id / "results" / "out.txt").read_text() == "done"
    fetched = th.fetch_results(job_id, dest_dir=str(tmp_path / "dl"))
    assert fetched["items"][0]["kind"] == "results"


def test_credit_exhausted_excludes_provider(tmp_path):
    modal = FakeAdapter("modal", [AttemptStatus.FAILED], resets_in_days=1)
    modal_poll = modal.poll
    modal.poll = lambda ref: AttemptState(AttemptStatus.FAILED, message="Workspace billing: spend limit reached") \
        if modal_poll(ref).status == AttemptStatus.FAILED else modal_poll(ref)
    kaggle = FakeAdapter("kaggle", [AttemptStatus.SUCCEEDED], resets_in_days=5)
    th, store = make(tmp_path, modal, kaggle)
    job_id = th.submit_job(EXAMPLE)["job_id"]
    th.job_status(job_id)
    assert [s["end_reason"] for s in th.job_status(job_id)["stops"]] == ["credit_exhausted", "completed"]
    assert store.latest_credit("modal").remaining == 0


def test_no_provider_waits_in_line_and_tick_dispatches(tmp_path):
    modal = FakeAdapter("modal", [], resets_in_days=1, fail_submit=True)
    kaggle = FakeAdapter("kaggle", [], resets_in_days=5, fail_submit=True)
    th, store = make(tmp_path, modal, kaggle)
    job_id = th.submit_job(EXAMPLE)["job_id"]
    assert store.get_job(job_id).status == "queued"
    kaggle.fail_submit = False
    assert th.tick()["dispatched"] == 1
    assert store.get_job(job_id).status == "running"
