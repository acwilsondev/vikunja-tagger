import os


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required env var {name}")
    return value


VIKUNJA_URL = _require("VIKUNJA_URL").rstrip("/")
VIKUNJA_API_TOKEN = _require("VIKUNJA_API_TOKEN")
WEBHOOK_SECRET = _require("WEBHOOK_SECRET")

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:latest")
