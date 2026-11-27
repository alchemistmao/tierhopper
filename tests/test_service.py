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


class RecordingNotifier:
    def __init__(self):
        self.done, self.failed, self.approvals = [], [], []

    def job_done(self, job, summary=None):
        self.done.append(job.id)
        self.summaries = [*getattr(self, 'summaries', []), summary]

    def job_failed(self, job, message):
        self.failed.append(job.id)

    def approval_needed(self, job, reason, paid):
        self.approvals.append(reason)


def test_finished_work_is_delivered_at_once_even_if_the_provider_errors_afterwards(tmp_path, monkeypatch):
    """The November incident: results saved, then Modal answered 401; the job hopped and asked to pay."""
    from tierhopper import ingest

    monkeypatch.setattr(ingest, "ingest_secret", lambda: "s3cret-value")
    blobs = FakeBlobs()
    modal = FakeAdapter("modal", [AttemptStatus.FAILED], resets_in_days=9)  # the 401 after the work ended
    kaggle = FakeAdapter("kaggle", [], resets_in_days=1)
    th, store = make(tmp_path, modal, kaggle, blobs=blobs)
    th.notifier = RecordingNotifier()
    job_id = th.submit_job(EXAMPLE)["job_id"]
    attempt = next(iter(store.attempts.values()))
    token = ingest.token_for(attempt.id, "s3cret-value")

    blobs.put(f"results/{job_id}/0/outputs.tar.gz", _results_tar("all 480"))  # the runner uploads, then exits
    th.handle_heartbeat({"attempt_id": attempt.id, "kind": "exit", "reason": "completed", "exit_code": 0,
                         "seconds": 166}, token)

    job = store.get_job(job_id)  # done at the heartbeat, before any scheduler pass
    assert job.status.value == "done" and th.notifier.done == [job_id]
    assert len(store.attempts) == 1 and not th.notifier.approvals and not th.notifier.failed
    assert (tmp_path / "results" / job_id / "results" / "out.txt").read_text() == "all 480"


def test_saved_results_are_never_run_again(tmp_path):
    blobs = FakeBlobs()
    modal = FakeAdapter("modal", [], resets_in_days=9)
    kaggle = FakeAdapter("kaggle", [], resets_in_days=1)
    th, store = make(tmp_path, modal, kaggle, blobs=blobs)
    job_id = th.submit_job(EXAMPLE)["job_id"]
    th.pause(job_id)
    blobs.put(f"results/{job_id}/0/outputs.tar.gz", _results_tar("ok"))
    assert th.resume(job_id)["status"] == "done"
    assert len(store.attempts) == 1 and modal.submitted + kaggle.submitted == 1


def test_paid_request_says_what_really_happened():
    from tierhopper.service import paid_reason

    text = paid_reason({"Modal": "the job hit an error there", "Lightning AI": "could not start the job"},
                       "RunPod", 0.0)
    assert "credit ran out" not in text and "Modal: the job hit an error there" in text
    assert text.endswith("Continue on RunPod for less than $0.01?")


def test_morning_check_skips_a_provider_that_stopped_answering_and_brings_it_back(tmp_path):
    class Health(RecordingNotifier):
        def __init__(self):
            super().__init__()
            self.health = []

        def providers_health(self, down, back):
            self.health.append(([d["provider"] for d in down], back))

    modal = FakeAdapter("modal", [], resets_in_days=9)
    kaggle = FakeAdapter("kaggle", [], resets_in_days=1)
    th, store = make(tmp_path, modal, kaggle)
    th.notifier = Health()
    kaggle.validate_credentials = lambda: ValidationResult(False, "403 Forbidden")

    rows = {r["provider"]: r for r in th.check_providers(retry_after_s=0)}
    assert rows["modal"]["ok"] and not rows["kaggle"]["ok"]
    assert rows["kaggle"]["fix"] == "tierhopper connect kaggle"
    assert store.get_provider("kaggle").status == ProviderStatus.UNAVAILABLE
    assert th.notifier.health == [(["kaggle"], [])]
    plan = th.submit_job(EXAMPLE, dry_run=True)
    assert all(c["provider"] != "kaggle" for c in plan["plan"])

    th.check_providers(retry_after_s=0)  # still down: no second message
    assert len(th.notifier.health) == 1
    kaggle.validate_credentials = lambda: ValidationResult(True, "ok")
    th.check_providers(retry_after_s=0)
    assert store.get_provider("kaggle").status == ProviderStatus.ACTIVE
    assert th.notifier.health[-1] == ([], ["Kaggle"])


