from test_service import EXAMPLE, FakeAdapter, make

from tierhopper.models import AttemptStatus


def test_two_schedulers_do_not_hop_twice(tmp_path):
    """The Mac and the cloud tick both see the same failure: only one may relaunch the job."""
    modal = FakeAdapter("modal", [AttemptStatus.FAILED], resets_in_days=1)
    kaggle = FakeAdapter("kaggle", [], resets_in_days=5)
    th, store = make(tmp_path, modal, kaggle)
    job_id = th.submit_job(EXAMPLE)["job_id"]
    job, shard = store.get_job(job_id), store.list_shards(job_id)[0]
    first = store.list_attempts(shard.id)[0]
    stale_a, stale_b = first.model_copy(), first.model_copy()  # each process holds its own copy

    modal.script = [AttemptStatus.FAILED, AttemptStatus.FAILED]
    th._poll_attempt(job, shard, stale_a)
    th._poll_attempt(store.get_job(job_id), shard, stale_b)

    attempts = store.list_attempts(shard.id)
    assert [a.provider_id for a in attempts] == ["modal", "kaggle"]  # exactly one hop
    assert kaggle.submitted == 1


def test_pause_wins_over_a_hop_in_flight(tmp_path):
    modal = FakeAdapter("modal", [AttemptStatus.FAILED], resets_in_days=1)
    kaggle = FakeAdapter("kaggle", [], resets_in_days=5)
    th, store = make(tmp_path, modal, kaggle)
    job_id = th.submit_job(EXAMPLE)["job_id"]
    stale_job, shard = store.get_job(job_id), store.list_shards(job_id)[0]
    attempt = store.list_attempts(shard.id)[0]

    original_finish = store.finish_attempt

    def finish_then_user_pauses(a):
        ok = original_finish(a)
        paused = store.get_job(job_id)
        paused.status = "paused"
        store.update_job(paused)  # the user pauses right after the failure is recorded
        return ok

    store.finish_attempt = finish_then_user_pauses
    th._poll_attempt(stale_job, shard, attempt)
    assert kaggle.submitted == 0 and store.get_job(job_id).status == "paused"


def test_kaggle_script_is_valid_python_without_requirements():
    from tierhopper.adapters.kaggle_adapter import _RUNNER

    script = _RUNNER.format(package="", command=repr("python x.py"), requirements=repr(None),
                            outputs=repr(["results/"]), runner_config=repr("{\"a\": null, \"b\": true}"))
    compile(script, "run.py", "exec")
    assert "REQUIREMENTS = None" in script and "null" not in script.split("RUNNER_CONFIG")[0]


def test_apt_names_are_validated():
    import pytest

    from tierhopper.spec import SpecError, parse_spec

    assert parse_spec({"name": "x", "entrypoint": "a", "apt": ["ffmpeg", "libgl1"]}).apt == ["ffmpeg", "libgl1"]
    with pytest.raises(SpecError):
        parse_spec({"name": "x", "entrypoint": "a", "apt": ["git; rm -rf /"]})


def test_telemetry_casts_and_logs_survive_a_metrics_failure(tmp_path, monkeypatch):
    from tierhopper import ingest

    monkeypatch.setattr(ingest, "ingest_secret", lambda: "k" * 40)
    th, store = make(tmp_path, FakeAdapter("modal", [], 1), FakeAdapter("kaggle", [], 5))
    job_id = th.submit_job(EXAMPLE)["job_id"]
    attempt = store.list_attempts(store.list_shards(job_id)[0].id)[0]
    token = ingest.token_for(attempt.id, "k" * 40)
    th.handle_heartbeat({"attempt_id": attempt.id, "kind": "progress", "gpu_util": 87.0, "vram_used_mb": 1234.0,
                         "vram_total_mb": 15360.0, "lines": ["step 1"]}, token)
    assert store.metrics[-1]["vram_used_mb"] == 1234 and isinstance(store.metrics[-1]["vram_used_mb"], int)

    def broken(*a):
        raise RuntimeError("db said no")

    store.add_metric = broken
    th.handle_heartbeat({"attempt_id": attempt.id, "kind": "progress", "gpu_util": 50.0, "lines": ["step 2"]}, token)
    assert [row["line"] for row in store.log_lines] == ["step 1", "step 2"]


def test_provider_reported_cost_is_used_for_credit(tmp_path):
    from tierhopper.adapters.base import AttemptState

    modal = FakeAdapter("modal", [], 1)
    modal.poll = lambda ref: AttemptState(AttemptStatus.SUCCEEDED, gpu_seconds=30, result={}, billed_usd=0.0525)
    th, store = make(tmp_path, modal, FakeAdapter("kaggle", [], 5))
    job_id = th.submit_job(EXAMPLE)["job_id"]
    th.job_status(job_id)
    attempt = store.list_attempts(store.list_shards(job_id)[0].id)[0]
    assert attempt.market_cost_usd == 0.0525  # not the ~0.005 that 30 s of T4 would suggest


def test_tick_refreshes_stale_credit_snapshots(tmp_path):
    from datetime import timedelta

    from tierhopper.models import now

    th, store = make(tmp_path, FakeAdapter("modal", [], 1), FakeAdapter("kaggle", [], 5))
    th.refresh_stale_credits()
    count = len(store.credits)
    th.tick()
    assert len(store.credits) == count  # fresh: nothing to do
    for snap in store.credits:
        snap.measured_at = now() - timedelta(minutes=30)
    th.tick()
    assert len(store.credits) == count + 2  # both active providers re-read


def test_actual_gpu_is_recorded_and_fallbacks_are_passed(tmp_path, monkeypatch):
    from tierhopper import ingest
    from tierhopper.service import match_gpu

    monkeypatch.setattr(ingest, "ingest_secret", lambda: "k" * 40)
    modal = FakeAdapter("modal", [], 1)
    plans = []
    modal.submit = lambda plan: (plans.append(plan), {"id": plan.attempt_id})[1]
    th, store = make(tmp_path, modal, FakeAdapter("kaggle", [], 5))
    job_id = th.submit_job(EXAMPLE)["job_id"]
    assert plans[0].gpu.type == "H100" and plans[0].gpu_fallbacks[:2] == ["A100-80GB", "A100-40GB"]
    attempt = store.list_attempts(store.list_shards(job_id)[0].id)[0]
    th.handle_heartbeat({"attempt_id": attempt.id, "kind": "started", "gpu_name": "NVIDIA A100-SXM4-40GB",
                         "vram_total_mb": 40960.0}, ingest.token_for(attempt.id, "k" * 40))
    assert store.get_attempt(attempt.id).gpu_type == "A100-40GB"  # Modal had no H100 free
    catalog = store.get_provider("modal").gpu_catalog
    assert match_gpu("Tesla T4", 15360, catalog) == "T4" and match_gpu("NVIDIA L40S", 46068, catalog) == "L40S"
    assert match_gpu("NVIDIA H100 80GB HBM3", 81559, catalog) == "H100" and match_gpu("", None, catalog) is None
