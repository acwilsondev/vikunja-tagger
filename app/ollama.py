import json
import logging
import re

import httpx

from . import config

log = logging.getLogger("vikunja-tagger")

WAITING_FOR = "waiting_for"

SYSTEM_PROMPT = """You tag to-do tasks with labels for a GTD-style task \
system. You will be given a task's title/description and a catalog of \
allowed labels, each shown as "title (kind): description". Choose labels \
only from this catalog - never invent one. If nothing clearly applies \
beyond the required ones below, don't add extras.

Rules:
- Every task must end up with EITHER:
  (a) exactly one "effort" label AND at least one "context" label, OR
  (b) the "waiting_for" label, on its own, with no effort/context label.
- waiting_for means YOU have already done your part and are now blocked \
on someone/something else - e.g. "waiting for the plumber to call back", \
"pending approval from finance". It does NOT mean the task merely \
involves another person. A task where you need to call, ask, message, or \
follow up with someone is an action YOU take - tag it with effort+context \
like any other task, never waiting_for, even if the wording mentions \
someone else or sounds indirect.
- "effort" labels are mutually exclusive - pick exactly one that best fits.
- Respect any mutual-exclusivity mentioned in a label's own description \
(e.g. two context labels that say they're exclusive of each other). In \
particular: "errands" covers any task requiring physically leaving home \
(shopping, appointments, pickups, etc) - use it over "home" whenever the \
task can't be done without leaving the house.
- "flag" labels other than waiting_for are structural markers a human or \
another system sets - never choose them yourself.
- "other" labels are optional extras - add one only if it clearly, \
specifically applies.

Examples (for calibration only, not literal tasks):
- "Call the dentist to reschedule" -> ["calls", "effort:S"] (an action \
you take; NOT waiting_for even though it's a call)
- "Waiting for the plumber to call back about the quote" -> \
["waiting_for"] (you already made contact, now genuinely blocked)
- "Get car brakes fixed" -> ["calls", "effort:S"] (the fact that someone \
else *does the work* doesn't make it waiting_for - YOU haven't taken any \
action yet, so this is an action: call/schedule it)
- "Ask Steph about the Albany trip dates" -> ["agenda:steph", "effort:S"] \
(an action you take - bringing something up - not waiting_for)
- "Buy milk" -> ["errands", "effort:S"] (requires leaving home, so \
errands, not home)
- "Refactor the auth middleware" -> ["computer", "effort:XL"]

Respond with JSON only, in the form: {"labels": ["label one", "label two"]}"""


def _strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or "").strip()


def _classify(label: dict) -> str:
    if label["title"].startswith("effort:"):
        return "effort"
    desc = _strip_html(label.get("description", ""))
    if desc.startswith("Context -") or desc.startswith("Context –"):
        return "context"
    if desc.startswith("Flag -") or desc.startswith("Flag –"):
        return "flag"
    return "other"


def _satisfies_rule(chosen: list[str], labels_by_title: dict[str, dict]) -> bool:
    if WAITING_FOR in chosen:
        return chosen == [WAITING_FOR]
    kinds = [_classify(labels_by_title[t]) for t in chosen if t in labels_by_title]
    return kinds.count("effort") == 1 and "context" in kinds


def _build_catalog(labels: list[dict]) -> str:
    lines = []
    for label in labels:
        kind = _classify(label)
        desc = _strip_html(label.get("description", "")) or "(no description)"
        lines.append(f"- {label['title']} ({kind}): {desc}")
    return "\n".join(lines)


async def _ask(title: str, description: str, catalog: str, correction: str | None) -> list[str]:
    user_prompt = (
        f"Allowed labels:\n{catalog}\n\n"
        f"Task title: {title}\n"
        f"Task description: {description or '(none)'}"
    )
    if correction:
        user_prompt += f"\n\n{correction}"

    async with httpx.AsyncClient(base_url=config.OLLAMA_URL, timeout=60) as client:
        resp = await client.post(
            "/api/chat",
            json={
                "model": config.OLLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "format": "json",
                "stream": False,
                "options": {"temperature": 0.2},
            },
        )
        resp.raise_for_status()
        content = resp.json()["message"]["content"]

    try:
        parsed = json.loads(content)
        return parsed.get("labels", [])
    except (json.JSONDecodeError, AttributeError):
        return []


async def suggest_labels(title: str, description: str, labels: list[dict]) -> list[str]:
    if not labels:
        return []

    labels_by_title = {label["title"]: label for label in labels}
    allowed_lower = {t.lower(): t for t in labels_by_title}
    catalog = _build_catalog(labels)

    def _validate(raw: list[str]) -> list[str]:
        return [allowed_lower[t.lower()] for t in raw if t.lower() in allowed_lower]

    chosen = _validate(await _ask(title, description, catalog, correction=None))

    if not _satisfies_rule(chosen, labels_by_title):
        correction = (
            f"Your previous selection was {chosen!r}, which doesn't satisfy the "
            "required rule: it needs exactly one effort label plus at least one "
            "context label, or the waiting_for label. Pick again, correctly this time."
        )
        chosen = _validate(await _ask(title, description, catalog, correction))

    if not _satisfies_rule(chosen, labels_by_title):
        log.warning("task %r: could not satisfy tagging rule, applying best effort %s", title, chosen)

    return chosen