def test_a_provider_that_refuses_a_job_is_checked_at_once(tmp_path):
    modal = FakeAdapter("modal", [], resets_in_days=9, fail_submit=True)
    kaggle = FakeAdapter("kaggle", [], resets_in_days=1)
    th, store = make(tmp_path, modal, kaggle)
    modal.validate_credentials = lambda: ValidationResult(False, "token revoked")
    th.submit_job(EXAMPLE)
    assert store.get_provider("modal").status == ProviderStatus.UNAVAILABLE
    assert kaggle.submitted == 1


def test_a_crash_reported_by_the_runner_ends_the_attempt_even_if_the_provider_never_answers(tmp_path, monkeypatch):
    from tierhopper import ingest

    monkeypatch.setattr(ingest, "ingest_secret", lambda: "s3cret-value")
    modal = FakeAdapter("modal", [], resets_in_days=9)  # polls always say "running" (the scheduler's 401)
    kaggle = FakeAdapter("kaggle", [], resets_in_days=1)
    th, store = make(tmp_path, modal, kaggle, blobs=FakeBlobs())
    th.submit_job(EXAMPLE)
    first = next(iter(store.attempts.values()))
    th.handle_heartbeat({"attempt_id": first.id, "kind": "exit", "reason": "error", "exit_code": 1,
                         "seconds": 30, "tail": ["ValueError: boom"]},
                        ingest.token_for(first.id, "s3cret-value"))
    th.tick()
    attempts = sorted(store.attempts.values(), key=lambda a: a.started_at)
    assert attempts[0].status.terminal and attempts[0].end_reason == EndReason.ERROR
    assert len(attempts) == 2  # moved on to the other free GPU right away


def test_the_done_message_says_how_long_how_much_where_and_what_it_cost(tmp_path):
    from tierhopper.models import Job
    from tierhopper.notify import done_text, duration

    summary = {"wall_seconds": 4380, "gpu_seconds": 3300, "ran_on": ["Kaggle · T4x2", "Modal · H100"], "attempts": 2,
               "parts": 1, "work": ["601 of 601 items"], "spent_usd": 0.0, "saved_usd": 1.84}
    job = Job(project_id="p", name="eval-large", spec={}, spec_hash="h")
    text = done_text(job, summary)
    assert "601 of 601 items" in text and "1 h 13 min from send to finish (55 min on the GPU)" in text
    assert "Kaggle · T4x2 → Modal · H100 (2 attempts)" in text and "free · about $1.84 saved" in text
    assert f"tierhopper fetch {job.id}" in text
    assert duration(42) == "42 s" and duration(600) == "10 min"
    assert done_text(job, {}).splitlines()[:2] == ["🟢 TierHopper · Done", "eval-large"]  # never fails on missing data


def _queued_on_modal(tmp_path, monkeypatch, kaggle):
    from tierhopper import credentials, ingest

    monkeypatch.setattr(ingest, "ingest_secret", lambda: "s3cret-value")
    monkeypatch.setattr(credentials, "get_secret", lambda *a, **k: "https://ingest.example")
    modal = FakeAdapter("modal", [], resets_in_days=9)  # accepted the job, never gives it a machine
    modal.start_deadline = timedelta(minutes=5)
    modal.cancelled = 0
    modal.cancel = lambda ref: setattr(modal, "cancelled", modal.cancelled + 1)
    th, store = make(tmp_path, modal, kaggle, blobs=FakeBlobs())
    th.submit_job(EXAMPLE)
    first = next(iter(store.attempts.values()))
    assert first.provider_id == "modal"
    return th, store, modal, first


