
import os
import re
import json

import httpx
from fastapi import FastAPI, HTTPException

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCard,
    AgentCapabilities,
    AgentInterface,
    AgentSkill,
    Part,
)
from a2a.utils.constants import PROTOCOL_VERSION_CURRENT


STORE_API_URL = os.getenv("STORE_API_URL", "http://localhost:8001")
ORDERS_AGENT_URL = os.getenv(
    "ORDERS_AGENT_URL", "http://orders-agent:8002"
)

app = FastAPI(title="ShopHub Orders Agent")


# --------------------------------------------------
# 1. Agent Card: describes this agent to other agents
# --------------------------------------------------

AGENT_CARD = AgentCard(
    name="ShopHub Orders Agent",
    description=(
        "Handles merchant-scoped order listing, lookup, "
        "refund proposals and cancellation proposals."
    ),
    supported_interfaces=[
        AgentInterface(
            url=f"{ORDERS_AGENT_URL}/rpc",
            protocol_binding="JSONRPC",
            protocol_version=PROTOCOL_VERSION_CURRENT,
        )
    ],
    version="1.0.0",
    capabilities=AgentCapabilities(
        streaming=False,
        push_notifications=False,
    ),
    default_input_modes=["text"],
    default_output_modes=["text"],
    skills=[
        AgentSkill(
            id="list_orders",
            name="List orders",
            description="Lists orders belonging to the authenticated merchant.",
            tags=["orders", "list"],
            examples=['{"method":"list_orders","params":{"status":"PAID"}}'],
        ),
        AgentSkill(
            id="lookup_order",
            name="Look up an order",
            description="Looks up an order belonging to the authenticated merchant.",
            tags=["orders", "lookup"],
            examples=['{"method":"lookup_order","params":{"id":"1001"}}'],
        ),
        AgentSkill(
            id="propose_refund",
            name="Prepare refund proposal",
            description="Prepares a refund proposal without executing it.",
            tags=["refund", "approval"],
        ),
        AgentSkill(
            id="propose_cancel",
            name="Prepare cancellation proposal",
            description="Prepares a cancellation proposal without executing it.",
            tags=["cancellation", "approval"],
        ),
    ],
)


# --------------------------------------------------
# 2. Existing business logic: preserved
# --------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


def clean_order(order: dict) -> dict:
    """Redact PII and neutralize instruction-like customer notes."""
    cleaned = dict(order)
    cleaned["customer_email"] = "[REDACTED]"
    cleaned["customer_phone"] = "[REDACTED]"

    note = str(cleaned.get("customer_note", ""))
    if re.search(
        r"(ignore (all )?(previous )?instructions|assistant:|"
        r"reveal .*phone|refund this order immediately)",
        note,
        re.I,
    ):
        cleaned["customer_note"] = (
            "[UNTRUSTED INSTRUCTION-LIKE NOTE REMOVED]"
        )

    return cleaned


async def call_store(
    path: str,
    token: str,
    method="GET",
    params=None,
    body=None,
):
    async with httpx.AsyncClient(
        base_url=STORE_API_URL,
        timeout=10,
    ) as client:
        response = await client.request(
            method,
            path,
            headers={"Authorization": token},
            params=params,
            json=body,
        )
        response.raise_for_status()
        return response.json()


