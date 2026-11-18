# ShopHub Design Notes

## Architecture


Merchant Staff
    |
    | Bearer token + message + thread_id
    v
Chat API / LangGraph Orchestrator (port 8000)
    |-- Validates merchant identity through Store API
    |-- Loads/saves conversation state in PostgreSQL
    |-- Discovers Orders Agent through Agent Card
    |
    | JSON-RPC 2.0 over HTTP
    | Original Authorization header forwarded
    v
Orders Agent (port 8002)
    |-- list_orders
    |-- lookup_order
    |-- propose_refund / propose_cancel
    |-- execute_refund / execute_cancel
    |
    | HTTP + unchanged Authorization header
    v
Mock Store API (port 8001)
    |-- Validates bearer token
    |-- Enforces merchant-level data isolation
    |-- Manages in-memory mock orders
    v
Merchant-specific order data

PostgreSQL (port 5432)
    ^
    | LangGraph checkpoint persistence
    |
Chat API / LangGraph Orchestrator

## One-turn flow

1. The Chat API accepts a bearer token, message, and thread ID.
2. It validates `/auth/me`; a token without a merchant is rejected.
3. The LangGraph orchestrator parses intent in fake mode.
4. The orchestrator fetches the Orders Agent Card and sends a JSON-RPC request to its advertised endpoint.
5. The Orders Agent forwards the original bearer token to the store API.
6. The store API enforces merchant scoping.
7. The Orders Agent redacts email/phone and neutralizes instruction-like notes before returning model-facing data.
8. The orchestrator produces a response and streams lifecycle events when `/chat/stream` is used.
9. Refund/cancel requests create a proposal; only `/approvals/{proposal_id}` with explicit approval executes it.

## Security decisions

- Tenant identity is derived from the bearer token at the store API, never from an LLM-supplied merchant parameter.
- The original Authorization header is forwarded unchanged across the service boundary.
- The orphan token is valid at authentication but has no merchant; all order operations fail closed.
- Foreign and unknown order IDs have the same legacy lookup envelope, avoiding cross-tenant existence disclosure.
- PII is redacted before order data is returned from the sub-agent.
- Customer notes are treated as untrusted data; instruction-like notes are replaced with a marker.
- Approval proposals are bound to the merchant that created them.

## Trade-offs and cuts

- Fake mode uses deterministic intent parsing rather than probabilistic model tool-calling. This makes offline tests reproducible and avoids API keys.
- The mock store is in-memory and intentionally small; order data resets on restart.
- Proposal storage is in-memory in this reference build. A production version should persist proposals and use idempotency keys.
- The minimal graph uses two nodes. A production graph could add explicit policy, retry, validation, and response nodes.
- The implementation does not add a web UI, RAG/vector store, OAuth, payment gateway, or multiple model providers because these are explicitly not graded.
- The optional OpenAI model setting is documented as an extension point; fake mode is the tested default.
- Conversation state is persisted through PostgreSQL checkpoints, but the mock order data and approval proposals are in-memory. Therefore, conversation history can survive an orchestrator restart, while mock order changes and unresolved proposals are not guaranteed to survive service restarts.
- The Orders Agent uses the official A2A Python SDK for Agent Card
  publication, JSON-RPC request handling, and agent communication.
  The orchestrator uses the SDK client to discover the Agent Card
  and communicate over HTTP without importing the sub-agent directly.

## Rough cost and latency per turn

- Fake mode: no model/API cost; typically a few milliseconds to tens of milliseconds plus HTTP service latency.
- Real model mode: depends on provider, model, prompt size, and response length. Estimate cost from provider token pricing and measured input/output tokens.
- Store API and agent-to-agent HTTP calls are local Docker network calls; network and database checkpoint latency depend on environment.
- These are expected local-development estimates, not benchmark results. Actual latency should be measured under representative load. Real-model latency and cost depend on provider, model, token usage, and network conditions.

## With another week

1. Persist proposals in PostgreSQL with status, approver, timestamps, and idempotency.
2. Add explicit LangGraph `interrupt()` / resume approval flow and approval audit trail.
3. Add structured model tool-calling behind a model factory and test provider adapter.
4. Add stronger PII detection, sanitization tests, prompt-injection adversarial cases, and rate limits.
5. Add correlation IDs, structured logs, metrics, and distributed tracing.
6. Add integration tests that start all four containers from a clean environment.
