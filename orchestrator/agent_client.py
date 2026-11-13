
import json
import os
import uuid

import httpx

from a2a.client import (
    A2ACardResolver,
    ClientCallContext,
    ClientCallInterceptor,
    ClientFactory,
    ClientConfig,
)
from a2a.client.interceptors import BeforeArgs, AfterArgs
from a2a.types import Message, Part, SendMessageRequest, Role


AGENT_BASE = os.getenv(
    "ORDERS_AGENT_URL",
    "http://localhost:8002",
)


class BearerTokenInterceptor(ClientCallInterceptor):
    """Forwards the original merchant token to the Orders Agent."""

    def __init__(self, bearer: str):
        self.bearer = bearer

    async def before(self, args: BeforeArgs) -> None:
        if args.context is None:
            args.context = ClientCallContext()

        if args.context.service_parameters is None:
            args.context.service_parameters = {}

        # Preserve the original Authorization value unchanged.
        args.context.service_parameters["Authorization"] = self.bearer
        args.context.service_parameters["A2A-Version"] = "1.0"

    async def after(self, args: AfterArgs) -> None:
        pass


async def discover_orders_agent():
    """Discover the Orders Agent through its Agent Card."""

    async with httpx.AsyncClient(timeout=10) as http_client:
        resolver = A2ACardResolver(http_client, AGENT_BASE)
        card = await resolver.get_agent_card()

        return card


async def call_orders_agent(method: str, params: dict, bearer: str):
    """Send an operation to the Orders Agent using the A2A SDK."""

    async with httpx.AsyncClient(timeout=20) as http_client:

        # 1. Discover the agent using its Agent Card.
        resolver = A2ACardResolver(http_client, AGENT_BASE)
        card = await resolver.get_agent_card()

        # 2. Support running the orchestrator directly on the host.
        for interface in card.supported_interfaces:
            if (
                interface.url.startswith("http://orders-agent:")
                and "localhost" in AGENT_BASE
            ):
                interface.url = interface.url.replace(
                    "http://orders-agent:",
                    "http://localhost:",
                )

        # 3. Create the official A2A SDK client.
        factory = ClientFactory(
            ClientConfig(
                streaming=False,
                httpx_client=http_client,
            )
        )

        client = factory.create(
            card,
            interceptors=[BearerTokenInterceptor(bearer)],
        )

        try:
            # 4. Put the requested operation inside the A2A message.
            operation = {
                "method": method,
                "params": params,
            }

            message = Message(
                message_id=str(uuid.uuid4()),
                role=Role.ROLE_USER,
                parts=[
                    Part(text=json.dumps(operation))
                ],
            )

            request = SendMessageRequest(
                message=message,
            )

            # 5. Send the message through the A2A protocol.
            response_text = None

            try:
                async for event in client.send_message(
                    request,
                    context=ClientCallContext(),
                ):
                    if event.HasField("message"):
                        for part in event.message.parts:
                            if part.text:
                                response_text = part.text
                                break

                    elif event.HasField("task"):
                        task = event.task

                        if task.HasField("status") and task.status.HasField("message"):
                            for part in task.status.message.parts:
                                if part.text:
                                    response_text = part.text
                                    break

                    if response_text:
                        break

            except Exception as exc:
                print("A2A CLIENT ERROR:", repr(exc))
                raise

            # 6. Decode the application-level response.
            payload = json.loads(response_text)

            if payload.get("error"):
                raise RuntimeError(
                    payload["error"]["message"]
                )

            return payload["result"]

        finally:
            await client.close()
