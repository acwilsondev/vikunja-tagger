# vikunja-tagger

A small webhook receiver that listens for Vikunja's `task.created` event and
uses an Ollama model to apply labels to the new task, chosen from your
existing Vikunja labels only (it never invents new ones).

## How it works

1. Vikunja calls `POST /webhooks/vikunja` on task creation (configured as a
   per-project webhook in Vikunja itself), signing the body with a shared
   secret (`X-Vikunja-Signature`, HMAC-SHA256 hex digest).
2. The worker verifies the signature, fetches your current Vikunja labels
   (title + description, so the model sees your own definitions, not
   guesses), and asks Ollama to pick zero or more for the new task based on
   its title/description.
3. It applies the chosen labels back to the task via the Vikunja API.

Tasks that already have labels when the event fires are skipped (checked
against live task state, so a redelivery of the same event is a no-op).

## Tagging rule

Each label's own description is classified by convention:
- Title starting `effort:` → an **effort** label (mutually exclusive - the
  model must pick exactly one).
- Description starting `Context -` → a **context** label.
- Description starting `Flag -` → a **flag** label (structural markers like
  `waiting_for`/`repeating` - the model is told never to guess these on its
  own, `waiting_for` excepted).
- Anything else → an **other** label, an optional extra.

Every task must end up with either exactly one effort label plus at least
one context label, or `waiting_for` on its own. This is enforced in code
(`app/ollama.py::_satisfies_rule`), not just requested in the prompt - if
the model's first answer doesn't satisfy it, the worker sends one
corrective retry with the violation spelled out before giving up and
applying whatever it got (logged as a warning either way).

The prompt (`SYSTEM_PROMPT` in `app/ollama.py`) also carries a few
calibration examples, mainly to stop the model conflating "this task
involves someone else" with `waiting_for` - e.g. "call the dentist to
reschedule" is an action *you* take (`calls` + an effort label), not
something you're already blocked waiting on. Tune those examples there if
you see it miscategorize a real task the same way.

## Configuration

Copy `.env.example` to `.env` and fill in:

| Var | Description |
|---|---|
| `VIKUNJA_URL` | Base URL of your Vikunja instance (no `/api/v1`) |
| `VIKUNJA_API_TOKEN` | Personal API token — Vikunja Settings > API Tokens. Needs read on labels, read/write on tasks |
| `WEBHOOK_SECRET` | Shared secret you also set on the Vikunja webhook |
| `OLLAMA_URL` | Base URL of an Ollama server reachable from wherever this runs |
| `OLLAMA_MODEL` | Model to use (default `llama3.1:latest`) |

## Setting up the Vikunja webhook

In each project you want auto-tagging on: **Project > Webhooks > Create** →
target URL pointing at this worker's `/webhooks/vikunja` endpoint, event
`task.created`, secret matching `WEBHOOK_SECRET`.

## Running locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## Building the image

```bash
docker build -t vikunja-tagger .
```

`.github/workflows/build.yml` builds and pushes to
`ghcr.io/<owner>/vikunja-tagger` on every push to `main` (tags `latest` and
the commit SHA).

Deployment (this worker running inside a k3s/Argo CD stack, alongside
Vikunja and Ollama) lives in a separate infra repo, not here — this repo is
just the source and the image build.

Note: since the deploy manifest tracks the `latest` tag, Argo CD's
self-heal won't notice a new image on its own (the manifest text doesn't
change). After a push, restart the deployment to pick it up, e.g.
`kubectl -n vikunja-tagger rollout restart deployment/vikunja-tagger`.
