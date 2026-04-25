---
spec_code: runtime
version: 0.1.0
---

# Runtime Contract

The harness owns state, progressive spec loading, validation, execution, and event
projection.

The decision engine may propose the next action, but the harness decides whether
that action is valid and executable.

## Runtime Invariants

- Bind each session to one spec bundle version.
- Load the smallest useful spec subset for the current step.
- Treat every model decision as untrusted until validated.
- Do not execute a skill until required slots, guardrails, and executor contract
  are satisfied.
- Emit observable events for every material transition.

