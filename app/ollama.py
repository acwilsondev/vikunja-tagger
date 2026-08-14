import json

import httpx

from . import config

SYSTEM_PROMPT = """You tag to-do tasks with labels. You will be given a task's \
title and description, and a fixed list of allowed labels. Choose zero or \
more labels from that list that clearly apply to the task. Never invent a \
label that isn't in the allowed list. If nothing clearly applies, return an \
empty list.

Respond with JSON only, in the form: {"labels": ["label one", "label two"]}"""


async def suggest_labels(title: str, description: str, allowed_labels: list[str]) -> list[str]:
    if not allowed_labels:
        return []

    user_prompt = (
        f"Allowed labels: {json.dumps(allowed_labels)}\n\n"
        f"Task title: {title}\n"
        f"Task description: {description or '(none)'}"
    )

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
        labels = parsed.get("labels", [])
    except (json.JSONDecodeError, AttributeError):
        return []

    allowed_lower = {label.lower(): label for label in allowed_labels}
    return [allowed_lower[label.lower()] for label in labels if label.lower() in allowed_lower]
