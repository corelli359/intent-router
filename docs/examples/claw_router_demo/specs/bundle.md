---
bundle_code: claw_router_demo
version: 0.1.0
default_locale: zh-CN
---

# Claw Router Demo Bundle

This bundle is the entrypoint for one router runtime version.

The runtime must bind a session to this bundle version before it interprets any
user input. A run may load more detailed specs later, but it should not mix
bundle versions inside the same session.

## Progressive Loading Levels

- L0 Bootstrap: bundle, runtime contract, action contract.
- L1 Session: user profile, permissions, memory summary, current run state.
- L2 Catalog Index: skill metadata and match policy only.
- L3 Candidate Card: short capability and slot summary for shortlisted skills.
- L4 Deep Skill Spec: extraction hints, confirmation policy, presentation hints.
- L5 Knowledge: business knowledge chunks needed for the current skill.
- L6 Executor Contract: execution backend contract loaded only before side effects.

