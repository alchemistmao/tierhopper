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


def _with_runpod(tmp_path, monkeypatch, modal, kaggle, runpod):
    import tierhopper.routing as routing

    monkeypatch.setattr(routing, "IMPLEMENTED", routing.IMPLEMENTED | {"runpod"})
    th, store = make(tmp_path, modal, kaggle)
    th.adapter_factory = {"modal": modal, "kaggle": kaggle, "runpod": runpod}.__getitem__
    rp = store.get_provider("runpod")
    store.upsert_provider(rp.model_copy(update={"status": ProviderStatus.ACTIVE, "adapter": "runpod"}))
    return th, store


def test_one_approval_pays_for_one_run_only(tmp_path, monkeypatch):
    modal = FakeAdapter("modal", [], resets_in_days=1, fail_submit=True)
    kaggle = FakeAdapter("kaggle", [], resets_in_days=5, fail_submit=True)
    runpod = FakeAdapter("runpod", [AttemptStatus.LOST], resets_in_days=30)  # the pod is taken back
    th, store = _with_runpod(tmp_path, monkeypatch, modal, kaggle, runpod)
    job_id = th.submit_job(EXAMPLE)["job_id"]
    th.approve(job_id)
    assert runpod.submitted == 1
    th.tick()  # the paid run ended early: continuing on RunPod needs a new OK
    assert store.get_job(job_id).status == "awaiting_approval" and runpod.submitted == 1


def test_no_request_to_pay_while_free_gpus_are_only_busy(tmp_path, monkeypatch):
    modal = FakeAdapter("modal", [], resets_in_days=1)
    kaggle = FakeAdapter("kaggle", [], resets_in_days=5)
    runpod = FakeAdapter("runpod", [], resets_in_days=30)
    th, store = _with_runpod(tmp_path, monkeypatch, modal, kaggle, runpod)
    for pid in ("modal", "kaggle"):
        store.upsert_provider(store.get_provider(pid).model_copy(update={"max_concurrent": 1}))
    job_id = th.submit_job(SHARDED)["job_id"]
    assert store.get_job(job_id).status == "running"
    assert runpod.submitted == 0 and "approval" not in store.get_job(job_id).estimate


def test_a_job_whose_parts_all_finished_is_done_and_paused_jobs_run_nothing(tmp_path):
    modal = FakeAdapter("modal", [], resets_in_days=1)
    kaggle = FakeAdapter("kaggle", [], resets_in_days=5)
    th, store = make(tmp_path, modal, kaggle)
    done_id = th.submit_job(EXAMPLE)["job_id"]
    for a in list(store.attempts.values()):  # the part ended but a race left the job "running"
        store.finish_attempt(a.model_copy(update={"status": AttemptStatus.SUCCEEDED,
                                                  "end_reason": EndReason.COMPLETED, "ended_at": now()}))
    for s in store.list_shards(done_id):
        store.update_shard(s.model_copy(update={"status": "done", "progress": 1.0}))
    paused_id = th.submit_job(EXAMPLE)["job_id"]
    job = store.get_job(paused_id)
    store.update_job(job.model_copy(update={"status": "paused"}))  # paused while an attempt was starting
    th.tick()
    assert store.get_job(done_id).status == "done"
    assert all(a.status.terminal for a in store.attempts.values())


def test_the_runner_last_word_decides_how_an_attempt_ended():
    from tierhopper.service import TierHopper

    classify = TierHopper._classify
    failed = AttemptStatus.FAILED
    assert classify(failed, "[tierhopper] job ended: session_limit (exit 75, 3000s)") == EndReason.SESSION_LIMIT
    assert classify(failed, "KeyError: credit_score\n[tierhopper] job ended: error (exit 1)") == EndReason.ERROR
    assert classify(failed, "Your GPU quota is exhausted") == EndReason.CREDIT_EXHAUSTED
    assert classify(AttemptStatus.CANCELLED, None) == EndReason.PREEMPTED


def test_a_finished_job_stuck_on_a_pay_request_is_delivered(tmp_path, monkeypatch):
    from test_service import FakeBlobs, _results_tar

    modal = FakeAdapter("modal", [AttemptStatus.FAILED], resets_in_days=1)
    kaggle = FakeAdapter("kaggle", [], resets_in_days=5, fail_submit=True)
    runpod = FakeAdapter("runpod", [], resets_in_days=30)
    th, store = _with_runpod(tmp_path, monkeypatch, modal, kaggle, runpod)
    th.blobs = FakeBlobs()
    job_id = th.submit_job(EXAMPLE)["job_id"]
    job = store.get_job(job_id)
    store.update_job(job.model_copy(update={"status": "awaiting_approval"}))  # the old code asked to pay
    for a in list(store.attempts.values()):
        store.finish_attempt(a.model_copy(update={"status": AttemptStatus.FAILED, "end_reason": EndReason.ERROR,
                                                  "ended_at": now()}))
    th.blobs.put(f"results/{job_id}/0/outputs.tar.gz", _results_tar("601"))
    th.tick()
    assert store.get_job(job_id).status == "done" and runpod.submitted == 0


def test_a_provider_status_from_a_newer_version_does_not_break_listing(tmp_path):
    modal = FakeAdapter("modal", [], resets_in_days=1)
    kaggle = FakeAdapter("kaggle", [], resets_in_days=5)
    th, store = make(tmp_path, modal, kaggle)
    raw = store.get_provider("kaggle").model_dump(mode="json") | {"status": "some_future_status"}
    from tierhopper.models import Provider

    assert Provider.model_validate(raw).status == ProviderStatus.DISABLED  # skipped, not a crash
    assert th.submit_job(EXAMPLE)["status"] == "running"
