from datetime import timedelta
from pathlib import Path

from test_service import EXAMPLE, FakeAdapter, make

from tierhopper.models import Attempt, AttemptStatus, EndReason, Event, ProviderStatus, now
from tierhopper.routing import plan_route
from tierhopper.spec import parse_spec
from tierhopper.stats import compute_stats

SHARDED = str(Path(__file__).parent / "fixtures" / "sharded")


def test_sharding_respects_parallel_and_provider_limits(tmp_path):
    modal = FakeAdapter("modal", [], resets_in_days=1)
    kaggle = FakeAdapter("kaggle", [], resets_in_days=5)
    th, store = make(tmp_path, modal, kaggle)
    modal_p = store.get_provider("modal")
    store.upsert_provider(modal_p.model_copy(update={"max_concurrent": 2}))
    out = th.submit_job(SHARDED)
    assert out["shards"] == 6  # 6 files, max_parallel 3 -> up to 6 parts
    shards = store.list_shards(out["job_id"])
    assert sorted(i for s in shards for i in s.items) == [f"data/doc{i}.txt" for i in range(1, 7)]
    running = [s for s in shards if s.status == "running"]
    assert len(running) == 3  # max_parallel
    per_provider = {}
    for a in store.attempts.values():
        per_provider[a.provider_id] = per_provider.get(a.provider_id, 0) + 1
    assert per_provider == {"modal": 2, "kaggle": 1}  # Modal first (expires sooner) until its limit


def test_over_threshold_waits_for_approval(tmp_path, monkeypatch):
    monkeypatch.setenv("TIERHOPPER_APPROVAL_GPU_HOURS", "0.01")
    modal = FakeAdapter("modal", [], resets_in_days=1)
    kaggle = FakeAdapter("kaggle", [], resets_in_days=5)
    th, store = make(tmp_path, modal, kaggle)
    out = th.submit_job(EXAMPLE)
    assert out["status"] == "awaiting_approval" and out["needs_approval"]
    assert modal.submitted == kaggle.submitted == 0
    assert th.tick()["dispatched"] == 0  # the scheduler never dispatches unapproved work
    assert th.approve(out["job_id"])["status"] == "running"


def test_paid_provider_only_after_approval(tmp_path, monkeypatch):
    import tierhopper.routing as routing

    monkeypatch.setattr(routing, "IMPLEMENTED", routing.IMPLEMENTED | {"runpod"})
    modal = FakeAdapter("modal", [], resets_in_days=1, fail_submit=True)
    kaggle = FakeAdapter("kaggle", [], resets_in_days=5, fail_submit=True)
    runpod = FakeAdapter("runpod", [], resets_in_days=30)
    th, store = make(tmp_path, modal, kaggle)
    th.adapter_factory = {"modal": modal, "kaggle": kaggle, "runpod": runpod}.__getitem__
    rp = store.get_provider("runpod")
    store.upsert_provider(rp.model_copy(update={"status": ProviderStatus.ACTIVE, "adapter": "runpod"}))

    job_id = th.submit_job(EXAMPLE)["job_id"]
    job = store.get_job(job_id)
    assert job.status == "awaiting_approval"
    assert job.estimate["approval"]["paid"]["provider"] == "runpod"
    assert runpod.submitted == 0  # never automatic
    th.approve(job_id)
    assert runpod.submitted == 1


def test_denying_paid_keeps_waiting_for_free(tmp_path, monkeypatch):
    import tierhopper.routing as routing

    monkeypatch.setattr(routing, "IMPLEMENTED", routing.IMPLEMENTED | {"runpod"})
    modal = FakeAdapter("modal", [], resets_in_days=1, fail_submit=True)
    kaggle = FakeAdapter("kaggle", [], resets_in_days=5, fail_submit=True)
    runpod = FakeAdapter("runpod", [], resets_in_days=30)
    th, store = make(tmp_path, modal, kaggle)
    th.adapter_factory = {"modal": modal, "kaggle": kaggle, "runpod": runpod}.__getitem__
    rp = store.get_provider("runpod")
    store.upsert_provider(rp.model_copy(update={"status": ProviderStatus.ACTIVE, "adapter": "runpod"}))
    job_id = th.submit_job(EXAMPLE)["job_id"]
    assert th.deny(job_id)["status"] == "queued"
    kaggle.fail_submit = False
    th.tick()
    assert store.get_job(job_id).status == "running" and runpod.submitted == 0


def test_learned_failure_rate_demotes_provider(tmp_path):
    modal = FakeAdapter("modal", [], resets_in_days=3)
    kaggle = FakeAdapter("kaggle", [], resets_in_days=3)
    th, store = make(tmp_path, modal, kaggle, same_speed=True)  # equal GPUs: reliability decides
    for i in range(6):
        store.create_attempt(Attempt(shard_id=f"s{i}", provider_id="modal", gpu_type="T4",
                                     status=AttemptStatus.FAILED, end_reason=EndReason.ERROR))
        store.create_attempt(Attempt(shard_id=f"k{i}", provider_id="kaggle", gpu_type="T4x2",
                                     status=AttemptStatus.SUCCEEDED, end_reason=EndReason.COMPLETED))
    stats = compute_stats(store)
    assert stats["modal"].failure_rate > 0.8 > stats["kaggle"].failure_rate
    spec = parse_spec({"name": "x", "entrypoint": "a", "gpu": {"min_vram_gb": 12}})
    providers = [p for p in store.list_providers() if p.status == ProviderStatus.ACTIVE]
    credits = {p.id: None for p in providers}
    ranked, _ = plan_route(spec, providers, credits, stats=stats)
    assert ranked[0].provider.id == "kaggle"


def test_queue_time_from_running_events(tmp_path):
    modal = FakeAdapter("modal", [], resets_in_days=3)
    kaggle = FakeAdapter("kaggle", [], resets_in_days=3)
    th, store = make(tmp_path, modal, kaggle)
    a = store.create_attempt(Attempt(shard_id="s", provider_id="kaggle", gpu_type="T4x2",
                                     started_at=now() - timedelta(minutes=10), end_reason=EndReason.COMPLETED))
    store.add_event(Event(job_id="j", attempt_id=a.id, type="attempt_running", ts=a.started_at + timedelta(minutes=4)))
    assert compute_stats(store)["kaggle"].queue_p50 == 240


def test_pause_and_resume(tmp_path):
    modal = FakeAdapter("modal", [], resets_in_days=1)
    kaggle = FakeAdapter("kaggle", [], resets_in_days=5)
    th, store = make(tmp_path, modal, kaggle)
    job_id = th.submit_job(EXAMPLE)["job_id"]
    assert th.pause(job_id)["status"] == "paused"
    assert th.tick()["dispatched"] == 0
    assert th.resume(job_id)["status"] == "running"
