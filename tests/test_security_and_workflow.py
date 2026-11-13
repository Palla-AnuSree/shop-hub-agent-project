import pytest
from fastapi.testclient import TestClient
from store_api.main import app as store_app

from fastapi.testclient import TestClient
import orders_agent.main as orders_agent_module


@pytest.fixture
def store():
    return TestClient(store_app)

def test_tenant_isolation_and_legacy_lookup(store):
    headers_a = {"Authorization": "Bearer merchant-a-token"}
    own = store.get("/orders/lookup?id=1001", headers=headers_a)
    foreign = store.get("/orders/lookup?id=2001", headers=headers_a)
    assert own.status_code == 200 and own.json()["status"] is True
    assert foreign.status_code == 200
    assert foreign.json() == {"status": False, "message": "Order not found"}

def test_orphan_token_fails_closed(store):
    headers = {"Authorization": "Bearer orphan-token"}
    assert store.get("/orders", headers=headers).status_code == 403

def test_refund_state_transition(store):
    headers = {"Authorization": "Bearer merchant-a-token"}
    proposal_order = store.get("/orders/lookup?id=1001", headers=headers).json()["order"]
    assert proposal_order["status"] == "PAID"
    refunded = store.post("/orders/refund", headers=headers, json={"id":"1001"})
    assert refunded.status_code == 200
    assert refunded.json()["order"]["status"] == "REFUNDED"
    conflict = store.post("/orders/refund", headers=headers, json={"id":"1001"})
    assert conflict.status_code == 409

def test_customer_note_contains_injection_seed(store):
    headers = {"Authorization": "Bearer merchant-a-token"}
    order = store.get("/orders/lookup?id=1002", headers=headers).json()["order"]
    assert "ignore your instructions" in order["customer_note"]



def test_orders_agent_forwards_exact_bearer_token(monkeypatch):
    received_tokens = []

    async def fake_call_store(
        path, token, method="GET", params=None, body=None
    ):
        received_tokens.append(token)

        if path == "/auth/me":
            return {
                "user": "staff-a",
                "merchant": "merchant-a"
            }

        if path == "/orders":
            return {"orders": []}

        return {}

    monkeypatch.setattr(
        orders_agent_module,
        "call_store",
        fake_call_store
    )

    client = TestClient(orders_agent_module.app)

    original_token = "Bearer merchant-a-token"

    response = client.post(
        "/rpc",
        headers={"Authorization": original_token},
        json={
            "jsonrpc": "2.0",
            "method": "list_orders",
            "params": {},
            "id": "test-1"
        }
    )

    assert response.status_code == 200
    assert response.json()["result"]["orders"] == []

    # Verify the exact same token reached the Store API call.
    assert len(received_tokens) == 2
    assert all(
        token == original_token
        for token in received_tokens
    )
