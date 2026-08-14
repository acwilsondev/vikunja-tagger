import logging

from fastapi import FastAPI, Header, HTTPException, Request

from . import config, ollama, signature, vikunja

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("vikunja-tagger")

app = FastAPI()


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
        log.info("task %s already has labels, skipping", task_id)
        return {"status": "skipped", "reason": "already labeled"}

    existing_labels = await vikunja.list_labels()

    chosen = await ollama.suggest_labels(task["title"], task.get("description", ""), existing_labels)
    if not chosen:
        log.info("task %s: no labels suggested", task_id)
        return {"status": "tagged", "labels": []}

    label_id_by_title = {label["title"]: label["id"] for label in existing_labels}
    for title in chosen:
        await vikunja.add_label_to_task(task_id, label_id_by_title[title])

    log.info("task %s: applied labels %s", task_id, chosen)
    return {"status": "tagged", "labels": chosen}
