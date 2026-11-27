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


def test_the_caller_gets_the_job_and_how_to_wait_but_not_where_it_runs_or_what_it_costs(monkeypatch):
    from tierhopper import mcp_server

    plan = [{"provider": "modal", "gpu": "H100", "score": 1}]
    base = {"plan": plan, "not_used": {"kaggle": "busy"}, "estimate": {"gpu_hours": 0.5, "market_usd": 2.0,
                                                                       "gpu": "H100"}, "shards": 1}

    class Fake:
        status = "running"

        def submit_job(self, spec_path, dry_run=False):
            if dry_run:
                return {"dry_run": True, "needs_approval": None, **base}
            return {"job_id": "abc123", "status": self.status, "needs_approval": "pay $2 on RunPod?",
                    "approve_command": "tierhopper approve abc123", "providers": ["modal"], **base}

        def job_status(self, job_id):
            return {"job_id": job_id, "status": self.status, "progress": 0.4,
                    "stops": [{"shard": 0, "provider": "modal", "gpu": "H100", "status": "running",
                               "gpu_minutes": 3.0, "link": "https://modal.com/x"}]}

    fake = Fake()
    monkeypatch.setattr(mcp_server, "_service", lambda: fake)
    submit = getattr(mcp_server.submit_job, "fn", mcp_server.submit_job)
    status = getattr(mcp_server.job_status, "fn", mcp_server.job_status)
    dry = submit("x", dry_run=True)
    assert dry["valid"] and "can_start_now" not in dry and "when_it_ends" not in dry
    tools = {name for name in dir(mcp_server) if hasattr(getattr(mcp_server, name), "fn")
             or name in ("submit_job", "job_status", "credits_status", "list_providers")}
    for hidden in ("credits_status", "list_providers", "check_providers", "usage_report", "connect_provider"):
        assert not hasattr(mcp_server, hidden), hidden  # credit and providers are not the caller's business
    assert "submit_job" in tools
    skill = (Path(mcp_server.__file__).parent / "skill" / "SKILL.md").read_text().lower()
    for word in ("modal", "kaggle", "runpod", "lightning", "credits_status"):
        assert word not in skill, word
    got = submit("x")
    assert "tierhopper watch abc123" in got["when_it_ends"] and got["estimated_gpu_hours"] == 0.5
    text = str(dry) + str(got)
    for word in ("modal", "kaggle", "H100", "RunPod", "approve abc123", "market_usd", "plan"):
        assert word not in text, word
    seen = status("abc123")
    assert seen["runs"][0]["gpu_minutes"] == 3.0 and "modal" not in str(seen) and "H100" not in str(seen)
    fake.status = "awaiting_approval"
    for answer in (submit("x"), status("abc123")):
        assert "not your question" in answer["note"] and "RunPod" not in str(answer)
    assert "never run `tierhopper approve`" in mcp_server.INSTRUCTIONS.replace("\n", " ") or \
        "never approve a job yourself" in mcp_server.INSTRUCTIONS.replace("\n", " ")


def test_system_packages_a_job_asks_for_are_installed_on_every_provider():
    import inspect

    from tierhopper.adapters import kaggle_adapter, lightning_adapter, modal_adapter, runpod_adapter
    from tierhopper.adapters.base import apt_step

    assert apt_step([]) is None
    assert "apt-get install" in apt_step(["zstd", "aria2"]) and apt_step(["zstd", "aria2"]).count("aria2") == 1
    for module in (kaggle_adapter, lightning_adapter, runpod_adapter):
        assert "apt_step(" in inspect.getsource(module), module.__name__
    assert "spec.apt" in inspect.getsource(modal_adapter)
    script = kaggle_adapter._RUNNER.format(package="", command="'x'", requirements="None", outputs="[]",
                                           runner_config="'{}'", apt=repr(apt_step(["aria2"])))
    compile(script, "run.py", "exec")
    assert script.index("apt-get install") < script.index("COMMAND, shell=True")


def test_a_job_with_its_own_image_only_goes_where_images_run_and_old_jobs_keep_their_history():
    from tierhopper.registry import load_registry
    from tierhopper.routing import plan_route
    from tierhopper.spec import JobSpec

    providers = [p.model_copy(update={"status": "active"}) for p in load_registry()]
    plain = JobSpec(name="a", entrypoint="python x.py")
    own = JobSpec(name="a", entrypoint="python x.py", image="pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime")
    ok, rejected = plan_route(own, providers, {}, allow_paid=True)
    assert [c.provider.id for c in ok] == ["modal"]
    assert any(r.provider_id == "kaggle" and "image" in r.reason for r in rejected)
    assert len(plan_route(plain, providers, {}, allow_paid=True)[0]) > 1
    assert "image" not in plain.model_dump_json(exclude_none=True) and plain.spec_hash() != own.spec_hash()
    import hashlib
    import json
    old = plain.model_dump(exclude={"name", "estimate_hours", "allow_paid", "image"})
    assert plain.spec_hash() == hashlib.sha256(json.dumps(old, sort_keys=True).encode()).hexdigest()[:16]


def test_one_odd_run_does_not_rewrite_the_gpu_speed_table():
    from collections import defaultdict

    from tierhopper.models import Attempt, EndReason, Job, Shard
    from tierhopper.stats import ProviderStats, _learn_speed, static_speed
    from tierhopper.store import MemoryStore

    store = MemoryStore()
    project = store.get_or_create_project("p")
    job = store.create_job(Job(project_id=project.id, name="j", spec={}, spec_hash="h"))
    shard = store.create_shard(Shard(job_id=job.id, idx=0, total=1))

    def run(gpu, seconds):
        return Attempt(shard_id=shard.id, provider_id="modal", gpu_type=gpu, end_reason=EndReason.COMPLETED,
                       progress_start=0.0, progress_end=1.0, gpu_seconds=seconds)

    stats = defaultdict(lambda: ProviderStats(""))
    _learn_speed(store, [run("A100-80GB", 5770)] + [run("H100", 284) for _ in range(6)], stats)
    assert "H100" not in stats["modal"].speed  # the single A100 run is not a baseline: nothing is learned
    stats = defaultdict(lambda: ProviderStats(""))
    _learn_speed(store, [run("A100-80GB", 5770), run("A100-80GB", 5770)] + [run("H100", 284) for _ in range(6)],
                 stats)
    assert stats["modal"].speed["H100"] <= static_speed("H100") * 2  # corrected, never multiplied by six
