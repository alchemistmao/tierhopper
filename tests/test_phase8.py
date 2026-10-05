from types import SimpleNamespace

from test_service import FakeAdapter, make

from tierhopper import discovery

GOOD = {"name": "NovaGPU", "url": "https://novagpu.example/pricing", "summary": "$20 free credit monthly",
        "credit_type": "monthly_usd", "credit_amount": 20, "gpu_types": ["T4"], "official_api": "yes",
        "headless_jobs": "yes", "api_docs_url": "https://novagpu.example/docs", "tos_allows_automation": "yes",
        "requires_phone": True, "requires_card": False, "confidence": 0.9,
        "evidence_urls": ["https://novagpu.example/pricing"]}


def test_rules_are_enforced_in_code():
    known = {"modal", "kaggle"}
    assert discovery.judge(GOOD, known).status == "qualified"
    assert discovery.judge({**GOOD, "headless_jobs": "no"}, known).status == "rejected"  # browser-only notebook
    assert discovery.judge({**GOOD, "official_api": "no"}, known).status == "rejected"
    unverified = discovery.judge({**GOOD, "headless_jobs": "unknown"}, known)
    assert unverified.status == "needs_review" and "not verified" in unverified.reason
    no_tos = discovery.judge({**GOOD, "tos_allows_automation": "no"}, known)
    assert no_tos.status == "rejected" and "terms" in no_tos.reason
    unknown_tos = discovery.judge({**GOOD, "tos_allows_automation": "unknown"}, known)
    assert unknown_tos.status == "needs_review" and unknown_tos.confidence < discovery.CONFIDENCE_BAR
    assert discovery.judge({**GOOD, "name": "Modal"}, known).status == "known"
    assert discovery.judge({**GOOD, "confidence": 0.5}, known).status == "needs_review"
    assert discovery.judge({**GOOD, "url": "http://insecure.example"}, known).status == "rejected"


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.message


def test_run_adds_only_qualified_offers_and_sends_a_card(tmp_path):
    turns = [
        SimpleNamespace(stop_reason="pause_turn", content=[SimpleNamespace(type="server_tool_use")]),
        SimpleNamespace(stop_reason="tool_use", content=[
            SimpleNamespace(type="tool_use", name="report_offer", id="t1", input=GOOD),
            SimpleNamespace(type="tool_use", name="report_offer", id="t2",
                            input={**GOOD, "name": "ClickOnlyLab", "headless_jobs": "no"}),
        ]),
        SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Found 2 offers.")]),
    ]
    requests = []

    def stream(**kwargs):
        requests.append({**kwargs, "messages": list(kwargs["messages"])})  # the loop reuses one list
        return FakeStream(turns[len(requests) - 1])

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    th, store = make(tmp_path, FakeAdapter("modal", [], 1), FakeAdapter("kaggle", [], 5))
    sent = []
    notifier = SimpleNamespace(whatsapp=SimpleNamespace(send=sent.append))
    out = discovery.run_discovery(store, notifier=notifier, client=client)

    assert out["added_to_registry"] == ["novagpu"] and out["summary"] == "Found 2 offers."
    provider = store.get_provider("novagpu")
    assert provider.status == "pending_adapter" and provider.source == "discovery"
    assert [f["status"] for f in store.findings] == ["qualified", "rejected"]
    assert len(sent) == 1 and "NovaGPU" in sent[0] and "verify your phone" in sent[0]
    # both tool results go back in ONE user message; the paused turn is resent without an extra user message
    assert requests[2]["messages"][-1]["role"] == "user" and len(requests[2]["messages"][-1]["content"]) == 2
    assert requests[1]["messages"][-1]["role"] == "assistant"


def test_dry_run_saves_nothing(tmp_path):
    turns = [SimpleNamespace(stop_reason="tool_use", content=[
                 SimpleNamespace(type="tool_use", name="report_offer", id="t1", input=GOOD)]),
             SimpleNamespace(stop_reason="end_turn", content=[])]
    calls = []
    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(
        stream=lambda **k: (calls.append(1), FakeStream(turns[len(calls) - 1]))[1])))
    th, store = make(tmp_path, FakeAdapter("modal", [], 1), FakeAdapter("kaggle", [], 5))
    out = discovery.run_discovery(store, client=client, dry_run=True)
    assert out["findings"][0]["status"] == "qualified" and not store.findings
    assert "novagpu" not in {p.id for p in store.list_providers()}
