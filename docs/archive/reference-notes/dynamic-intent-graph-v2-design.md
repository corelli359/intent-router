# Dynamic Intent Graph V2 Design

## 1. Goal

V2 adds a graph-oriented runtime beside the existing V1 serial task queue.

Target capabilities:

- multi-intent recognition before graph construction
- intent relation inference and graph factory output
- node-level multi-turn interaction
- user cancel current node or cancel current graph
- user switch intent mid-flow and trigger replanning
- separate V2 chat entry under `/chat/v2`
- separate V2 router API under `/api/router/v2`

V2 does not replace V1. Both stay available in the same codebase.

## 2. Why Not LLMCompiler Runtime Directly

Current project already has most of the non-planner capabilities that LLMCompiler lacks:

- session lifecycle
- SSE event delivery
- waiting/resume
- agent HTTP protocol
- slot memory
- cancel and switch handling

So V2 keeps the existing router and agent protocol foundation, and only upgrades the planning and scheduling layer from queue to graph.

## 3. Runtime Shape

Pipeline:

1. `IntentRecognizer` extracts primary and candidate intents.
2. `IntentGraphPlanner` orders intents and infers relation edges.
3. `ExecutionGraphState` is created as the graph factory output.
4. `GraphRouterOrchestrator` activates ready nodes and dispatches them one by one.
5. A waiting node can either:
   - resume with user supplements
   - be cancelled explicitly
   - be replaced by a new graph if the user switches intent

Current scheduling policy is deliberately conservative:

- graph model is dynamic
- execution is still single-foreground-node
- this keeps memory flat and avoids concurrent interactive agents competing for the same user turn

## 4. Core Models

Backend V2 models live in:

- `backend/src/router_core/v2_domain.py`
- `backend/src/router_core/v2_planner.py`
- `backend/src/router_core/v2_orchestrator.py`

Main abstractions:

- `ExecutionGraphState`
- `GraphNodeState`
- `GraphEdge`
- `GraphStatus`
- `GraphNodeStatus`

Important design choice:

- node execution still reuses the existing `Task` and `StreamingAgentClient`
- graph adds dependency, activation, waiting, cancel, and replan semantics above that layer

## 5. API Contract

V2 routes:

- `POST /api/router/v2/sessions`
- `GET /api/router/v2/sessions/{session_id}`
- `POST /api/router/v2/sessions/{session_id}/messages`
- `POST /api/router/v2/sessions/{session_id}/messages/stream`
- `POST /api/router/v2/sessions/{session_id}/actions`
- `POST /api/router/v2/sessions/{session_id}/actions/stream`
- `GET /api/router/v2/sessions/{session_id}/events`

Router actions currently include:

- `confirm_graph`
- `cancel_graph`
- `cancel_node`

## 6. Frontend Shape

V2 frontend entry is `/chat/v2`.

The V2 page is isolated from V1:

- V1 `/chat` stays untouched
- V2 page uses `/api/router/v2`
- V2 page shows graph status, nodes, edges, current active node, and event timeline

The V2 frontend should not depend on mutating the shared V1 `api-client` defaults.

## 7. Memory Strategy

V2 is implemented as parallel code paths inside the existing chat-web and router-api services.

Reason:

- current Kubernetes requests are already below the 7.5G budget
- sharing the existing deployments is still more memory-efficient than cloning a full second runtime plane
- ingress prefix routing already covers `/chat/v2` under `/chat` and `/api/router/v2` under `/api/router`

This keeps steady-state memory close to the current baseline while still exposing a full V2 feature set.

## 8. Current Limits

The current V2 is intentionally pragmatic, not maximal:

- relation inference is heuristic, not full LLM replanning
- foreground execution is serialized
- node-level confirmation is reserved in the state model, but current built-in demo agents mostly use `waiting_user_input`
- branch-local graph merge is still simpler than a full persistent graph revision engine

These are acceptable for the current phase because the main goal is to move from queue orchestration to a workable graph runtime without destabilizing V1.
