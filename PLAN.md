# PLAN.md — Deliberation Room Implementation Sequence

## Overview

This plan breaks the MVP into phases with clear milestones. Each phase produces a testable increment. Dependencies flow top to bottom — later phases depend on earlier ones.

---

## Phase 0: Stack Decision

**Goal:** Make an explicit implementation choice for the first build without letting it happen by accident.

**Decision to make:**
- Python + UV
- Go
- another option, if explicitly justified

**Evaluation criteria:**
- fastest path to a usable MVP
- strongest fit for a headless engine + CLI-first workflow
- ease of provider integration
- coding-agent effectiveness
- portability and runtime simplicity
- future performance and concurrency needs

**Deliverables:**
- short stack decision record
- rationale for why the chosen stack fits the first implementation
- explicit note on what is being optimized for: MVP learning speed, long-term engine simplicity, or another stated priority

**Done when:** The stack choice is made intentionally and reflected in `AGENTS.md`, without ambiguity.

**Can be stubbed:** Nothing. This decision should happen before implementation begins.

---

## Phase 1: Domain Objects + Persistence Skeleton

**Goal:** Establish the core data model and local filesystem persistence so all later modules have something to read and write.

**Deliverables:**
- Domain object definitions: Room, Participant, Agent, Round, Message, Checkpoint, StructuredState, CompletionResult
- Structured state JSON schema (canonical, as defined in `deliberation-room-architecture-v2.md`)
- Persistence layer: read and write functions for transcript JSONL, structured state versioned JSON, room config JSON, checkpoint log JSONL, metrics JSONL
- Unit tests for serialization round-trips and append/read operations

**Done when:** You can create a Room, write rounds to transcript, write and version structured state, and read it all back from disk.

**Can be stubbed:** Nothing — this is foundational.

---

## Phase 2: Provider Layer

**Goal:** Connect to at least two LLM providers and return normalized `CompletionResult` objects.

**Deliverables:**
- Key discovery: scan environment variables for known provider keys
- Manual key registration
- At least two provider adapters
- OpenRouter adapter
- `list_available_models()` aggregated across all registered keys
- `complete()` with retry logic (1–2 retries, short backoff)
- Normalized `CompletionResult` returned for all calls (success and error)
- Unit tests with mocked provider responses; one integration test per real adapter

**Done when:** You can discover keys, list available models, send a prompt, and get back a `CompletionResult` from at least two providers. Errors return `status: "error"` instead of throwing.

**Can be stubbed:** Model-to-role recommendation logic.

---

## Phase 3: Memory Engine

**Goal:** Implement the three-layer memory model with checkpoint-driven summarization.

**Deliverables:**
- `append_transcript(round)` — writes to JSONL
- `run_checkpoint()` — calls Provider Layer with summarization prompt, generates new working summary and updated structured state, writes versioned files
- `get_context_payload()` — returns summary + current structured state
- `apply_human_edit()` — writes override to structured state, logs in edit_log
- `get_state_history()` — lists versions, supports diff
- Summarization prompt (first draft — functional, not optimized)
- Structured state generation prompt (first draft)
- Tests: checkpoint produces valid structured state, versioning works, human edits are tracked

**Done when:** Given a sequence of completed rounds, `run_checkpoint()` produces a working summary and a valid structured state object. Human edits are recorded and versioned.

**Can be stubbed:** RAG/transcript search. Summary quality tuning.

---

## Phase 4: Protocol Manager

**Goal:** Implement round lifecycle, blind-round mechanics, and checkpoint triggering.

**Deliverables:**
- `start_round()` — creates round, notifies participants
- `submit_response()` — records response or pass, enforces no-double-response
- `close_round()` — fires when all participants responded or passed, triggers transcript append
- Blind-round enforcement: responses are collected but not revealed until close
- Checkpoint trigger logic: every-N (default every round for MVP), compaction request, topic shift, pre-swap
- Round-settle invariant: next round blocked until checkpoint resolves
- Participant registry: add/remove, human vs. agent tracking
- Agent orchestration: for each agent, assemble prompt, call Provider Layer, record response
- Tests: round lifecycle, blind enforcement, checkpoint triggers, settle invariant, error handling when agent unavailable

**Done when:** You can run a complete round: seed → agent responses (blind) → human response → close → checkpoint → settle. The system blocks the next round until settled.

**Can be stubbed:** Agent swap flow.

---

## Phase 5: CLI Shell

**Goal:** Human-usable interface for running a deliberation session.

**Deliverables:**
- Room creation flow: name the room, describe problem, select or confirm agents
- Key detection display
- Round display: show seed, show waiting status, reveal all responses on round close
- Structured state panel display
- Commands: `/pass`, `/checkpoint`, `/swap`, `/status`, `/history`, `/edit`, `/metrics`
- Provider failure notification + human decision prompt
- Agent swap flow integrated end-to-end
- Onboarding: zero to working room in under 2 minutes

**Done when:** A human can launch the tool, set up a room, run multiple rounds, see the structured state evolve, edit it, and swap an agent — all from the CLI.

---

## Phase 6: Instrumentation + Evaluation Baseline

**Goal:** Capture all metrics defined in the evaluation plan from day one of real usage.

**Deliverables:**
- Metrics logging to JSONL: token cost per checkpoint, token cost per response, round duration, checkpoint duration, provider errors, human edits, latency per call
- Simple CLI command to dump session metrics: `/metrics`
- First real test session: 1 human, 2 agents, coding architecture problem
- Evaluate against success criteria from the MVP spec

**Done when:** You can run a full session and review a metrics report that covers all tracked items.

---

## Dependency Graph

```text
Phase 0 (Stack Decision)
    │
    └──► Phase 1 (Domain + Persistence)
             │
             ├──► Phase 2 (Provider Layer)
             │        │
             │        ├──► Phase 3 (Memory Engine)
             │        │        │
             │        │        └──► Phase 4 (Protocol Manager)
             │        │                 │
             │        │                 └──► Phase 5 (CLI Shell)
             │        │                          │
             │        │                          └──► Phase 6 (Instrumentation)
```

---

## What Is NOT In This Plan

- Prompt optimization / tuning
- RAG over transcript
- Web or GUI client
- Multi-room support
- Distribution / packaging
- Role-to-model recommendation logic
- Containerization / Docker / Podman
- Deployment packaging
- Devcontainer or container-first local setup
