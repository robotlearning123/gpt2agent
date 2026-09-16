"""Phase 0 manual handoff mode — self-contained paste/send instructions.

When a conversation-class tool is called with ``manual=True`` it makes zero
network calls and returns ``build_handoff`` serialized as JSON: the exact
prompt text the REST/SSE path would send, where to paste it, and which
existing tools to use for readback.
"""
from __future__ import annotations

CHATGPT_URL = "https://chatgpt.com/"

_READBACK = {"list": "list_conversations", "fetch": "get_conversation"}

_MODE_LABELS = {
    "agent": "Agent",
    "deep_research": "Deep Research",
    "canvas": "Canvas",
    "images": "image generation",
}


def gpt_chat_url(gizmo_id: str) -> str:
    """Custom-GPT chat URL; accepts both 'g/<slug>' and bare '<slug>'."""
    slug = str(gizmo_id).removeprefix("g/")
    return f"{CHATGPT_URL}g/{slug}"


def build_handoff(
    tool: str,
    prompt: str,
    *,
    model: str | None = None,
    temporary: bool | None = None,
    extra: dict | None = None,
) -> dict:
    """Assemble the manual-handoff payload for a conversation-class tool."""
    extra = dict(extra or {})
    if tool == "gpt_chat":
        url = gpt_chat_url(extra.get("gizmo_id", ""))
    else:
        url = CHATGPT_URL

    steps = [f"Open {url} in a browser signed in to your ChatGPT account."]
    mode = extra.get("mode")
    if mode:
        steps.append(
            f"Enable {_MODE_LABELS.get(mode, mode)} mode in the composer."
        )
    if model:
        steps.append(f"Select the {model} model in the model picker.")
    if temporary:
        steps.append("Start a temporary chat (hourglass icon) so it is not saved.")
    steps.append("Paste the prompt text from this payload and send it.")
    if temporary:
        # Temporary chats are never saved to history, so list_conversations
        # cannot find them — the only way to keep the reply is to copy it from
        # the browser (found during the v0.0.14 release roundtrip).
        steps.append(
            "Temporary chats do not appear in list_conversations — copy the "
            "reply text directly from the browser, or re-run with "
            "temporary=False if you need tool readback."
        )
        readback = {
            "note": "temporary chats are not saved to history; copy the reply "
            "from the browser, or use temporary=False for tool readback",
        }
    else:
        steps.append(
            "When the reply finishes, run list_conversations to find the new "
            "conversation, then get_conversation to read the result back."
        )
        readback = dict(_READBACK)
    steps = [f"{i}. {s}" for i, s in enumerate(steps, 1)]

    handoff = {
        "status": "manual_handoff",
        "tool": tool,
        "prompt": prompt,
        "url": url,
        "model_hint": model,
        "temporary_hint": temporary,
        "steps": steps,
        "readback": readback,
    }
    handoff.update(extra)
    return handoff
