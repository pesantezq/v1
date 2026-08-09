# ADR 0001 — Prime superseded by the StockBot-native R&D Control Plane

- **Status:** Accepted (Phase 0C, 2026-08-09)
- **Supersedes runtime role of:** Prime (agent framework) in the local R&D environment

## Context
The local StockBot R&D environment (WSL2 `StockBot-Agent-Lab` distro) originally
used **Prime** as the agent runtime, jailed behind a per-agent egress stack:
a private `agentjail` network namespace, a `tinyproxy` domain-allowlist egress
proxy (`agent-egress-proxy`), and a `socat` Ollama bridge (`agent-ollama-bridge`),
launched via `stockbot-prime-jailed` under the `stockbot-agent` account with
credentials in `~/.prime`.

Phases 0A/0B replaced this with a **framework-independent** design:
- **Phase 0A** — a StockBot-native R&D Control Plane: an authoritative SQLite
  registry with a CAS job-state machine; the worker's self-declared status is
  never trusted.
- **Phase 0B** — a generic, kernel-enforced sandbox (systemd transient service
  per job: per-job mount isolation, cgroup process/resource containment,
  `NoNewPrivileges`+empty caps, an `OFFLINE_LOCAL` netns whose only egress is an
  inference-only Ollama proxy, hostile-safe result ingestion, and
  runner-enforced timeout/cancel cgroup termination). Certified
  `SANDBOX_RUNNER_READY_WITH_QUALIFICATIONS` at commit `50babe8`.

## Decision
**Prime is superseded and removed as a runtime dependency.** Authority and
isolation live in deterministic, version-controlled StockBot code and the
systemd/kernel boundary — not in any third-party agent framework.

Replacement mapping:
- Prime agent runtime            → StockBot R&D Control Plane (Phase 0A) + generic sandbox (Phase 0B)
- `agentjail` netns + tinyproxy allowlist egress → `rdsbx-offline` netns, default-deny, inference-only Ollama proxy
- `agent-ollama-bridge` (socat)  → the inference-only proxy forwards directly to `127.0.0.1:11434`
- `stockbot-prime-jailed` / `~/.prime` creds → removed; the generic runner needs no framework login

## Rationale
Authority must remain **deterministic and framework-independent**: a research
worker is untrusted and replaceable, so the control plane — not the framework —
owns admission, validation, and lifecycle. Removing Prime eliminates a
credential-bearing external dependency and a broader (DNS-allowing) egress
surface, leaving a smaller, auditable, `$0`-recurring, local-first runtime.

## Consequences
- Any roadmap text mentioning a "future Agent Lab / Prime consumer" (e.g. the
  observe-only `agent_export` subsystem) is **historical**; the future consumer
  is the StockBot R&D Control Plane. Those production-adjacent modules are left
  unmodified in Phase 0C.
- Prime credentials/state were destroyed, not archived (no secret is preserved
  "for history").
- The generic path's Prime-independence was proven by systemd dependency-edge
  tracing and end-to-end certification with Prime stopped, then with Prime fully
  removed.
