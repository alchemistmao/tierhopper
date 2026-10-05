from test_service import EXAMPLE, FakeAdapter, FakeBlobs, make

from tierhopper import ingest
from tierhopper.models import AttemptStatus
from tierhopper.storage_r2 import Keys


def test_job_logs_live_then_full_log(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "ingest_secret", lambda: "k" * 40)
    blobs = FakeBlobs()
    modal = FakeAdapter("modal", [AttemptStatus.FAILED], 1)
    kaggle = FakeAdapter("kaggle", [], 5)
    th, store = make(tmp_path, modal, kaggle, blobs=blobs)
    job_id = th.submit_job(EXAMPLE)["job_id"]
    first = store.list_attempts(store.list_shards(job_id)[0].id)[0]
    token = ingest.token_for(first.id, "k" * 40)
    th.handle_heartbeat({"attempt_id": first.id, "kind": "progress",
                         "lines": ["loading", "vllm did not start: AssertionError", "page 1 of 9"]}, token)

    live = th.job_logs(job_id, contains="VLLM")["attempts"][0]
    assert live["source"] == "live" and live["lines"] == ["vllm did not start: AssertionError"]

    blobs.put(Keys.log(first.id), b"line a\nline b\nTraceback: boom\n")
    th.job_status(job_id)  # modal fails -> hop to kaggle
    attempts = th.job_logs(job_id, lines=2)["attempts"]
    assert attempts[0]["source"] == "full log" and attempts[0]["lines"] == ["line b", "Traceback: boom"]
    assert attempts[1]["provider"] == "kaggle" and attempts[1]["lines"] == []


def test_cancel_stops_attempts_and_is_final(tmp_path):
    modal = FakeAdapter("modal", [], 1)
    th, store = make(tmp_path, modal, FakeAdapter("kaggle", [], 5))
    job_id = th.submit_job(EXAMPLE)["job_id"]
    assert th.cancel(job_id)["status"] == "cancelled"
    assert not store.list_active_attempts()
    assert th.tick()["dispatched"] == 0 and th.resume(job_id)["status"] == "cancelled"


def test_list_jobs_finds_ids(tmp_path):
    th, store = make(tmp_path, FakeAdapter("modal", [], 1), FakeAdapter("kaggle", [], 5))
    job_id = th.submit_job(EXAMPLE)["job_id"]
    rows = th.list_jobs()["jobs"]
    assert rows[0]["job_id"] == job_id and rows[0]["status"] == "running" and rows[0]["name"] == "hello-gpu"
    assert th.list_jobs(status="done")["jobs"] == []
