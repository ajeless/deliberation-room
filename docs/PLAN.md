# PLAN.md — Deliberation Room Implementation Sequence

## Overview

This plan breaks the MVP into phases with clear milestones. Each phase produces a testable increment. Dependencies flow top to bottom — later phases depend on earlier ones.

---

## Phase 0: Stack Decision

**Goal:** Make an explicit implementation choice for the first build without letting it happen by accident.

**Decision outcome:**
- MVP implementation: Python 3.12 + UV
- Optimization target: MVP learning speed
- Post-MVP direction: if the MVP proves this is a genuinely useful product and not just a cool experiment, Go is the leading candidate for an early Room Engine reimplementation before broad feature expansion
- Rewrite status: likely, but not automatic; final decision follows MVP evaluation

**Evaluation criteria:**
- fastest path to a usable MVP
- strongest fit for a headless engine + CLI-first workflow
- ease of provider integration
- coding-agent effectiveness
- portability and runtime simplicity
- future performance and concurrency needs

**Deliverables:**
- short stack decision record in `docs/STACK-DECISION.md`
- rationale for why the chosen stack fits the first implementation
- explicit note on what is being optimized for: MVP learning speed, long-term engine simplicity, or another stated priority
- explicit note on the expected post-MVP Go path and the condition that would trigger it

**Done when:** The stack choice is made intentionally and reflected in `AGENTS.md` and `docs/STACK-DECISION.md`, without ambiguity.

**Can be stubbed:** Nothing. This decision should happen before implementation begins.

---

## Phase 1: Domain Objects + Persistence Skeleton

**Goal:** Establish the core data model and local filesystem persistence so all later modules have something to read and write.

**Deliverables:**
- Domain object definitions: Room, Participant, Agent, Round, Message, Checkpoint, StructuredState, CompletionResult
- Canonical room, round, and participant-in-round state enums plus allowed transitions
- Structured state JSON schema (canonical, as defined in `docs/architecture/contracts.md`), including `field_path` override addressing and clear semantics
- Persistence layer: read and write functions for transcript JSONL, versioned summary snapshots, structured state revisions, room config JSON, checkpoint log JSONL with success/error outcome records, metrics JSONL
- Unit tests for serialization round-trips, append/read operations, and lifecycle transition validity

**Done when:** You can create a Room, transition its room/round states including archive and resume, write rounds to transcript, write checkpoint success/error records, write and version summary/state revisions, and read it all back from disk.

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
- `run_checkpoint()` — calls Provider Layer with summarization prompt, generates a checkpoint result, writes a new working-summary snapshot and updated structured-state revision on success, and records failures explicitly
- `get_context_payload()` — returns summary + current structured state
- `apply_human_edit(field_path, new_value, author)` — writes override to structured state, creates a new revision, logs in edit_log
- `clear_human_override(field_path, author)` — clears an active override, creates a new revision, logs in edit_log
- `get_state_history()` — lists versions, supports diff
- Summarization prompt (first draft — functional, not optimized)
- Structured state generation prompt (first draft)
- Tests: checkpoint produces valid structured state, checkpoint failures are recorded explicitly, versioning works, human edits are tracked, human clears are tracked, active overrides survive later checkpoints

**Done when:** Given a sequence of completed rounds, `run_checkpoint()` produces a working summary and a valid structured state object on success, records explicit checkpoint failures on error, and preserves active overrides. Human edits and clears are recorded, versioned, and preserved across later checkpoints unless explicitly changed or cleared.

**Can be stubbed:** RAG/transcript search. Summary quality tuning.

---

## Phase 4: Protocol Manager

**Goal:** Implement round lifecycle, blind-round mechanics, and checkpoint triggering.

**Deliverables:**
- `start_round()` — creates a human-seeded round, consumes the human turn for that round, notifies agents
- `submit_response()` — records response or pass, enforces no-double-response
- `close_round()` — fires when all non-seed participants responded, passed, or were explicitly marked unavailable, triggers transcript append
- `resume_room()` — reactivates an archived room using the latest persisted summary/state with no open round
- Blind-round enforcement: responses are collected but not revealed until close
- MVP cadence enforcement: every round is human-seeded; agents do not autonomously open rounds
- Checkpoint trigger logic: every-N (default every round for MVP), compaction request, topic shift, pre-swap
- Round-settle invariant: next round blocked until checkpoint resolves
- Room and round lifecycle state transitions
- Provider failure resolution: `continue`, `wait_once`, `swap_next_checkpoint`, `archive`, `end`
- Checkpoint failure resolution: `retry_checkpoint`, `archive`, `end`
- Participant registry: add/remove, human vs. agent tracking
- Agent orchestration: for each agent, assemble prompt, call Provider Layer, record response
- Tests: round lifecycle, human-seeded cadence enforcement, blind enforcement, checkpoint triggers, settle invariant, provider failure resolution, checkpoint failure resolution, archived-room resume behavior, room archival/end transitions

**Done when:** You can run a complete round: human seed → agent responses (blind) → close → checkpoint → settle. The system blocks the next round until settled.

**Can be stubbed:** Agent swap flow.

---

## Phase 5: CLI Shell

**Goal:** Human-usable interface for running a deliberation session.

**Deliverables:**
- Room creation flow: name the room, describe problem, select or confirm agents
- Manual restart flow for archived rooms
- Key detection display
- Round display: show seed, show waiting status, reveal all responses on round close
- Structured state panel display
- Commands: `/checkpoint`, `/swap`, `/status`, `/history`, `/edit`, `/clear`, `/metrics`
- Provider failure notification + human decision prompt (`continue`, `wait_once`, `swap_next_checkpoint`, `archive`, `end` as applicable)
- Checkpoint failure notification + human decision prompt (`retry_checkpoint`, `archive`, `end`)
- Agent swap flow integrated end-to-end
- Onboarding: zero to working room in under 2 minutes

**Done when:** A human can launch the tool, set up a room, run multiple human-seeded rounds, see the structured state evolve, edit and clear overrides, respond to provider and checkpoint failures, archive or resume a room, and swap an agent — all from the CLI.

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

## Deferred MVP Clarifications

These are known documentation and design clarifications to resolve during later MVP phases. They are not blockers for Phase 1, but they should be addressed before the relevant phase is considered complete.

- **Phase 4:** Define the compaction request mechanism. The protocol already treats a compaction request as a checkpoint trigger; Phase 4 should specify how that signal is represented and surfaced to the Protocol Manager.
- **Phase 4:** Define open-round context consistency. Decide whether `get_context_payload()` is snapshotted at round open or whether human edits made during an open round are deferred until the round settles.
- **Phase 5:** Add an explicit room creation / `draft -> active` flow to `docs/architecture/flows.md`, covering room creation, participant setup, and the first round start.

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
