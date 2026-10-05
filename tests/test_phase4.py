from test_service import EXAMPLE, FakeAdapter, make

from tierhopper import actions, credentials
from tierhopper.models import AttemptStatus
from tierhopper.notify import WhatsApp, integration_card, weekly_text


def test_action_tokens_sign_and_expire():
    tok = actions.make_token("approve", "job-1", "k" * 32, now=1000)
    assert actions.read_token(tok, "k" * 32, now=1500)["j"] == "job-1"
    assert actions.read_token(tok, "k" * 32, now=1000 + 1801) is None  # 30 min
    assert actions.read_token(tok, "other-key", now=1500) is None
    body, sig = tok.split(".")
    assert actions.read_token(body + "." + sig[::-1], "k" * 32, now=1500) is None


def test_card_has_link_steps_and_command(tmp_path):
    th, store = make(tmp_path, FakeAdapter("modal", [], 1), FakeAdapter("kaggle", [], 5))
    text = integration_card(store.get_provider("kaggle"))
    assert "https://www.kaggle.com/account/login" in text
    assert "verify your phone" in text
    assert text.strip().endswith("Run: tierhopper connect kaggle")
    assert 3 <= sum(1 for line in text.splitlines() if line[:2].rstrip(".").isdigit()) <= 5


def test_whatsapp_messages_are_redacted():
    sent = []
    credentials.redact.register("super-secret-token-xyz")
    WhatsApp(sender=sent.append).send("token super-secret-token-xyz leaked?")
    assert "super-secret" not in sent[0]


def test_get_never_acts_post_does(tmp_path, monkeypatch):
    monkeypatch.setenv("TIERHOPPER_APPROVAL_GPU_HOURS", "0.01")
    monkeypatch.setattr(actions, "secret", lambda: "a" * 64)
    th, store = make(tmp_path, FakeAdapter("modal", [], 1), FakeAdapter("kaggle", [], 5))
    job_id = th.submit_job(EXAMPLE)["job_id"]
    tok = actions.make_token("approve", job_id, "a" * 64)
    code, html = th.handle_action(tok, "GET")
    assert code == 200 and "<form" in html
    assert store.get_job(job_id).status == "awaiting_approval"
    code, html = th.handle_action(tok, "POST")
    assert store.get_job(job_id).status == "running"
    assert th.handle_action("garbage", "POST")[0] == 403


def test_usage_report_and_weekly_text(tmp_path):
    modal = FakeAdapter("modal", [AttemptStatus.FAILED], 1)
    kaggle = FakeAdapter("kaggle", [AttemptStatus.SUCCEEDED], 5)
    th, store = make(tmp_path, modal, kaggle)
    job_id = th.submit_job(EXAMPLE)["job_id"]
    th.job_status(job_id)
    th.job_status(job_id)
    report = th.usage_report("week")
    assert report["totals"]["jobs_done"] == 1 and report["totals"]["hops"] == 1
    assert report["totals"]["saved_usd"] >= 0
    assert "GPU time" in weekly_text(report)


def test_notifier_called_on_done(tmp_path):
    sent = []
    modal = FakeAdapter("modal", [AttemptStatus.SUCCEEDED], 1)
    th, store = make(tmp_path, modal, FakeAdapter("kaggle", [], 5))
    th.notifier = WhatsApp(sender=sent.append)
    job_id = th.submit_job(EXAMPLE)["job_id"]
    th.job_status(job_id)
    assert sent and "Done" in sent[0] and job_id in sent[0]
