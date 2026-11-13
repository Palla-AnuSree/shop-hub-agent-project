import os, re
import httpx
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

STORE_API_URL = os.getenv("STORE_API_URL", "http://localhost:8001")
app = FastAPI(title="ShopHub Orders Agent")

AGENT_CARD = {
    "name": "ShopHub Orders Agent",
    "description": "Handles merchant-scoped order listing, lookup, refund and cancellation.",
    "url": "http://orders-agent:8002/rpc",
    "version": "1.0.0",
    "skills": [
        {"id": "list_orders", "name": "List orders"},
        {"id": "lookup_order", "name": "Look up an order"},
        {"id": "propose_refund", "name": "Prepare refund proposal"},
        {"id": "propose_cancel", "name": "Prepare cancellation proposal"}
    ]
}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/.well-known/agent-card.json")
def agent_card():
    return AGENT_CARD

def clean_order(order: dict) -> dict:
    """Redact PII and neutralize instruction-like customer notes before model-facing use."""
    cleaned = dict(order)
    cleaned["customer_email"] = "[REDACTED]"
    cleaned["customer_phone"] = "[REDACTED]"
    note = str(cleaned.get("customer_note", ""))
    if re.search(r"(ignore (all )?(previous )?instructions|assistant:|reveal .*phone|refund this order immediately)", note, re.I):
        cleaned["customer_note"] = "[UNTRUSTED INSTRUCTION-LIKE NOTE REMOVED]"
    return cleaned

async def call_store(path: str, token: str, method="GET", params=None, body=None):
    async with httpx.AsyncClient(base_url=STORE_API_URL, timeout=10) as client:
        response = await client.request(method, path, headers={"Authorization": token}, params=params, json=body)
        response.raise_for_status()
        return response.json()

async def dispatch(method: str, params: dict, token: str):
    if not token.startswith("Bearer "):
        raise HTTPException(401, "Bearer token required")
    # Fail closed before any order operation.
    identity = await call_store("/auth/me", token)
    if identity.get("merchant") is None:
        raise HTTPException(403, "Token is not associated with a merchant")
    if method == "list_orders":
        result = await call_store("/orders", token, params={"status": params.get("status")})
        return {"orders": [clean_order(o) for o in result["orders"]]}
    if method == "lookup_order":
        result = await call_store("/orders/lookup", token, params={"id": str(params["id"])})
        if result.get("status") is not True:
            return {"found": False, "message": result.get("message", "Order not found")}
        return {"found": True, "order": clean_order(result["order"])}
    if method in {"propose_refund", "propose_cancel"}:
        result = await call_store("/orders/lookup", token, params={"id": str(params["id"])})
        if result.get("status") is not True:
            return {"found": False, "message": "Order not found"}
        order = result["order"]
        allowed = {"PAID", "DELIVERED"} if method == "propose_refund" else {"PLACED", "PAID"}
        if order["status"] not in allowed:
            return {"found": False, "message": f"Cannot {method.removeprefix('propose_')} order in {order['status']} state"}
        return {"found": True, "proposal": {
            "order_id": order["id"], "merchant": identity["merchant"],
            "action": method.removeprefix("propose_"), "amount": order["total"],
            "current_status": order["status"],
            "expected_status": "REFUNDED" if method == "propose_refund" else "CANCELLED"
        }}
    if method in {"execute_refund", "execute_cancel"}:
        path = "/orders/refund" if method == "execute_refund" else "/orders/cancel"
        result = await call_store(path, token, method="POST", body={"id": str(params["id"])})
        return {"success": True, "order": clean_order(result["order"])}
    raise HTTPException(404, f"Unknown method: {method}")

@app.post("/rpc")
async def rpc(request: Request):
    payload = await request.json()
    rpc_id = payload.get("id")
    if payload.get("jsonrpc") != "2.0" or not payload.get("method"):
        return {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Invalid Request"}, "id": rpc_id}
    token = request.headers.get("authorization", "")
    try:
        result = await dispatch(payload["method"], payload.get("params") or {}, token)
        return {"jsonrpc": "2.0", "result": result, "id": rpc_id}
    except HTTPException as exc:
        return {"jsonrpc": "2.0", "error": {"code": exc.status_code, "message": str(exc.detail)}, "id": rpc_id}
    except httpx.HTTPStatusError as exc:
        return {"jsonrpc": "2.0", "error": {"code": exc.response.status_code, "message": exc.response.text}, "id": rpc_id}
    except Exception as exc:
        return {"jsonrpc": "2.0", "error": {"code": -32603, "message": str(exc)}, "id": rpc_id}