async def dispatch(method: str, params: dict, token: str):
    if not token.startswith("Bearer "):
        raise HTTPException(401, "Bearer token required")

    # Fail closed before any order operation.
    identity = await call_store("/auth/me", token)

    if identity.get("merchant") is None:
        raise HTTPException(
            403,
            "Token is not associated with a merchant",
        )

    if method == "list_orders":
        result = await call_store(
            "/orders",
            token,
            params={"status": params.get("status")},
        )
        return {
            "orders": [clean_order(o) for o in result["orders"]]
        }

    if method == "lookup_order":
        result = await call_store(
            "/orders/lookup",
            token,
            params={"id": str(params["id"])},
        )

        if result.get("status") is not True:
            return {
                "found": False,
                "message": result.get("message", "Order not found"),
            }

        return {
            "found": True,
            "order": clean_order(result["order"]),
        }

    if method in {"propose_refund", "propose_cancel"}:
        result = await call_store(
            "/orders/lookup",
            token,
            params={"id": str(params["id"])},
        )

        if result.get("status") is not True:
            return {"found": False, "message": "Order not found"}

        order = result["order"]

        allowed = (
            {"PAID", "DELIVERED"}
            if method == "propose_refund"
            else {"PLACED", "PAID"}
        )

        if order["status"] not in allowed:
            action = method.removeprefix("propose_")
            return {
                "found": False,
                "message": (
                    f"Cannot {action} order in {order['status']} state"
                ),
            }

        return {
            "found": True,
            "proposal": {
                "order_id": order["id"],
                "merchant": identity["merchant"],
                "action": method.removeprefix("propose_"),
                "amount": order["total"],
                "current_status": order["status"],
                "expected_status": (
                    "REFUNDED"
                    if method == "propose_refund"
                    else "CANCELLED"
                ),
            },
        }

    if method in {"execute_refund", "execute_cancel"}:
        path = (
            "/orders/refund"
            if method == "execute_refund"
            else "/orders/cancel"
        )

        result = await call_store(
            path,
            token,
            method="POST",
            body={"id": str(params["id"])},
        )

        return {
            "success": True,
            "order": clean_order(result["order"]),
        }

    raise HTTPException(404, f"Unknown method: {method}")


# --------------------------------------------------
# 3. A2A Executor: processes incoming agent messages
# --------------------------------------------------

class OrdersAgentExecutor(AgentExecutor):

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:

        updater = TaskUpdater(
            event_queue,
            context.task_id,
            context.context_id,
        )

        user_input = context.get_user_input()

        # Keep all your remaining existing code unchanged

        try:
            # Extract the text from the incoming A2A message.
            user_input = context.get_user_input()

            # Our message convention carries the operation as JSON text.
            request_data = json.loads(user_input)

            method = request_data.get("method")
            params = request_data.get("params") or {}

            if not method:
                raise ValueError("A method is required")

            # Get the original Authorization header.
            headers = context.call_context.state.get("headers", {})
            token = headers.get("authorization", "")

            # Call the existing business logic.
            result = await dispatch(method, params, token)

            response_data = {
                "result": result,
            }

        except HTTPException as exc:
            response_data = {
                "error": {
                    "code": exc.status_code,
                    "message": str(exc.detail),
                }
            }

        except httpx.HTTPStatusError as exc:
            response_data = {
                "error": {
                    "code": exc.response.status_code,
                    "message": exc.response.text,
                }
            }

        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            response_data = {
                "error": {
                    "code": -32602,
                    "message": str(exc),
                }
            }

        except Exception:
            # Avoid exposing internal exception details to other agents.
            response_data = {
                "error": {
                    "code": -32603,
                    "message": "Internal error while processing order request",
                }
            }

        # Convert the result into an A2A agent message.
        response_message = updater.new_agent_message(
            parts=[
                Part(
                    text=json.dumps(
                        response_data,
                        ensure_ascii=False,
                        default=str,
                    )
                )
            ]
        )

        # Publish the final message and complete the task.
        await event_queue.enqueue_event(response_message)

    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        # No long-running operation currently needs cancellation.
        return


# --------------------------------------------------
# 4. Register the official A2A SDK server routes
# --------------------------------------------------

request_handler = DefaultRequestHandler(
    agent_executor=OrdersAgentExecutor(),
    task_store=InMemoryTaskStore(),
    agent_card=AGENT_CARD,
)

app.router.routes.extend(
    create_jsonrpc_routes(
        request_handler=request_handler,
        rpc_url="/rpc",
    )
)

app.router.routes.extend(
    create_agent_card_routes(
        agent_card=AGENT_CARD,
    )
)
