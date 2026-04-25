# Claw Router Demo

This demo is a markdown-first router harness. It intentionally does not reuse the
current router-service implementation.

The goal is to show the open version of a router:

1. Markdown files describe runtime rules, skills, policies, fields, and executors.
2. The harness progressively loads only the needed specs for each user turn.
3. A decision engine emits structured actions.
4. The harness validates and executes those actions.
5. The whole turn is projected as SSE-style events.

Run it from the repository root:

```bash
python scripts/demo_claw_router.py --demo transfer
python scripts/demo_claw_router.py --demo confirmation
python scripts/demo_claw_router.py --interactive
```

The bundled decision engine is deterministic so the demo runs without an LLM API
key. It is deliberately shaped like a model boundary: swap the decision engine
for a real LLM and keep the markdown specs, progressive loader, action contract,
validation, and executor layer unchanged.

