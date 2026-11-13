import os, re, uuid
from typing import TypedDict, Any
from langgraph.graph import StateGraph, START, END
from .agent_client import call_orders_agent
from .llm import classify_request

class ChatState(TypedDict, total=False):
    message: str
    bearer: str
    response: str
    events: list[dict]
    proposal: dict
    proposal_id: str
    intent: str
    params: dict
    messages: list[dict]

def parse_intent(message: str):
    text = message.lower()
    match = re.search(r"\b(?:order\s*)?#?(\d{3,})\b", text)
    order_id = match.group(1) if match else None
    if "refund" in text:
        return "propose_refund", {"id": order_id} if order_id else {}
    if "cancel" in text:
        return "propose_cancel", {"id": order_id} if order_id else {}
    if order_id and any(x in text for x in ("find", "lookup", "show", "details", "tell me")):
        return "lookup_order", {"id": order_id}
    status = next((s for s in ["placed", "paid", "shipped", "delivered", "cancelled", "refunded"] if s in text), None)
    return "list_orders", {"status": status.upper() if status else None}

def build_graph(checkpointer=None):

    # 1. Helper function to save assistant responses
    def save_response(state: ChatState, response: str, **extra):
        history = state.get("messages", [])
        updated_history = history + [
            {"role": "assistant", "content": response}
        ]
        return {
            **extra,
            "response": response,
            "messages": updated_history
        }

    # 2. Understand node
    async def understand(state: ChatState):
        history = state.get("messages", [])

        intent, params = await classify_request(
            state["message"],
            history
        )

        updated_history = history + [
            {"role": "user", "content": state["message"]}
        ]

        return {
            "intent": intent,
            "params": params,
            "messages": updated_history,
            "events": [
                {"event": "intent_selected", "data": intent}
            ]
        }

    # 3. Execute node
    
    async def execute(state: ChatState):
        intent, params = state["intent"], state["params"]
        
        if intent == "conversation_memory":
            code = params.get("reference_code")
            response = (
                f"Your reference code is {code}."
                if code
                else "I couldn't find a reference code in our conversation."
            )
            return save_response(state, response)

        if intent in {"propose_refund", "propose_cancel"} and not params.get("id"):
            return save_response(
                state,
                "Please provide an order ID before requesting this action."
            )

        result = await call_orders_agent(intent, params, state["bearer"])

        if intent.startswith("propose_"):
            if not result.get("found"):
                return save_response(
                    state,
                    result.get("message", "Order not found")
                )

            proposal = result["proposal"]
            proposal_id = str(uuid.uuid4())

            response = (
                f"Approval required: {proposal['action']} "
                f"order {proposal['order_id']} for {proposal['amount']:.2f}. "
                f"Proposal ID: {proposal_id}. "
                "No action has been executed."
            )

            return save_response(
                state,
                response,
                proposal=proposal,
                proposal_id=proposal_id
            )

        if intent == "lookup_order":
            if not result.get("found"):
                return save_response(
                    state,
                    result.get("message", "Order not found")
                )

            order = result["order"]

            response = (
                f"Order {order['id']} belongs to {order['customer_name']}. "
                f"Status: {order['status']}. "
                f"Total: {order['total']:.2f}. "
                "Customer contact details are redacted."
            )

            return save_response(state, response)

        orders = result["orders"]

        if not orders:
            return save_response(
                state,
                "No matching orders were found for your merchant."
            )

        summary = "; ".join(
            f"{order['id']} ({order['status']}, {order['total']:.2f})"
            for order in orders
        )

        response = f"Found {len(orders)} order(s): {summary}."

        return save_response(state, response)


    # 4. Build LangGraph
    graph = StateGraph(ChatState)
    graph.add_node("understand", understand)
    graph.add_node("execute", execute)
    graph.add_edge(START, "understand")
    graph.add_edge("understand", "execute")
    graph.add_edge("execute", END)

    return graph.compile(checkpointer=checkpointer)