from types import SimpleNamespace

from test_service import EXAMPLE, FakeAdapter, FakeBlobs, make

from tierhopper import command


class FakeClaude:
    """Stands in for anthropic.Anthropic: returns a canned content list and records the request."""

    def __init__(self, content, stop_reason="end_turn"):
        self.content, self.stop_reason, self.request = content, stop_reason, None
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.request = kwargs
        return SimpleNamespace(content=self.content, stop_reason=self.stop_reason)


SNAP = {"projects": [{"id": "j1", "name": "layout-eval", "status": "running", "progress": 0.4}], "providers": []}


def test_tool_call_becomes_a_proposal_that_needs_confirmation():
    fake = FakeClaude([SimpleNamespace(type="tool_use", name="pause_project", input={"job_id": "j1"})])
    out = command.plan_command("pause the layout one", SNAP, client=fake)
    assert out == {"kind": "action", "action": "pause_project", "args": {"job_id": "j1"},
                   "summary": "Pause “layout-eval”. Its progress is kept.", "confirm": True}
    assert all(t["strict"] and t["input_schema"]["additionalProperties"] is False for t in fake.request["tools"])
    assert fake.request["tool_choice"]["type"] == "auto"  # forced tool use is rejected by current models


def test_unknown_project_id_is_never_acted_on():
    fake = FakeClaude([SimpleNamespace(type="tool_use", name="pause_project", input={"job_id": "made-up"})])
    assert command.plan_command("pause it", SNAP, client=fake)["kind"] == "answer"


def test_questions_get_a_plain_answer_and_navigation_needs_no_confirmation():
    fake = FakeClaude([SimpleNamespace(type="text", text="One project is running at 40%.")])
    assert command.plan_command("what is running?", SNAP, client=fake) == {
        "kind": "answer", "text": "One project is running at 40%."}
    nav = FakeClaude([SimpleNamespace(type="tool_use", name="navigate", input={"tab": "providers"})])
    assert command.plan_command("show providers", SNAP, client=nav)["confirm"] is False


def test_refusal_is_handled():
    fake = FakeClaude([], stop_reason="refusal")
    assert command.plan_command("x", SNAP, client=fake)["kind"] == "answer"


def test_rerun_creates_a_new_job_with_overrides(tmp_path):
    modal = FakeAdapter("modal", [], 1)
    kaggle = FakeAdapter("kaggle", [], 5)
    th, store = make(tmp_path, modal, kaggle, blobs=FakeBlobs())
    first = th.submit_job(EXAMPLE)["job_id"]
    out = th.dashboard_action(first, "rerun", {"min_vram_gb": 24, "cheapest": True})
    assert out["job_id"] != first
    new = store.get_job(out["job_id"])
    assert new.spec["gpu"]["min_vram_gb"] == 24
    attempts = [a for a in store.attempts.values() if a.shard_id == store.list_shards(new.id)[0].id]
    assert attempts and attempts[0].gpu_type not in ("T4", "T4x2")  # 16 GB no longer fits
    vram = next(g.vram_gb for g in store.get_provider(attempts[0].provider_id).gpu_catalog
                if g.type == attempts[0].gpu_type)
    assert vram >= 24


def test_snapshot_has_no_secrets_or_paths(tmp_path):
    th, store = make(tmp_path, FakeAdapter("modal", [], 1), FakeAdapter("kaggle", [], 5))
    th.submit_job(EXAMPLE)
    snap = th.command_snapshot()
    text = str(snap)
    assert "/Users/" not in text and "_root" not in text and "api_key" not in text
    assert snap["projects"][0]["name"] == "hello-gpu"


def test_notifier_fans_out_to_push_and_whatsapp(tmp_path):
    from tierhopper.models import AttemptStatus
    from tierhopper.notify import Notifier, WhatsApp

    wa, pushes = [], []
    modal = FakeAdapter("modal", [AttemptStatus.SUCCEEDED], 1)
    th, store = make(tmp_path, modal, FakeAdapter("kaggle", [], 5))
    th.notifier = Notifier(store, whatsapp=WhatsApp(sender=wa.append),
                           push_sender=lambda s, title, body, url: pushes.append((title, url)))
    job_id = th.submit_job(EXAMPLE)["job_id"]
    th.job_status(job_id)
    assert wa and "Done" in wa[0]
    assert pushes == [("Done", f"/jobs/{job_id}")]


def test_push_drops_revoked_subscriptions(tmp_path, monkeypatch):
    import pywebpush

    from tierhopper import push

    monkeypatch.setattr(push.credentials, "get_secret", lambda p, f: "k" if f == "vapid_private" else None)
    th, store = make(tmp_path, FakeAdapter("modal", [], 1), FakeAdapter("kaggle", [], 5))
    store.push_subscriptions = [{"endpoint": "https://gone", "keys": {}}, {"endpoint": "https://ok", "keys": {}}]

    def fake_webpush(subscription_info, **kwargs):
        if subscription_info["endpoint"] == "https://gone":
            raise pywebpush.WebPushException("gone", response=SimpleNamespace(status_code=410))
        return None

    monkeypatch.setattr(pywebpush, "webpush", fake_webpush)
    assert push.send_all(store, "Done", "x") == 1
    assert [s["endpoint"] for s in store.push_subscriptions] == ["https://ok"]
