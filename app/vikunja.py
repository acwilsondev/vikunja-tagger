import httpx

from . import config


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"{config.VIKUNJA_URL}/api/v1",
        headers={"Authorization": f"Bearer {config.VIKUNJA_API_TOKEN}"},
        timeout=10,
    )


async def list_labels() -> list[dict]:
    async with _client() as client:
        resp = await client.get("/labels", params={"per_page": 200})
        resp.raise_for_status()
        return resp.json()


async def get_task(task_id: int) -> dict:
    async with _client() as client:
        resp = await client.get(f"/tasks/{task_id}")
        resp.raise_for_status()
        return resp.json()


async def add_label_to_task(task_id: int, label_id: int) -> None:
    async with _client() as client:
        resp = await client.put(f"/tasks/{task_id}/labels", json={"label_id": label_id})
        # Vikunja rejects a label that's already on the task (seen as both
        # 400 and 500, wording varies) - not worth failing the request over.
        if resp.status_code >= 400 and "already" not in resp.text.lower():
            resp.raise_for_status()
