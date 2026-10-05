"""Natural-language commands for the dashboard palette (Claude API, direct tool use).

Claude never changes anything itself: it turns the user's sentence into ONE proposed action
(a strict tool call) or a short answer. The dashboard shows the proposal and the user confirms
anything that changes state.
"""

from __future__ import annotations

import json
from typing import Any

from tierhopper import credentials

MODEL = "claude-opus-5-5"
CONFIRM = {"pause_project", "resume_project", "approve_project", "rerun_project"}

SYSTEM = """You turn one short command from the TierHopper dashboard into a single action.

TierHopper runs GPU jobs ("projects") on free cloud GPU tiers and hops between providers. The user
message contains the command and a JSON snapshot of the current projects and providers.

Rules:
- Pick exactly one tool when the command asks to do something. Use the project `id` from the snapshot;
  match names loosely (the user may say "the layout one"). If several projects could match, do not
  guess: answer with a short question naming the candidates instead of calling a tool.
- If the command is a question (status, credit, savings, what is running), do not call a tool: answer
  in one or two plain sentences using only the snapshot. No jargon: say "project", not "job" or "shard".
- Never invent projects, providers or numbers that are not in the snapshot.
- Reply in the language the user wrote in."""


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"name": name, "description": description, "strict": True,
            "input_schema": {"type": "object", "properties": properties, "required": required,
                             "additionalProperties": False}}


_JOB = {"job_id": {"type": "string", "description": "Project id from the snapshot"}}
TOOLS = [
    _tool("open_project", "Open a project's detail page.", _JOB, ["job_id"]),
    _tool("pause_project", "Pause a running or waiting project (its progress is kept).", _JOB, ["job_id"]),
    _tool("resume_project", "Resume a paused project from its last save.", _JOB, ["job_id"]),
    _tool("approve_project", "Approve a project that is waiting for the user's OK.", _JOB, ["job_id"]),
    _tool("get_results", "Download a project's results (or its latest save if still running).", _JOB, ["job_id"]),
    _tool("rerun_project", "Run an existing project again as a new project, optionally with different GPU needs.",
          {**_JOB,
           "min_vram_gb": {"type": ["number", "null"], "description": "Minimum GPU memory per GPU, or null to keep"},
           "cheapest": {"type": "boolean", "description": "True when the user asks for the cheapest GPU that fits"}},
          ["job_id", "min_vram_gb", "cheapest"]),
    _tool("navigate", "Go to a dashboard tab.",
          {"tab": {"type": "string", "enum": ["projects", "map", "providers"]}}, ["tab"]),
    _tool("set_theme", "Switch the dashboard theme.",
          {"theme": {"type": "string", "enum": ["mission", "crt", "light"]}}, ["theme"]),
]


def configured() -> bool:
    return bool(credentials.get_secret("anthropic", "api_key"))


def plan_command(text: str, snapshot: dict[str, Any], client: Any = None) -> dict[str, Any]:
    """Return {"kind": "action", action, args, summary, confirm} or {"kind": "answer", text}."""
    import anthropic

    client = client or anthropic.Anthropic(api_key=credentials.get_secret("anthropic", "api_key"),
                                           timeout=40.0, max_retries=1)
    user = f"Command: {text.strip()[:500]}\n\nSnapshot:\n{json.dumps(snapshot, default=str)[:12000]}"
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=1500,
            system=SYSTEM,
            tools=TOOLS,
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.AuthenticationError:
        return {"kind": "answer", "text": "The Claude API key is not valid. Run `tierhopper config anthropic`."}
    except anthropic.RateLimitError:
        return {"kind": "answer", "text": "Claude is rate limited right now. Try again in a minute."}
    except anthropic.APIStatusError as e:
        return {"kind": "answer", "text": f"Claude could not process that (HTTP {e.status_code})."}
    except anthropic.APIConnectionError:
        return {"kind": "answer", "text": "Could not reach Claude. Check the connection and try again."}

    if response.stop_reason == "refusal":
        return {"kind": "answer", "text": "I can't help with that command."}
    names = {j["id"]: j["name"] for j in snapshot.get("projects", [])}
    for block in response.content:
        if block.type == "tool_use":
            args = dict(block.input)
            job_id = args.get("job_id")
            if job_id is not None and job_id not in names:
                return {"kind": "answer", "text": "I could not find that project. Which one do you mean?"}
            return {"kind": "action", "action": block.name, "args": args,
                    "summary": _summary(block.name, args, names), "confirm": block.name in CONFIRM}
    answer = " ".join(b.text for b in response.content if b.type == "text").strip()
    return {"kind": "answer", "text": answer or "I did not understand that. Try: “pause the layout project”."}


def _summary(action: str, args: dict[str, Any], names: dict[str, str]) -> str:
    name = names.get(args.get("job_id", ""), "")
    if action == "rerun_project":
        extra = []
        if args.get("min_vram_gb"):
            extra.append(f"at least {args['min_vram_gb']:g} GB per GPU")
        if args.get("cheapest"):
            extra.append("on the cheapest GPU that fits")
        return f"Run “{name}” again" + (f" with {' and '.join(extra)}" if extra else "") + " — free providers first."
    return {
        "open_project": f"Open “{name}”.",
        "pause_project": f"Pause “{name}”. Its progress is kept.",
        "resume_project": f"Resume “{name}” from its last save.",
        "approve_project": f"Approve “{name}”.",
        "get_results": f"Download the results of “{name}”.",
        "navigate": f"Go to {args.get('tab')}.",
        "set_theme": f"Switch to the {args.get('theme')} theme.",
    }.get(action, action)
