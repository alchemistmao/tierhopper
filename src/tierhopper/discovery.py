"""Weekly discovery of free GPU tiers and credit programs (Claude API, direct tool use).

Claude researches with the server-side web search/fetch tools and reports each candidate through the
strict `report_offer` tool. The model only *reports*; the rules that decide what becomes a provider
are enforced here in code:
  - an official API/SDK for headless (non-interactive) GPU jobs is mandatory;
  - terms that forbid automation disqualify the offer;
  - nothing that needs multiple accounts, CAPTCHA/phone-verification bypass or scraping;
  - providers already in the registry are skipped.
Qualified offers (confidence >= 0.7) enter the registry as `pending_adapter` and get a card.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from tierhopper import credentials
from tierhopper.models import CreditModel, Provider, ProviderKind, ProviderStatus, Requirements, now
from tierhopper.redact import redact

MODEL = "claude-opus-5-5"
CONFIDENCE_BAR = 0.7
MAX_CONTINUATIONS = 6

CURATED_SOURCES = [
    "https://github.com/zszazi/Deep-learning-in-cloud",
    "https://github.com/ripienaar/free-for-dev",
    "https://www.reddit.com/r/LocalLLaMA/",
    "https://www.reddit.com/r/MachineLearning/",
    "https://news.ycombinator.com/",
]

SYSTEM = """You research free GPU compute for an orchestrator called TierHopper, which runs batch GPU jobs
through each provider's OFFICIAL API or SDK, with one account per provider created by its owner.

Find cloud providers that currently offer free GPU time: recurring free tiers, sign-up credits, or
credit programs for individuals/researchers/startups. Check the curated sources given by the user and
search the web for recent announcements. Verify each candidate on the provider's own pages (pricing,
docs, terms) before reporting it.

Report each distinct provider once with the `report_offer` tool. Report honestly:
- `official_api` is "yes" only if you read docs for an API, SDK or CLI of the provider's own; "no" if the
  provider clearly has none; "unknown" if you did not get to check.
- `headless_jobs` is "yes" only if that API can start a GPU job/notebook/container non-interactively
  (no browser session required); "no" for a browser-only product; "unknown" if you did not verify it.
- Use "unknown" rather than guessing. Spend your searches verifying the most promising candidates on
  their official docs instead of listing many unverified ones.
- `tos_allows_automation`: "yes" if the terms allow programmatic use, "no" if they forbid automated or
  scripted use, "unknown" if you could not find it.
- `confidence` (0-1) reflects how sure you are that the offer exists today exactly as described.
- Put the URLs you actually read in `evidence_urls`.

