# ShopHub Multi-Tenant AI Order Support Assistant

A runnable reference implementation for the Senior AI Agent Engineer take-home task.

## Architecture

- `store_api`: FastAPI in-memory mock store with merchant-scoped bearer tokens.
- `orders_agent`: independent FastAPI JSON-RPC 2.0 service; publishes `/.well-known/agent-card.json`.
- `orchestrator`: FastAPI chat service using LangGraph; discovers the Orders Agent from its card and calls it over HTTP.
- PostgreSQL: LangGraph checkpoint persistence.
- SSE: `/chat/stream` emits tool lifecycle and response events.
- Fake LLM: default, deterministic, no API key required.

The implementation deliberately uses a deterministic fake model for offline execution. Set `LLM_MODE=openai` and `OPENAI_API_KEY` to enable the optional OpenAI adapter.
See [DESIGN.md](DESIGN.md) for architecture details, request flow,
security decisions, trade-offs, latency estimates, and future improvements.

## Quick start

Requirements: Docker Engine + Docker Compose.

```bash
cp .env.example .env
docker compose up --build
```

Services:
- Chat/orchestrator: http://localhost:8000
- Orders Agent: http://localhost:8002
- Store API: http://localhost:8001
- Agent Card: http://localhost:8002/.well-known/agent-card.json

Seed credentials:
- Merchant A: `merchant-a-token`
- Merchant B: `merchant-b-token`
- Valid token without merchant: `orphan-token`

OpenAPI docs:
- http://localhost:8000/docs
- http://localhost:8001/docs
- http://localhost:8002/docs

## Example: list orders

```bash
curl -N http://localhost:8000/chat/stream \
  -H 'Authorization: Bearer merchant-a-token' \
  -H 'Content-Type: application/json' \
  -d '{"thread_id":"demo-1","message":"Show my paid orders"}'
```

## Example: lookup order

```bash
curl -N http://localhost:8000/chat/stream \
  -H 'Authorization: Bearer merchant-a-token' \
  -H 'Content-Type: application/json' \
  -d '{"thread_id":"demo-2","message":"Find order 1001"}'
```

## Example: propose a refund

```bash
curl -s http://localhost:8000/chat \
  -H 'Authorization: Bearer merchant-a-token' \
  -H 'Content-Type: application/json' \
  -d '{"thread_id":"refund-1","message":"Refund order 1001"}'
```

The response contains a `proposal_id`. Approve or reject it explicitly:

```bash
curl -X POST http://localhost:8000/approvals/<proposal_id> \
  -H 'Authorization: Bearer merchant-a-token' \
  -H 'Content-Type: application/json' \
  -d '{"decision":"approve"}'
```

Use `{"decision":"reject"}` to reject. Approval is bound to the merchant that created the proposal.

## Run tests without an API key
  The automated test suite currently contains 7 tests covering approval,
  rejection, refund execution, tenant isolation, orphan-token handling,
  and customer-note injection scenarios.

Install dependencies:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
pytest -q
python -m evals.run_eval
```

The tests use local ASGI clients and mocked service calls; they do not require Docker or an LLM key.

## Fake LLM mode

Fake mode is the default and is explicitly offline:

```bash
LLM_MODE=fake uvicorn orchestrator.main:app --port 8000
```

For local multi-service execution, start PostgreSQL and each service separately, or use Docker Compose. Set `LLM_MODE=openai` and `OPENAI_API_KEY` to enable the optional OpenAI model adapter; model selection is centralized in `orchestrator/llm.py`.

## Notes and trade-offs

- The mock store is intentionally in-memory; its seed data resets when the service restarts. The assignment asks for a small mock API, not durable order storage.
- The orchestrator and Orders Agent are separate services and communicate only through the discovered Agent Card endpoint and JSON-RPC HTTP.
- The fake model performs deterministic intent routing and is designed for reproducible evaluation. The optional OpenAI mode is a minimal adapter, not required for tests.
- The policy layer redacts email/phone values and neutralizes instruction-like customer notes before model-facing content is assembled. Tool data is always treated as untrusted.
- The approval endpoint is explicit and merchant-scoped. A proposal does not execute a refund/cancellation until approved.
- The implementation uses PostgreSQL-backed LangGraph checkpointing in Docker. For test execution, the graph can run without a checkpointer to keep tests self-contained.
- For a production deployment, add durable proposal storage, stronger PII detection, signed service-to-service identity, rate limiting, audit retention, and idempotency keys.

## Evaluation cases

`python -m evals.run_eval` reports individual pass/fail outcomes for:
1. Paid-order listing.
2. Order lookup.
3. Refund proposal without execution.
4. Prompt injection ignored and PII redacted.
5. Cross-merchant order access refused.
