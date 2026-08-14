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

## Usage

Getting a working setup means: run the container somewhere that can reach
both Vikunja and Ollama over HTTP, give it a Vikunja API token, and tell
Vikunja to call it. Steps 1-2 differ depending on where you're running it;
steps 3-5 are the same everywhere.

### 1. Run the worker

**Option A — Kubernetes.** Minimal example (adjust image/hosts/namespace to
your setup; a real deployment will also want resource limits, etc.):

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: vikunja-tagger
spec:
  replicas: 1
  selector:
    matchLabels: {app: vikunja-tagger}
  template:
    metadata:
      labels: {app: vikunja-tagger}
    spec:
      containers:
        - name: vikunja-tagger
          image: ghcr.io/acwilsondev/vikunja-tagger:latest
          ports: [{containerPort: 8000}]
          env:
            - name: VIKUNJA_URL
              value: "http://vikunja.vikunja.svc.cluster.local:3456"
            - name: OLLAMA_URL
              value: "http://ollama.ollama.svc.cluster.local:11434"
            - name: OLLAMA_MODEL
              value: "llama3.1:latest"
          envFrom:
            - secretRef: {name: vikunja-tagger-secrets}
---
apiVersion: v1
kind: Service
metadata:
  name: vikunja-tagger
spec:
  selector: {app: vikunja-tagger}
  ports: [{port: 8000, targetPort: 8000}]
```

```bash
kubectl create namespace vikunja-tagger
kubectl -n vikunja-tagger create secret generic vikunja-tagger-secrets \
  --from-literal=VIKUNJA_API_TOKEN='<from step 2>' \
  --from-literal=WEBHOOK_SECRET="$(openssl rand -hex 32)"
kubectl -n vikunja-tagger apply -f deployment.yaml
```

If Vikunja and Ollama aren't cluster-internal Services reachable by the
hostnames above, swap in whatever URLs actually reach them. If Vikunja
*is* calling a cluster-internal ClusterIP for the webhook (the normal case
for an in-cluster setup like this), see the SSRF note in step 4 — Vikunja
will otherwise refuse to deliver the webhook at all.

(This repo intentionally ships no deployment manifests of its own — keep
those in whatever infra/GitOps repo manages the rest of your stack, so this
one stays just the source + image build. My own deployment — a
Helm-values/Argo CD app on a shared k3s cluster, structurally the same as
the YAML above — lives in
[acwilsondev/anarchy-pizza-docker](https://github.com/acwilsondev/anarchy-pizza-docker),
under `k8s/apps/vikunja-tagger/`.)

**Option B — anywhere else (Docker, bare process).** See
[Local development](#local-development) below; in production, run the
built image (see [Building the image](#building-the-image)) with the same
env vars via `docker run --env-file .env -p 8000:8000 vikunja-tagger`, or
your platform's equivalent.

### 2. Create a Vikunja API token

In Vikunja: **Settings > API Tokens > Create**. Needs at minimum:
- `labels:read`
- `tasks:read`, `tasks:write` (to read task payloads and attach labels)

Use this as `VIKUNJA_API_TOKEN`.

### 3. Allow Vikunja to reach the worker (only if the target is a private/cluster-internal address)

Vikunja has built-in SSRF protection: by default it refuses outgoing
requests (webhook deliveries included) to non-globally-routable IPs — which
covers any Kubernetes ClusterIP, Docker bridge address, or other RFC1918
address. If the worker's URL resolves to one of those (the normal case for
an in-cluster deployment), you'll need:

```
VIKUNJA_OUTGOINGREQUESTS_ALLOWNONROUTABLEIPS=true
```

set on the Vikunja instance itself (not this worker). Fine for a
single-user instance; think twice on a multi-tenant one, since it also
relaxes SSRF protection for anything else that makes Vikunja fetch a URL.
If you skip this and the target is non-routable, Vikunja will log
`prohibited IP address ... denied by: 10.0.0.0/8` (or similar) and never
call the worker at all — nothing will show up in the worker's own logs,
which is the tell that this is the problem.

If the worker is reachable at a normal public/routable hostname instead,
skip this step.

### 4. Register the webhook in Vikunja

Per project you want auto-tagging on: **Project > Webhooks > Create**.

- **Target URL**: wherever the worker is reachable, e.g.
  `http://vikunja-tagger.vikunja-tagger.svc.cluster.local:8000/webhooks/vikunja`
  for the in-cluster example above.
- **Event**: `task.created`
- **Secret**: the same value as `WEBHOOK_SECRET`

### 5. Verify

Create a task in that project, then check the worker's logs (`kubectl -n
vikunja-tagger logs -l app=vikunja-tagger -f`, or your platform's
equivalent) for a `task N: applied labels [...]` line. Troubleshooting:

- **Nothing in the worker's logs at all** → Vikunja isn't reaching it.
  Check Vikunja's own logs for an SSRF rejection (step 3), or a plain
  connection error if the URL/network path is wrong.
- **`invalid signature`** on the very first delivery → `WEBHOOK_SECRET`
  mismatch between the worker's env and what you set on the Vikunja
  webhook.
- **Worker logs show it ran but no labels appeared** → check for a
  `could not satisfy tagging rule` warning (see below) or an `applied
  labels: []` line, meaning the model genuinely found nothing that fit.

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

This rule is specific to my own label taxonomy (`effort:*`, `Context -
...`, `Flag - ...` descriptions) - if you're adapting this for your own
Vikunja labels, either match that convention or adjust `_classify` and
`SYSTEM_PROMPT` in `app/ollama.py` to fit your own.

## Configuration

Copy `.env.example` to `.env` and fill in:

| Var | Description |
|---|---|
| `VIKUNJA_URL` | Base URL of your Vikunja instance (no `/api/v1`) |
| `VIKUNJA_API_TOKEN` | Personal API token — Vikunja Settings > API Tokens. Needs read on labels, read/write on tasks |
| `WEBHOOK_SECRET` | Shared secret you also set on the Vikunja webhook |
| `OLLAMA_URL` | Base URL of an Ollama server reachable from wherever this runs |
| `OLLAMA_MODEL` | Model to use (default `llama3.1:latest`) |

## Local development

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

Note: if your deployment tracks the `latest` tag, nothing auto-restarts the
running container on a new push by itself (a plain `latest` tag change
isn't something most deploy tooling — Argo CD included — treats as a
manifest change). After a push, restart it explicitly, e.g. in Kubernetes:
`kubectl -n vikunja-tagger rollout restart deployment/vikunja-tagger`.
