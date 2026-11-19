from test_service import EXAMPLE, FakeAdapter, make

from tierhopper import ingest
from tierhopper.progress import parse_counter


def test_counters_in_real_world_lines():
    assert parse_counter(["detection: página 37 de 150 (id abc): 12.3s"]) == {"done": 37, "total": 150, "unit": "pages"}
    assert parse_counter(["Processed 12 of 80 documents"]) == {"done": 12, "total": 80, "unit": "documents"}
    assert parse_counter(["Epoch 3/10 loss=0.21"]) == {"done": 3, "total": 10, "unit": "epochs"}
    assert parse_counter([" 45%|████▌     | 450/1000 [01:02<01:15,  7.2it/s]"]) == {
        "done": 450, "total": 1000, "unit": "items"}
    assert parse_counter(["page 2 of 9", "page 3 of 9"])["done"] == 3  # the latest line wins


def test_lines_that_are_not_progress():
    assert parse_counter(["aspect ratio 16/9", "saved 2026/10/01", "version 3/2", "date 01/10"]) is None
    assert parse_counter(["torch 2.8.0 loaded", "GPU 0: Tesla T4"]) is None
    assert parse_counter(["step 200 of 150"]) is None  # done > total


def test_heartbeat_lines_drive_progress_and_label(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "ingest_secret", lambda: "k" * 40)
    th, store = make(tmp_path, FakeAdapter("modal", [], 1), FakeAdapter("kaggle", [], 5))
    job_id = th.submit_job(EXAMPLE)["job_id"]
    attempt = store.list_attempts(store.list_shards(job_id)[0].id)[0]
    token = ingest.token_for(attempt.id, "k" * 40)
    th.handle_heartbeat({"attempt_id": attempt.id, "kind": "progress",
                         "lines": ["loading model", "detection: página 30 de 150 (id x): 9s"]}, token)
    job = store.get_job(job_id)
    assert job.progress == 0.2
    assert job.estimate["counters"]["0"] == {"done": 30, "total": 150, "unit": "pages"}
    assert store.metrics[-1]["progress"] == 0.2  # the pace history the ETA is computed from


def test_named_unit_beats_a_later_generic_bar():
    lines = ["página 3 de 300 (detection)", "Fetching 12 files:  67%|██████▋   | 8/12 [00:01<00:00]"]
    assert parse_counter(lines) == {"done": 3, "total": 300, "unit": "pages"}


def test_download_bars_do_not_become_progress_when_the_job_reports_its_own(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "ingest_secret", lambda: "k" * 40)
    th, store = make(tmp_path, FakeAdapter("modal", [], 1), FakeAdapter("kaggle", [], 5))
    job_id = th.submit_job(EXAMPLE)["job_id"]
    job = store.get_job(job_id)
    job.spec["progress_file"] = ".progress"
    store.update_job(job)
    attempt = store.list_attempts(store.list_shards(job_id)[0].id)[0]
    token = ingest.token_for(attempt.id, "k" * 40)
    th.handle_heartbeat({"attempt_id": attempt.id, "kind": "progress",
                         "lines": ["Fetching 927 files:   1%|          | 8/927 [00:00<00:40]"]}, token)
    job = store.get_job(job_id)
    assert job.progress == 0 and not job.estimate.get("counters")
    th.handle_heartbeat({"attempt_id": attempt.id, "kind": "progress", "progress": 0.01,
                         "lines": ["página 3 de 300 (detection, id 1): 40s"]}, token)
    job = store.get_job(job_id)
    assert job.progress == 0.01 and job.estimate["counters"]["0"]["unit"] == "pages"


def test_setup_bars_are_not_the_job_progress():
    assert parse_counter(["Loading safetensors checkpoint shards: 100% Completed | 5/5 [00:11<00:00]"]) is None
    assert parse_counter(["Capturing CUDA graphs (FULL): 100%|██████████| 51/51 [00:01<00:00, 38.00it/s]"]) is None
    assert parse_counter(["Downloading model.safetensors: 100%|██| 12/12 [00:30<00:00]"]) is None
    assert parse_counter(["Processed prompts:  10%|█  | 60/600 [00:09<01:20]"])["total"] == 600


def test_a_running_job_never_shows_100_percent_and_follows_its_latest_bar(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "ingest_secret", lambda: "k" * 40)
    th, store = make(tmp_path, FakeAdapter("modal", [], 1), FakeAdapter("kaggle", [], 5))
    job_id = th.submit_job(EXAMPLE)["job_id"]
    attempt = store.list_attempts(store.list_shards(job_id)[0].id)[0]
    token = ingest.token_for(attempt.id, "k" * 40)

    def beat(line):
        th.handle_heartbeat({"attempt_id": attempt.id, "kind": "progress", "lines": [line]}, token)
        return store.get_job(job_id).progress

    assert beat("Warm-up: 100%|██████████| 20/20 [00:11<00:00,  2.35s/it]") == 0.99  # a first bar that fills up
    assert beat("Processed prompts:   0%|          | 0/600 [00:00<?, ?it/s]") == 0  # the real work starts
    assert beat("Processed prompts:  50%|█████     | 300/600 [01:00<01:00]") == 0.5
    assert store.get_job(job_id).status == "running"