def test_a_job_left_in_the_modal_queue_for_five_minutes_moves_to_another_free_gpu(tmp_path, monkeypatch):
    kaggle = FakeAdapter("kaggle", [], resets_in_days=1)
    th, store, modal, first = _queued_on_modal(tmp_path, monkeypatch, kaggle)
    th.tick()
    assert len(store.attempts) == 1 and not modal.cancelled  # two seconds in: still waiting on Modal
    first.started_at -= timedelta(minutes=6)
    store.update_attempt(first)
    th.tick()
    attempts = sorted(store.attempts.values(), key=lambda a: a.started_at)
    assert modal.cancelled == 1 and attempts[0].status.terminal and "no GPU" in attempts[0].message
    assert [a.provider_id for a in attempts] == ["modal", "kaggle"]
    assert attempts[0].cost_usd == 0 and attempts[0].gpu_seconds == 0


def test_a_job_that_already_started_on_modal_is_never_moved_for_being_slow(tmp_path, monkeypatch):
    from tierhopper import ingest

    kaggle = FakeAdapter("kaggle", [], resets_in_days=1)
    th, store, modal, first = _queued_on_modal(tmp_path, monkeypatch, kaggle)
    th.handle_heartbeat({"attempt_id": first.id, "kind": "started"}, ingest.token_for(first.id, "s3cret-value"))
    running = store.get_attempt(first.id)
    running.started_at -= timedelta(minutes=6)
    store.update_attempt(running)
    th.tick()
    assert len(store.attempts) == 1 and not modal.cancelled


def test_with_no_other_free_gpu_the_job_keeps_its_place_in_the_modal_queue(tmp_path, monkeypatch):
    kaggle = FakeAdapter("kaggle", [], resets_in_days=1)
    th, store, modal, first = _queued_on_modal(tmp_path, monkeypatch, kaggle)
    store.upsert_provider(store.get_provider("kaggle").model_copy(update={"status": ProviderStatus.UNAVAILABLE}))
    kaggle.validate_credentials = lambda: ValidationResult(False, "down")
    first.started_at -= timedelta(minutes=6)
    store.update_attempt(first)
    th.tick()
    assert len(store.attempts) == 1 and not modal.cancelled


def test_a_job_whose_cost_does_not_fit_the_free_dollars_left_never_goes_to_that_provider(tmp_path):

    modal = FakeAdapter("modal", [], resets_in_days=9)
    kaggle = FakeAdapter("kaggle", [], resets_in_days=1)
    left = {"usd": 1.02}  # two cents above the reserve: less than even this tiny job costs
    modal.credit_remaining = lambda provider, used_usd=0.0: CreditReading(left["usd"], CreditUnit.USD,
                                                                          CreditSource.API)
    th, store = make(tmp_path, modal, kaggle)
    got = th.submit_job(EXAMPLE, dry_run=True)
    assert [c["provider"] for c in got["plan"]] == ["kaggle"]
    assert "does not cover" in got["not_used"]["modal"]
    left["usd"] = 25.0
    assert th.submit_job(EXAMPLE, dry_run=True)["plan"][0]["provider"] == "modal"


def test_two_jobs_do_not_count_on_the_same_free_dollars(tmp_path):
    from tierhopper.routing import job_cost
    from tierhopper.spec import load_spec

    modal = FakeAdapter("modal", [], resets_in_days=9)
    kaggle = FakeAdapter("kaggle", [], resets_in_days=1)
    th, store = make(tmp_path, modal, kaggle)
    spec, _ = load_spec(EXAMPLE)
    fastest = max(store.get_provider("modal").gpu_catalog, key=lambda g: g.usd_per_hour)
    one = job_cost(fastest, spec, None, spec.estimate_hours)
    budget = one * 1.5 + 1.0  # room for one job on the fastest GPU plus the reserve, not for two
    modal.credit_remaining = lambda provider, used_usd=0.0: CreditReading(budget, CreditUnit.USD, CreditSource.API)
    first = th.submit_job(EXAMPLE)
    assert first["providers"] == ["modal"]
    assert th._committed()["modal"] > 0
    second = th.submit_job(EXAMPLE, dry_run=True)
    again = [c["gpu"] for c in second["plan"] if c["provider"] == "modal"]
    assert fastest.type not in again  # what is left no longer pays for a second run on the fastest GPU


