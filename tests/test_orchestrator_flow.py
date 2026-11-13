
from fastapi.testclient import TestClient
import orchestrator.main as api
from orchestrator import graph, agent_client
import pytest
import httpx
import respx


def test_refund_proposal_requires_explicit_approval(monkeypatch):
    async def fake_agent_call(method, params, token):
        if method == "propose_refund":
            return {
                "found": True,
                "proposal": {
                    "action": "refund",
                    "order_id": "ORD-1001",
                    "amount": 100,
                    "status": "pending_approval",
                },
            }
        return {"success": True}
    

    
    async def fake_verify_merchant(token):
        return {"merchant": "merchant-a"}

    monkeypatch.setattr(api, "verify_merchant", fake_verify_merchant)
    monkeypatch.setattr(graph, "call_orders_agent", fake_agent_call)
    monkeypatch.setattr(agent_client, "call_orders_agent", fake_agent_call)

    with TestClient(api.app) as client:
        response = client.post(
            "/chat",
            headers={"Authorization": "Bearer merchant-a-token"},
            json={"message": "Refund order ORD-1001"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data.get("proposal_id") is not None
    assert "Approval required" in data.get("response", "")


def test_approval_executes_only_after_approval(monkeypatch):
    executed = []

    async def fake_agent_call(method, params, token):
        if method == "propose_refund":
            return {
                "found": True,
                "proposal": {
                    "action": "refund",
                    "order_id": "ORD-1001",
                    "amount": 100,
                    "status": "pending_approval",
                },
            }
        if method == "execute_refund":
            executed.append(params)
            return {"success": True, "order": {"id": "ORD-1001"}}
        return {"found": True}

    async def fake_verify_merchant(token):
        return {"merchant": "merchant-a"}

    monkeypatch.setattr(api, "verify_merchant", fake_verify_merchant)
    monkeypatch.setattr(graph, "call_orders_agent", fake_agent_call)
    monkeypatch.setattr(agent_client, "call_orders_agent", fake_agent_call)

    with TestClient(api.app) as client:
        response = client.post(
            "/chat",
            headers={"Authorization": "Bearer merchant-a-token"},
            json={"message": "Refund order ORD-1001"},
        )

    assert response.status_code == 200
    data = response.json()
    approval_id = data.get("proposal_id")
    assert approval_id is not None

    
    approval_response = client.post(
    f"/approvals/{approval_id}",
    headers={"Authorization": "Bearer merchant-a-token"},
    json={"decision": "approve"},
)

    assert approval_response.status_code == 200
    assert len(executed) == 1


def test_rejected_refund_does_not_execute(monkeypatch):
    executed = []
    order_status = {"ORD-1001": "PAID"}

    async def fake_agent_call(method, params, token):
        if method == "propose_refund":
            return {
                "found": True,
                "proposal": {
                    "action": "refund",
                    "order_id": "ORD-1001",
                    "amount": 100,
                    "status": "pending_approval",
                },
            }

        if method == "execute_refund":
            executed.append(params)
            order_status["ORD-1001"] = "REFUNDED"
            return {"success": True}

        return {"found": True}

    async def fake_verify_merchant(token):
        return {"merchant": "merchant-a"}

    monkeypatch.setattr(api, "verify_merchant", fake_verify_merchant)
    monkeypatch.setattr(graph, "call_orders_agent", fake_agent_call)
    monkeypatch.setattr(agent_client, "call_orders_agent", fake_agent_call)

    with TestClient(api.app) as client:
        # Step 1: Create a refund proposal
        response = client.post(
            "/chat",
            headers={"Authorization": "Bearer merchant-a-token"},
            json={"message": "Refund order ORD-1001"},
        )

        assert response.status_code == 200
        proposal_id = response.json().get("proposal_id")
        assert proposal_id is not None

        # Step 2: Reject the proposal
        rejection_response = client.post(
            f"/approvals/{proposal_id}",
            headers={"Authorization": "Bearer merchant-a-token"},
            json={"decision": "reject"},
        )

        assert rejection_response.status_code == 200
        result = rejection_response.json()

        # Step 3: Verify that the refund was not executed
        assert result["decision"] == "rejected"
        assert result["executed"] is False
        assert executed == []

        # Step 4: Verify that the order remains PAID
        assert order_status["ORD-1001"] == "PAID"
    
@pytest.mark.asyncio
async def test_orchestrator_forwards_exact_bearer_token(monkeypatch):
    original_token = "Bearer merchant-a-token"

    # Mock Agent Card discovery, but use the real HTTP client function.
    async def fake_discover():
        return {
            "url": "http://orders-agent:8002/rpc"
        }

    monkeypatch.setattr(
        agent_client,
        "discover_orders_agent",
        fake_discover
    )

    # Intercept the actual HTTP request.
    with respx.mock(assert_all_called=True) as router:
        route = router.post(
            "http://localhost:8002/rpc"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "result": {"orders": []},
                    "id": "chat-call-1"
                }
            )
        )

        result = await agent_client.call_orders_agent(
            method="list_orders",
            params={},
            bearer=original_token
        )

        assert result == {"orders": []}
        assert route.called

        # Verify that the exact original token was forwarded.
        forwarded_request = route.calls[0].request

        assert forwarded_request.headers["Authorization"] == original_token


