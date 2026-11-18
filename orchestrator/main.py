import os, json, uuid, asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import httpx
from fastapi import Depends
from .graph import build_graph
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from .approval_store import (
    initialize_approval_store,
    save_proposal,
    get_proposal,
    resolve_proposal,
)

security = HTTPBearer(auto_error=False)

DATABASE_URL = os.getenv("DATABASE_URL")
checkpointer = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global checkpointer

    saver_context = None

    if DATABASE_URL:
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            saver_context = AsyncPostgresSaver.from_conn_string(DATABASE_URL)
            checkpointer = await saver_context.__aenter__()

            await checkpointer.setup()

        except Exception as exc:
            checkpointer = None
            app.state.checkpointer_error = str(exc)

    
    if DATABASE_URL:
        try:
            await initialize_approval_store()
            app.state.approval_store_error = None
        except Exception as exc:
            app.state.approval_store_error = str(exc)

    app.state.graph = build_graph(checkpointer)

    yield

    if saver_context is not None:
        await saver_context.__aexit__(None, None, None)

app = FastAPI(title="ShopHub Orchestrator Chat API", lifespan=lifespan)
PROPOSALS: dict[str, dict] = {}


class ChatRequest(BaseModel):
    thread_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    message: str

class ApprovalRequest(BaseModel):
    decision: str

def require_bearer(value: str | None):
    if not value or not value.startswith("Bearer "):
        raise HTTPException(401, "Bearer token required")
    return value

async def verify_merchant(bearer: str):
    store_url = os.getenv("STORE_API_URL", "http://localhost:8001")
    async with httpx.AsyncClient(base_url=store_url, timeout=10) as client:
        response = await client.get("/auth/me", headers={"Authorization": bearer})
        response.raise_for_status()
        identity = response.json()
    if identity.get("merchant") is None:
        raise HTTPException(403, "Token is valid but is not associated with a merchant")
    return identity

@app.get("/health")
def health():
    return {
        "status": "ok",
        "persistence": "postgres" if checkpointer else "unavailable",
        "checkpointer_error": getattr(
            app.state, "checkpointer_error", None
        )
    }

@app.post("/chat")
async def chat(
    body: ChatRequest,
    credentials: HTTPAuthorizationCredentials | None = Depends(security)
):
    bearer = require_bearer(
        f"Bearer {credentials.credentials}" if credentials else None
    )

    identity = await verify_merchant(bearer)
    graph = app.state.graph
    config = {"configurable": {"thread_id": body.thread_id}}

    try:
        result = await graph.ainvoke(
            {"message": body.message, "bearer": bearer},
            config=config
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            exc.response.status_code,
            "Store service rejected the request"
        )
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))

    if result.get("proposal"):
        await save_proposal(
            proposal_id=result["proposal_id"],
            merchant=identity["merchant"],
            thread_id=body.thread_id,
            proposal=result["proposal"]
        )

    return {
        "thread_id": body.thread_id,
        "merchant": identity["merchant"],
        "response": result.get("response"),
        "proposal_id": result.get("proposal_id")
    }

@app.post("/chat/stream")
async def chat_stream(
    body: ChatRequest,
    credentials: HTTPAuthorizationCredentials | None = Depends(security)
):
    bearer = require_bearer(
        f"Bearer {credentials.credentials}" if credentials else None
    )
    identity = await verify_merchant(bearer)

    async def event_stream():
        yield f"event: tool_started\ndata: {json.dumps({'tool': 'orchestrator'})}\n\n"

        try:
            result = await app.state.graph.ainvoke(
                {"message": body.message, "bearer": bearer},
                config={
                    "configurable": {
                        "thread_id": body.thread_id
                    }
                }
            )

            yield f"event: tool_finished\ndata: {json.dumps({'intent': result.get('intent')})}\n\n"

            if result.get("proposal"):
                await save_proposal(
                    proposal_id=result["proposal_id"],
                    merchant=identity["merchant"],
                    thread_id=body.thread_id,
                    proposal=result["proposal"]
                )

            yield f"event: response\ndata: {json.dumps({'text': result.get('response'), 'proposal_id': result.get('proposal_id')})}\n\n"
            yield "event: done\ndata: {}\n\n"

        except Exception as exc:
            yield f"event: error\ndata: {json.dumps({'message': str(exc)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream"
    )

# @app.post("/chat/stream")
# async def chat_stream(
#     body: ChatRequest,
#     credentials: HTTPAuthorizationCredentials | None = Depends(security)
# ):
#     bearer = require_bearer(
#         f"Bearer {credentials.credentials}" if credentials else None
#     )

# @app.post("/approvals/{proposal_id}")
# async def approval(proposal_id: str, body: ApprovalRequest, authorization: str | None = Header(default=None)):
#     bearer = require_bearer(authorization)
#     identity = await verify_merchant(bearer)
#     item = PROPOSALS.get(proposal_id)
#     if not item or item["merchant"] != identity["merchant"]:
#         raise HTTPException(404, "Proposal not found")
#     if body.decision not in {"approve", "reject"}:
#         raise HTTPException(422, "decision must be approve or reject")
#     if item.get("resolved"):
#         raise HTTPException(409, "Proposal already resolved")
#     item["resolved"] = True
#     if body.decision == "reject":
#         return {"proposal_id": proposal_id, "decision": "rejected", "executed": False}
#     action = item["proposal"]["action"]
#     method = "execute_refund" if action == "refund" else "execute_cancel"
#     from .agent_client import call_orders_agent
#     result = await call_orders_agent(method, {"id": item["proposal"]["order_id"]}, bearer)
#     return {"proposal_id": proposal_id, "decision": "approved", "executed": True, "result": result}



@app.post("/approvals/{proposal_id}")
async def approval(
    proposal_id: str,
    body: ApprovalRequest,
    credentials: HTTPAuthorizationCredentials | None = Depends(security)
):
    bearer = require_bearer(
        f"Bearer {credentials.credentials}" if credentials else None
    )
    identity = await verify_merchant(bearer)

    if body.decision not in {"approve", "reject"}:
        raise HTTPException(422, "decision must be approve or reject")

    # Retrieve the proposal from PostgreSQL
    item = await get_proposal(proposal_id, identity["merchant"])

    if not item:
        raise HTTPException(404, "Proposal not found")

    if item["status"] != "pending":
        raise HTTPException(409, "Proposal already resolved")

    # Claim the proposal so it cannot be resolved twice
    claimed = await resolve_proposal(
        proposal_id,
        identity["merchant"],
        "processing"
    )

    if not claimed:
        raise HTTPException(409, "Proposal already resolved")

    if body.decision == "reject":
        await resolve_proposal(
            proposal_id,
            identity["merchant"],
            "rejected",
            expected_status="processing"
        )
        return {
            "proposal_id": proposal_id,
            "decision": "rejected",
            "executed": False
        }

    action = item["proposal"]["action"]
    method = "execute_refund" if action == "refund" else "execute_cancel"

    from .agent_client import call_orders_agent

    try:
        result = await call_orders_agent(
            method,
            {"id": item["proposal"]["order_id"]},
            bearer
        )
    except Exception:
        # Keep the proposal in processing state to prevent duplicate execution.
        raise HTTPException(502, "Action execution failed; proposal requires review")

    await resolve_proposal(
        proposal_id,
        identity["merchant"],
        "approved",
        expected_status="processing"
    )

    return {
        "proposal_id": proposal_id,
        "decision": "approved",
        "executed": True,
        "result": result
    }
