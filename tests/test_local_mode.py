"""Local mode: the same service scenarios on the SQLite store a fresh install uses."""

from pathlib import Path

from test_service import EXAMPLE, FakeAdapter

from tierhopper.models import AttemptStatus, ProviderStatus
from tierhopper.service import TierHopper
from tierhopper.store.sqlite_store import SQLiteStore


def make_local(tmp_path, modal, kaggle):
    store = SQLiteStore(tmp_path / "state.db")
    th = TierHopper(store, adapter_factory={"modal": modal, "kaggle": kaggle}.__getitem__,
                    results_dir=tmp_path / "results", packages_dir=tmp_path / "pkgs", blobs=None)
    for pid in ("modal", "kaggle"):
        p = store.get_provider(pid)
        store.upsert_provider(p.model_copy(update={"status": ProviderStatus.ACTIVE}))
    return th, store


def test_registry_ships_inside_the_package():
    import tierhopper

    root = Path(tierhopper.__file__).parent
    assert (root / "providers.yaml").is_file() and (root / "skill" / "SKILL.md").is_file()
    assert (root / "smoke" / "tierhopper.yaml").is_file() and (root / "runner_script.py").is_file()


def test_job_runs_hops_and_survives_a_restart(tmp_path):
    modal = FakeAdapter("modal", [AttemptStatus.FAILED], resets_in_days=1)
    kaggle = FakeAdapter("kaggle", [AttemptStatus.RUNNING, AttemptStatus.SUCCEEDED], resets_in_days=5)
    th, store = make_local(tmp_path, modal, kaggle)
    job_id = th.submit_job(EXAMPLE)["job_id"]
    th.job_status(job_id)  # modal fails -> hop to kaggle

    # a new process (e.g. the MCP server restarting) opens the same file and carries on
    th2 = TierHopper(SQLiteStore(tmp_path / "state.db"), adapter_factory={"modal": modal, "kaggle": kaggle}.__getitem__,
                     results_dir=tmp_path / "results", packages_dir=tmp_path / "pkgs", blobs=None, sync=False)
    assert th2.list_jobs()["jobs"][0]["job_id"] == job_id
    th2.job_status(job_id)
    status = th2.job_status(job_id)
    assert status["status"] == "done" and [s["provider"] for s in status["stops"]] == ["modal", "kaggle"]
    assert (tmp_path / "results" / job_id / "result.json").exists()
    assert th2.usage_report("week")["totals"]["jobs_done"] == 1


def test_only_one_process_finishes_an_attempt(tmp_path):
    store = SQLiteStore(tmp_path / "state.db")
    other = SQLiteStore(tmp_path / "state.db")
    from tierhopper.models import Attempt

    a = store.create_attempt(Attempt(shard_id="s", provider_id="kaggle", gpu_type="T4x2"))
    mine, theirs = a.model_copy(update={"status": AttemptStatus.FAILED}), a.model_copy(
        update={"status": AttemptStatus.FAILED})
    assert store.finish_attempt(mine) is True
    assert other.finish_attempt(theirs) is False
    assert not other.list_active_attempts()


def test_logs_events_and_credit_roundtrip(tmp_path):
    from tierhopper.models import CreditSnapshot, Event

    store = SQLiteStore(tmp_path / "state.db")
    store.add_log_lines("a1", ["one", "two", "three"])
    assert store.list_log_lines("a1", limit=2) == ["two", "three"]
    store.add_event(Event(job_id="j", type="hop", payload={"reason": "error"}))
    assert store.list_events("j")[0].payload == {"reason": "error"}
    store.add_credit_snapshot(CreditSnapshot(provider_id="kaggle", remaining=12.5, unit="gpu_hours", source="api"))
    assert store.latest_credit("kaggle").remaining == 12.5 and store.latest_credit("modal") is None
