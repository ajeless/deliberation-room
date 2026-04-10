# AGENTS.md — Deliberation Room

## Purpose

Deliberation Room is a tool where humans and LLMs participate as peers in structured, round-based deliberation to solve problems together. It is a shared reasoning room, not a chat app.

## Read These Reference Docs

- `docs/spec/deliberation-room-mvp.md` — product source of truth
- `docs/architecture/overview.md` — system boundary and module responsibilities
- `docs/architecture/contracts.md` — canonical schemas and interface contracts
- `docs/architecture/flows.md` — canonical event flows, persistence model, and instrumentation
- `docs/PLAN.md` — implementation sequence and milestones
- `docs/STACK-DECISION.md` — resolved Phase 0 implementation choice and post-MVP direction

## Core Design Principles

1. **Structured over real-time.** Deliberation, not conversation simulation.
2. **Role-first, model-second.** Users choose agent roles; the system resolves backing models.
3. **Shared cognition over raw transcript.** The structured state object is the product, not the chat log.
4. **Human-controlled source of truth.** The system maintains state; humans correct it.
5. **Protocol simplicity in V1.** One cadence, one room, one workflow.

## Anti-Goals

Do not turn this project into:
- a real-time chat product
- a model comparison playground
- an autonomous agent swarm

Humans remain in the loop and in control.

## Architecture Summary

The system is a **headless Room Engine** with a **CLI Shell** as the first client.

The Room Engine contains:
- **Protocol Manager** — round lifecycle, participant registry, checkpoint triggers
- **Memory Engine** — transcript, summary, structured state
- **Provider Layer** — API key management, model routing, LLM calls, error handling

The CLI Shell owns no business logic. It delegates to the Room Engine API.

## Key Invariants

- **Single-human MVP:** MVP rooms contain exactly one human participant
- **Blind rounds:** all within-round responses are blind until reveal
- **Simultaneous reveal:** responses are revealed only when the round closes
- **Sequential rounds:** no overlapping rounds
- **Human-seeded cadence in MVP:** every round is seeded by the human participant, and the seed consumes the human's turn for that round
- **No autonomous round opening by agents:** agents never open a new round on their own
- **No double response:** no participant speaks twice in one round
- **Round completion:** a round closes only when every non-seed participant has responded, passed, or been explicitly marked unavailable
- **Round-settle invariant:** in MVP mode, no new round opens until the prior round’s post-close checkpoint completes or fails explicitly
- **Checkpoint before swap:** agent swap only happens at a checkpoint boundary
- **Provider failures never silently auto-pass:** failures must be surfaced for human decision
- **Checkpoint failure pause:** the room does not continue on stale summary/state after a failed checkpoint; a human must explicitly resolve it
- **Archive vs. end:** archived rooms are resumable by manual restart; ended rooms are terminal
- **Human disconnect semantics:** intentional quit ends the room; unexpected disconnect archives it for possible manual restart

## Persistence Rules

V1 uses local filesystem only. No database.

- transcript: append-only JSONL
- summary: versioned checkpoint snapshots plus a current summary projection
- structured state: versioned JSON revisions
- room config: single JSON file
- checkpoint log: append-only JSONL
- metrics: append-only JSONL
- local per-room persistence supports durability and manual restart; global cross-session history is out of MVP

## Data and Interface Conventions

- Canonical structured state format is JSON
- Human-facing renders are projections of canonical JSON
- Provider Layer returns normalized `CompletionResult`
- Raw provider responses do not leak beyond adapters
- All summarization and state-generation LLM calls go through the Provider Layer
- Human edits target canonical `field_path` addresses, not positional list indexes
- Human edits to structured state must be tracked as active overrides and preserved until explicitly changed or cleared

## Phase 0 Stack Decision

The first implementation stack is **Python 3.12 + UV** for both the Room Engine and the CLI Shell.

This choice optimizes for **MVP learning speed**: fast iteration on provider adapters, checkpointing, structured-state generation, and the CLI-first workflow.

The architecture must remain language-agnostic despite this implementation choice:
- keep all architecture and interface decisions language-agnostic
- do not bake Python-specific assumptions into core design
- do not introduce framework-specific patterns into the engine design

If the MVP demonstrates that Deliberation Room is a genuinely useful product and not merely an interesting experiment, the likely next step is an early post-MVP reimplementation of the Room Engine in **Go**, before broad product-feature expansion.

This is an expected post-MVP direction, not an automatic commitment. The final rewrite decision happens after MVP evaluation.

## Implementation Language and Toolchain

For the MVP implementation:
- use **UV** for environment and dependency management
- prefer `uv run ...` for execution and `uv add ...` for dependencies
- do not introduce `pip install ...` workflows unless explicitly required

If a Go reimplementation is explicitly approved later:
- prefer a simple module layout with standard tooling
- keep the engine framework-free and CLI-friendly

## Implementation Rules for Coding Agents

- Keep the Room Engine framework-free.
- Do not introduce a database in V1.
- Do not assume containerization in V1, but keep paths, config, and runtime behavior portable so Docker or Podman can be added later.
- Test modules independently using mocks for adjacent modules.
- Prefer simple, readable implementations over premature optimization.
- Do not optimize prompts yet; functional prompts are enough for MVP.
- Do not expand scope beyond the MVP spec unless explicitly directed.

## Build Priority

Follow `docs/PLAN.md` in order:
1. stack decision
2. domain objects + persistence
3. provider layer
4. memory engine
5. protocol manager
6. CLI shell
7. instrumentation

## When Unsure

Prefer the choice that:
- preserves protocol simplicity
- keeps humans in control
- protects the structured state as the main artifact
- avoids hidden behavior
- keeps the engine reusable across future clients