def test_a_provider_is_active_only_when_its_login_works_in_every_place(tmp_path, monkeypatch):
    modal = FakeAdapter("modal", [], resets_in_days=9)
    kaggle = FakeAdapter("kaggle", [], resets_in_days=1)
    th, store = make(tmp_path, modal, kaggle)
    kaggle.validate_credentials = lambda: ValidationResult(False, "the API token is missing or not valid")
    rows = th.check_providers(["kaggle"], retry_after_s=0)
    assert rows[0]["not_working_on"] == ["this computer"]
    assert store.get_provider("kaggle").status == ProviderStatus.UNAVAILABLE
    monkeypatch.setenv("MODAL_TASK_ID", "ta-1")  # the cloud scheduler has a working copy of the login
    kaggle.validate_credentials = lambda: ValidationResult(True, "ok")
    th.check_providers(["kaggle"], retry_after_s=0)
    p = store.get_provider("kaggle")
    assert p.health["cloud"]["ok"] and not p.health["this computer"]["ok"]
    assert p.status == ProviderStatus.UNAVAILABLE  # still broken on the computer: not shown as working
    monkeypatch.delenv("MODAL_TASK_ID")
    th.check_providers(["kaggle"], retry_after_s=0)
    assert store.get_provider("kaggle").status == ProviderStatus.ACTIVE


def test_modal_balance_is_the_official_number_and_goes_below_zero_when_the_card_is_billed():
    from tierhopper.adapters.modal_adapter import credit_left

    november = {"metered_cost": "34.10241326", "billed_cost": "3.35000000",
                "adjustments": {"credits": "-30.00000000", "free_storage": "-0.54944111", "plan_cost": "0E-8",
                                "network_egress_allowance": "-0.20297215"}}
    assert credit_left(november) == -3.35
    assert credit_left({"metered_cost": "4.5", "adjustments": {"credits": "-30"}}) == 25.5


def test_starting_tierhopper_again_keeps_what_each_place_knows_about_a_provider(tmp_path):
    from tierhopper.registry import sync_registry

    modal = FakeAdapter("modal", [], resets_in_days=9)
    kaggle = FakeAdapter("kaggle", [], resets_in_days=1)
    th, store = make(tmp_path, modal, kaggle)
    kaggle.validate_credentials = lambda: ValidationResult(False, "no token")
    th.check_providers(["kaggle"], retry_after_s=0)
    sync_registry(store)  # what every new process does when it starts
    p = store.get_provider("kaggle")
    assert p.status == ProviderStatus.UNAVAILABLE and p.health["this computer"]["ok"] is False


def test_the_paid_question_says_why_each_free_gpu_cannot_take_the_job():
    from types import SimpleNamespace as N

    from tierhopper.models import CreditSnapshot, ProviderKind

    free = dict(kind=ProviderKind.FREE, status=ProviderStatus.ACTIVE)
    providers = [N(id="kaggle", name="Kaggle", **free), N(id="modal", name="Modal", **free),
                 N(id="lightning", name="Lightning AI", kind=ProviderKind.CREDIT_PROGRAM,
                   status=ProviderStatus.UNAVAILABLE),
                 N(id="runpod", name="RunPod", kind=ProviderKind.PAID, status=ProviderStatus.ACTIVE)]
    rejected = [N(provider_id="kaggle", reason="no GPU with 40 GB+"),
                N(provider_id="modal", reason="free credit used up"),
                N(provider_id="lightning", reason="not answering (failed its health check)"),
                N(provider_id="runpod", reason="paid provider needs your approval")]
    credits = {"modal": CreditSnapshot(provider_id="modal", remaining=-3.4, unit=CreditUnit.USD,
                                       source=CreditSource.API, expires_at=datetime(2026, 12, 1, tzinfo=UTC))}
    why = TierHopper._why_not_free(rejected, providers, credits)
    assert why == {"Kaggle": "has no GPU with 40 GB+", "Modal": "has no free credit left until Dec 1",
                   "Lightning AI": "is not working right now"}
    from tierhopper.service import paid_reason
    text = paid_reason(why, "RunPod", 8.22)
    assert "Kaggle: has no GPU with 40 GB+" in text and "RunPod for about $8.22" in text
