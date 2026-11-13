import os
import httpx

AGENT_BASE = os.getenv("ORDERS_AGENT_URL", "http://localhost:8002")

async def discover_orders_agent():
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(f"{AGENT_BASE}/.well-known/agent-card.json")
        response.raise_for_status()
        return response.json()

async def call_orders_agent(method: str, params: dict, bearer: str):
    card = await discover_orders_agent()
    endpoint = card["url"]
    # In Docker, card URL resolves to service name. Locally, allow env override.
    if endpoint.startswith("http://orders-agent:") and "localhost" in AGENT_BASE:
        endpoint = endpoint.replace("http://orders-agent:", "http://localhost:")
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(endpoint, json={
            "jsonrpc": "2.0", "method": method, "params": params, "id": "chat-call-1"
        }, headers={"Authorization": bearer})
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise RuntimeError(payload["error"]["message"])
        return payload["result"]
