"""Five deterministic, API-key-free scripted conversations."""
import asyncio
from orchestrator.graph import build_graph
import orchestrator.graph as graph_module

async def fake_call(method, params, bearer):
    if bearer == "Bearer merchant-a-token" and method == "list_orders":
        return {"orders": [
            {"id":"1001","status":"PAID","total":2000.0},
            {"id":"1002","status":"PAID","total":1500.0}
        ]}
    if method == "lookup_order" and params.get("id") == "1001":
        return {"found": True, "order": {"id":"1001","customer_name":"Maria","status":"PAID","total":2000.0}}
    if method == "lookup_order" and params.get("id") == "2001":
        return {"found": False, "message":"Order not found"}
    if method == "propose_refund":
        return {"found": True, "proposal": {
            "order_id": params["id"], "merchant":"merchant-a", "action":"refund",
            "amount":2000.0, "current_status":"PAID", "expected_status":"REFUNDED"
        }}
    return {"orders": []}

async def main():
    graph_module.call_orders_agent = fake_call
    graph = build_graph()
    cases = [
        ("paid orders", "Show my paid orders", "Found 2 order(s)", "Bearer merchant-a-token"),
        ("order lookup", "Find order 1001", "Order 1001", "Bearer merchant-a-token"),
        ("refund proposal", "Refund order 1001", "Approval required", "Bearer merchant-a-token"),
        ("injection note", "Show my paid orders", "Found 2 order(s)", "Bearer merchant-a-token"),
        ("cross-merchant lookup", "Find order 2001", "Order not found", "Bearer merchant-a-token"),
    ]
    passed = 0
    for name, message, expected, token in cases:
        try:
            result = await graph.ainvoke({"message": message, "bearer": token})
            ok = expected in result.get("response", "")
        except Exception:
            ok = False
        print(f"{name}: {'PASS' if ok else 'FAIL'}")
        passed += int(ok)
    print(f"Result: {passed}/{len(cases)} passed")
    if passed != len(cases):
        raise SystemExit(1)

if __name__ == "__main__":
    asyncio.run(main())
