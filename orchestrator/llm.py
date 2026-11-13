"""Single model factory. Fake mode is deterministic and requires no API key."""
import os, json, re

def get_chat_model():
    mode = os.getenv("LLM_MODE", "fake").lower()
    if mode == "fake":
        return None
    if mode == "openai":
        from langchain_openai import ChatOpenAI
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is required when LLM_MODE=openai")
        return ChatOpenAI(model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"), temperature=0)
    raise ValueError(f"Unsupported LLM_MODE: {mode}")


async def classify_request(message: str, history: list[dict] | None = None):
    """Classify order requests and handle reference-code follow-ups."""

    history = history or []
    model = get_chat_model()

    if model is None:
        # Handle a follow-up that asks about previously shared information.
        text = message.lower()

        if any(phrase in text for phrase in (
            "what was my reference code",
            "what is my reference code",
            "what did i tell you",
            "what was the code"
        )):
            for item in reversed(history):
                if item.get("role") == "user":
                    match = re.search(
                        r"reference code is\s+([A-Za-z0-9-]+)",
                        item.get("content", ""),
                        re.I
                    )
                    if match:
                        return "conversation_memory", {
                            "reference_code": match.group(1)
                        }

            return "conversation_memory", {"reference_code": None}

        from .graph import parse_intent
        return parse_intent(message)

    conversation = "\n".join(
        f"{item['role']}: {item['content']}"
        for item in history[-10:]
    )

    prompt = (
        "Classify this order-support request. Return ONLY JSON with keys "
        "intent and params. Allowed intents: list_orders, lookup_order, "
        "propose_refund, propose_cancel. params may contain id and status. "
        "Never invent an order id.\n"
        f"Previous conversation:\n{conversation}\n"
        f"Current user request: {message}"
    )

    response = await model.ainvoke(prompt)
    content = response.content if hasattr(response, "content") else str(response)

    match = re.search(r"\{.*\}", content, re.S)
    if not match:
        raise ValueError("Model did not return valid JSON")

    data = json.loads(match.group(0))
    allowed = {
        "list_orders", "lookup_order",
        "propose_refund", "propose_cancel"
    }

    if data.get("intent") not in allowed or not isinstance(data.get("params", {}), dict):
        raise ValueError("Model returned an unsupported action")

    return data["intent"], data.get("params", {})