Do not report: providers in the user's already-known list, offers that require several accounts,
anything that needs bypassing CAPTCHA or phone verification, expired promotions, or rumours you could
not confirm on an official page. When you have reported everything worth reporting, reply with one
short sentence summarising the run."""

REPORT_TOOL = {
    "name": "report_offer",
    "description": "Report one verified free-GPU offer from a cloud provider.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Provider name"},
            "url": {"type": "string", "description": "Official sign-up or pricing URL"},
            "summary": {"type": "string", "description": "What is free, in one sentence"},
            "credit_type": {"type": "string", "enum": ["monthly_usd", "weekly_hours", "one_time_usd", "other"]},
            "credit_amount": {"type": "number", "description": "USD or GPU-hours per period; 0 if unknown"},
            "gpu_types": {"type": "array", "items": {"type": "string"}},
            "official_api": {"type": "string", "enum": ["yes", "no", "unknown"]},
            "headless_jobs": {"type": "string", "enum": ["yes", "no", "unknown"]},
            "api_docs_url": {"type": "string", "description": "API/SDK docs URL, or empty"},
            "tos_allows_automation": {"type": "string", "enum": ["yes", "no", "unknown"]},
            "requires_phone": {"type": "boolean"},
            "requires_card": {"type": "boolean"},
            "confidence": {"type": "number"},
            "evidence_urls": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["name", "url", "summary", "credit_type", "credit_amount", "gpu_types", "official_api",
                     "headless_jobs", "api_docs_url", "tos_allows_automation", "requires_phone", "requires_card",
                     "confidence", "evidence_urls"],
        "additionalProperties": False,
    },
}


@dataclass
class Finding:
    name: str
    url: str
    summary: str
    confidence: float
    official_api: bool
    headless: bool
    tos_ok: bool | None
    status: str  # qualified | needs_review | rejected | known
    reason: str
    raw: dict[str, Any] = field(default_factory=dict)
    provider_id: str | None = None


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "provider"


def judge(offer: dict[str, Any], known: set[str]) -> Finding:
    """Apply the non-negotiable rules to one reported offer."""
    name = str(offer.get("name", "")).strip()
    confidence = max(0.0, min(1.0, float(offer.get("confidence") or 0)))
    tri = {"yes": True, "no": False, True: True, False: False}
    api_state, headless_state = tri.get(offer.get("official_api")), tri.get(offer.get("headless_jobs"))
    api, headless = api_state is True, headless_state is True
    tos = {"yes": True, "no": False}.get(str(offer.get("tos_allows_automation")))
    url = str(offer.get("url", ""))
    base = dict(name=name, url=url, summary=redact(str(offer.get("summary", "")))[:400], official_api=api,
                headless=headless, tos_ok=tos, raw=offer)
    sid = slug(name)
    if sid in known or any(k in sid or sid in k for k in known if len(k) > 3):
        return Finding(**base, confidence=confidence, status="known", reason="already in the registry")
    if not url.startswith("https://"):
        return Finding(**base, confidence=min(confidence, 0.2), status="rejected", reason="no official https URL")
    if api_state is False or headless_state is False:
        return Finding(**base, confidence=min(confidence, 0.4), status="rejected",
                       reason="no official API for headless GPU jobs")
    if not api or not headless:  # not verified either way: a person should look, never auto-qualify
        return Finding(**base, confidence=min(confidence, 0.5), status="needs_review",
                       reason="API for headless jobs not verified yet")
    if tos is False:
        return Finding(**base, confidence=min(confidence, 0.3), status="rejected",
                       reason="terms of use forbid automation")
    if not offer.get("evidence_urls"):
        confidence = min(confidence, 0.5)
    if tos is None:
        confidence = min(confidence, 0.69)  # never auto-qualify without having read the terms
    if confidence >= CONFIDENCE_BAR:
        return Finding(**base, confidence=confidence, status="qualified", reason="official API, terms allow it",
                       provider_id=sid)
    why = "terms of use not verified" if tos is None else "low confidence"
    return Finding(**base, confidence=confidence, status="needs_review", reason=why)


def to_provider(f: Finding) -> Provider:
    o = f.raw
    credit_type = o.get("credit_type") if o.get("credit_type") != "other" else "one_time_usd"
    return Provider(
        id=f.provider_id or slug(f.name), name=f.name, kind=ProviderKind.CREDIT_PROGRAM,
        status=ProviderStatus.PENDING_ADAPTER, adapter=None, signup_url=f.url,
        api_key_url=o.get("api_docs_url") or None,
        requirements=Requirements(phone=bool(o.get("requires_phone")), card=bool(o.get("requires_card")),
                                  notes=f.summary),
        credit_model=CreditModel(type=credit_type, amount=float(o.get("credit_amount") or 0),
                                 notes="Found by weekly discovery; verify before connecting."),
        gpu_catalog=[], source="discovery", confidence=f.confidence, notes=f.reason, updated_at=now(),
    )


def research(known_names: list[str], client: Any = None, max_searches: int = 20) -> tuple[list[dict], str]:
    """Run the research loop. Returns (reported offers, final one-line summary)."""
    import anthropic

    client = client or anthropic.Anthropic(api_key=credentials.get_secret("anthropic", "api_key"),
                                           timeout=600.0, max_retries=2)
    tools = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": max_searches},
        REPORT_TOOL,
    ]
    user = ("Already known (skip these): " + ", ".join(known_names) + ".\n"
            "Curated sources to check first:\n" + "\n".join(f"- {u}" for u in CURATED_SOURCES) + "\n"
            f"Today is {now().date().isoformat()}. Report up to 8 offers.")
    messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
    offers: list[dict] = []
    summary = ""
    for _ in range(MAX_CONTINUATIONS):
        with client.beta.messages.stream(
            model=MODEL, max_tokens=16000, system=SYSTEM, tools=tools, messages=messages,
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        ) as stream:
            response = stream.get_final_message()
        if response.stop_reason == "refusal":
            return offers, "The research request was declined."
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type == "tool_use" and block.name == "report_offer":
                offers.append(dict(block.input))
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": "recorded"})
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        summary = text or summary
        if response.stop_reason == "tool_use" and results:
            messages.append({"role": "user", "content": results})  # all results in one message
            continue
        if response.stop_reason == "pause_turn":
            continue  # server-tool loop paused: resend as-is, the API resumes
        break
    return offers, summary


def run_discovery(store: Any, notifier: Any = None, client: Any = None, dry_run: bool = False) -> dict[str, Any]:
    known_providers = store.list_providers()
    known = {p.id for p in known_providers} | {slug(p.name) for p in known_providers} | {"colab", "google-colab"}
    offers, summary = research(sorted({p.name for p in known_providers} | {"Google Colab"}), client=client)
    findings = [judge(o, known) for o in offers]
    added = []
    for f in findings:
        if f.status == "known":
            continue
        if not dry_run:
            if f.status == "qualified":
                provider = store.upsert_provider(to_provider(f))
                added.append(provider.id)
                if notifier is not None and getattr(notifier, "whatsapp", None):
                    from tierhopper.notify import integration_card

                    notifier.whatsapp.send(integration_card(provider) +
                                           "\n(New: TierHopper still needs an adapter for this one.)")
            store.add_finding({"name": f.name, "url": f.url, "summary": f.summary, "confidence": f.confidence,
                               "official_api": f.official_api, "headless": f.headless, "tos_ok": f.tos_ok,
                               "status": f.status, "reason": f.reason, "provider_id": f.provider_id})
    return {"summary": summary, "reported": len(offers), "added_to_registry": added,
            "findings": [{"name": f.name, "status": f.status, "confidence": round(f.confidence, 2),
                          "reason": f.reason, "url": f.url} for f in findings]}
