from test_service import EXAMPLE, FakeAdapter, make

from tierhopper import ingest
from tierhopper.models import Attempt, AttemptStatus, EndReason, ProviderStatus, now


class PodAdapter(FakeAdapter):
    terminates_on_exit = False

    def __init__(self):
        super().__init__("runpod", [], resets_in_days=30)
        self.cancelled = 0

    def submit(self, plan):
        return {"pod_id": "p1", "cost_per_hr": 0.5}

    def poll(self, ref):
        from tierhopper.adapters.base import AttemptState

        return AttemptState(AttemptStatus.RUNNING)  # a pod keeps running after the runner exits

    def cancel(self, ref):
        self.cancelled += 1


def _paid_setup(tmp_path, monkeypatch):
    import tierhopper.routing as routing

    monkeypatch.setattr(routing, "IMPLEMENTED", routing.IMPLEMENTED | {"runpod"})
    monkeypatch.setattr(ingest, "ingest_secret", lambda: "k" * 40)
    modal = FakeAdapter("modal", [], 1, fail_submit=True)
    kaggle = FakeAdapter("kaggle", [], 5, fail_submit=True)
    pod = PodAdapter()
    th, store = make(tmp_path, modal, kaggle)
    th.adapter_factory = {"modal": modal, "kaggle": kaggle, "runpod": pod}.__getitem__
    rp = store.get_provider("runpod")
    store.upsert_provider(rp.model_copy(update={"status": ProviderStatus.ACTIVE, "adapter": "runpod"}))
    return th, store, pod


def test_pod_terminated_when_runner_reports_exit(tmp_path, monkeypatch):
    th, store, pod = _paid_setup(tmp_path, monkeypatch)
    job_id = th.submit_job(EXAMPLE)["job_id"]
    th.approve(job_id)
    attempt = next(a for a in store.attempts.values() if a.provider_id == "runpod")
    th.job_status(job_id)
    assert pod.cancelled == 0  # still working
    token = ingest.token_for(attempt.id, "k" * 40)
    th.handle_heartbeat({"attempt_id": attempt.id, "kind": "exit", "reason": "completed", "seconds": 120}, token)
    status = th.job_status(job_id)
    assert pod.cancelled == 1 and status["status"] == "done"
    a = store.get_attempt(attempt.id)
    assert a.cost_usd > 0  # billed wall time at the pod's hourly rate


def test_spend_cap_blocks_paid_proposal(tmp_path, monkeypatch):
    th, store, pod = _paid_setup(tmp_path, monkeypatch)
    store.create_attempt(Attempt(shard_id="old", provider_id="runpod", gpu_type="L4", status=AttemptStatus.SUCCEEDED,
                                 end_reason=EndReason.COMPLETED, cost_usd=20.0, started_at=now()))
    job_id = th.submit_job(EXAMPLE)["job_id"]
    job = store.get_job(job_id)
    assert job.status == "queued"  # waits for free credit; never proposes going over the cap
    assert any(e.type == "spend_cap" for e in store.events)


def test_tick_survives_a_broken_job(tmp_path):
    modal = FakeAdapter("modal", [], 1)
    kaggle = FakeAdapter("kaggle", [], 5)
    th, store = make(tmp_path, modal, kaggle)
    bad = th.submit_job(EXAMPLE)["job_id"]
    good = th.submit_job(EXAMPLE)["job_id"]
    bad_shard = store.list_shards(bad)[0].id
    real_poll = th._poll_attempt

    def poll(job, shard, attempt, hop=True):
        if shard.id == bad_shard:
            raise RuntimeError("provider API key missing")
        return real_poll(job, shard, attempt, hop)

    th._poll_attempt = poll
    result = th.tick()
    assert result["errors"] == 1 and result["polled"] >= 1
    assert store.get_job(good).status == "running"


def test_local_poll_sees_runner_exit_recorded_by_the_cloud(tmp_path, monkeypatch):
    th, store, pod = _paid_setup(tmp_path, monkeypatch)
    job_id = th.submit_job(EXAMPLE)["job_id"]
    th.approve(job_id)
    attempt = next(a for a in store.attempts.values() if a.provider_id == "runpod")
    stale = attempt.model_copy()  # what a long-running local loop holds in memory
    token = ingest.token_for(attempt.id, "k" * 40)
    th.handle_heartbeat({"attempt_id": attempt.id, "kind": "exit", "reason": "completed", "seconds": 30}, token)
    job, shard = store.get_job(job_id), store.list_shards(job_id)[0]
    th._poll_attempt(job, shard, stale, hop=False)
    assert pod.cancelled == 1 and stale.status == AttemptStatus.SUCCEEDED
