# AGENTS.md — Deliberation Room

## Purpose

Deliberation Room is a tool where humans and LLMs participate as peers in structured, round-based deliberation to solve problems together. It is a shared reasoning room, not a chat app.

## Read These Reference Docs

- `deliberation-room-mvp-spec-v4.md` — product source of truth
- `deliberation-room-architecture-v2.md` — module boundaries, schemas, event flows
- `PLAN.md` — implementation sequence and milestones

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

- **Blind rounds:** all participants, human and agent, respond blind within a round
- **Simultaneous reveal:** responses are revealed only when the round closes
- **Sequential rounds:** no overlapping rounds
- **No double response:** no participant speaks twice in one round
- **Round-settle invariant:** in MVP mode, no new round opens until the prior round’s post-close checkpoint completes or fails explicitly
- **Checkpoint before swap:** agent swap only happens at a checkpoint boundary
- **Provider failures never silently auto-pass:** failures must be surfaced for human decision

## Persistence Rules

V1 uses local filesystem only. No database.

- transcript: append-only JSONL
- summary: single file overwritten per checkpoint
- structured state: versioned JSON files
- room config: single JSON file
- checkpoint log: append-only JSONL
- metrics: append-only JSONL

## Data and Interface Conventions

- Canonical structured state format is JSON
- Human-facing renders are projections of canonical JSON
- Provider Layer returns normalized `CompletionResult`
- Raw provider responses do not leak beyond adapters
- All summarization and state-generation LLM calls go through the Provider Layer
- Human edits to structured state must be tracked as overrides

## Implementation Language and Toolchain

The implementation language and toolchain are **not locked yet**.

Until that choice is made explicitly:
- keep all architecture and interface decisions language-agnostic
- do not bake Python-specific or Go-specific assumptions into core design
- do not introduce framework-specific patterns into the engine design

If Python is chosen for the first implementation:
- use **UV** for environment and dependency management
- prefer `uv run ...` for execution and `uv add ...` for dependencies
- do not introduce `pip install ...` workflows unless explicitly required

If Go is chosen for the first implementation:
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

Follow `PLAN.md` in order:
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
