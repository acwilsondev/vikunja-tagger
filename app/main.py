import logging

from fastapi import FastAPI, Header, HTTPException, Request

from . import config, ollama, signature, vikunja

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("vikunja-tagger")

app = FastAPI()


async def _load_examples(existing_labels: list[dict]) -> list[tuple[str, list[str]]]:
    # Examples only improve suggestions; never let a failure to fetch them
    # stop the task from being tagged.
    if config.EXAMPLE_COUNT <= 0:
        return []
    try:
        completed = await vikunja.list_recent_completed(config.EXAMPLE_COUNT)
    except Exception:
        log.warning("could not fetch completed tasks, tagging without examples", exc_info=True)
        return []
    labels_by_title = {label["title"]: label for label in existing_labels}
    return ollama.select_examples(completed, labels_by_title)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/webhooks/vikunja")
async def vikunja_webhook(request: Request, x_vikunja_signature: str | None = Header(default=None)):
    raw_body = await request.body()
    if not signature.verify(config.WEBHOOK_SECRET, raw_body, x_vikunja_signature):
        raise HTTPException(status_code=401, detail="invalid signature")

    payload = await request.json()
    event_name = payload.get("event_name")
    if event_name != "task.created":
        return {"status": "ignored", "event_name": event_name}

    task = payload["data"]["task"]
    task_id = task["id"]

    # The task embedded in the webhook payload is a snapshot from when the
    # event fired, always unlabeled for a brand-new task - checking it
    # wouldn't catch a redelivery of the same event. Check live state
    # instead, which also makes redelivery a safe no-op.
    current = await vikunja.get_task(task_id)
    if current.get("labels"):
        log.info("task %s (%r) already has labels, skipping", task_id, task["title"])
        return {"status": "skipped", "reason": "already labeled"}

    existing_labels = await vikunja.list_labels()

    examples = await _load_examples(existing_labels)

    chosen = await ollama.suggest_labels(task["title"], task.get("description", ""), existing_labels, examples)
    if not chosen:
        log.info("task %s (%r): no labels suggested", task_id, task["title"])
        return {"status": "tagged", "labels": []}

    label_id_by_title = {label["title"]: label["id"] for label in existing_labels}
    for title in chosen:
        await vikunja.add_label_to_task(task_id, label_id_by_title[title])

    log.info("task %s (%r): applied labels %s (%d examples)", task_id, task["title"], chosen, len(examples))
    return {"status": "tagged", "labels": chosen}
