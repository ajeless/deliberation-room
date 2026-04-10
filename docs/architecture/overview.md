# Deliberation Room — Architecture Overview

> **Scope:** This document describes system boundary, module boundaries, and module responsibilities at a level sufficient for implementation. Canonical data contracts live in `contracts.md`. Canonical runtime flows live in `flows.md`. This is NOT code.

---

## System Boundary

The system is a **headless engine** with a **CLI shell** as the first client. The engine exposes a clean internal API that any future client (web, IDE plugin, desktop app) can consume.

**Naming hierarchy (explicit):**
- **Room Engine** = the overall headless core; the top-level container for all server-side logic
- **Protocol Manager** = a module *within* the Room Engine; owns round lifecycle, participant state, checkpoint triggers
- **Memory Engine** = a module *within* the Room Engine; owns transcript, summary, structured state
- **Provider Layer** = a module *within* the Room Engine; owns API keys, model routing, LLM calls

The Room Engine is not a synonym for the Protocol Manager. It is the parent that coordinates all three modules.

**Implementation language note:**
The architecture is intentionally language-agnostic at this stage. Python, Go, or another implementation language may be chosen later, but the module boundaries, data contracts, event flows, and invariants defined here should not depend on that choice.

```text
┌─────────────────────────────────────────────┐
│                CLI Shell                     │
│  (thin client — input, display, commands)   │
└──────────────────┬──────────────────────────┘
                   │ Engine API
┌──────────────────▼──────────────────────────┐
│              Room Engine                     │
│  ┌────────────┐ ┌────────────┐ ┌──────────┐ │
│  │  Protocol  │ │   Memory   │ │ Provider │ │
│  │  Manager   │ │   Engine   │ │  Layer   │ │
│  └────────────┘ └────────────┘ └──────────┘ │
└─────────────────────────────────────────────┘
```

---

## Modules

### 1. Protocol Manager

**Responsibility:** Manages round lifecycle, turn order, checkpoint triggers, and room state transitions.

**Owns:**
- Room state (`draft`, `active`, `awaiting_human_decision`, `checkpointing`, `archived`, `ended`)
- Round state (`open`, `closed`, `settled`, `abandoned`)
- Participant-in-round state (`pending`, `responded`, `passed`, `unavailable`)
- Checkpoint trigger logic (every-N, compaction request, topic shift, pre-swap)
- Participant registry (who is in the room, human vs. agent)

**Key operations:**
- `start_round(seed_message, author)` — opens a new round
- `submit_response(participant_id, content | pass)` — records a response or pass
- `close_round()` — triggered automatically when all non-seed participants have reached a terminal state; returns the complete round
- `trigger_checkpoint(reason)` — initiates a checkpoint via the Memory Engine and records a `Checkpoint` result
- `resolve_provider_failure(participant_id, action)` — applies a human decision after an agent completion failure (`continue`, `wait_once`, `swap_next_checkpoint`, `archive`, `end`)
- `resolve_checkpoint_failure(action)` — applies a human decision after a failed post-round checkpoint (`retry_checkpoint`, `archive`, `end`)
- `archive_room(reason)` — moves the room into an inactive but resumable lifecycle state
- `resume_room()` — reactivates an archived room using the latest persisted summary/state, with no open round
- `end_room(reason)` — moves the room into a terminal lifecycle state
- `add_participant(participant_config)` / `remove_participant(participant_id)`
- `get_room_status()` — returns room state, current round state, participant statuses, checkpoint history

**Rules enforced:**
- MVP supports exactly one human participant per room
- In MVP, every round seed must be human-authored
- The seed consumes the human's turn for that round
- Agents do not autonomously open rounds
- No participant responds twice in a round
- Rounds are sequential (no new round until current closes)
- A round closes only when all non-seed participants have responded, passed, or been explicitly marked unavailable after a human decision
- Checkpoint must complete before agent swap
- Archived rooms may be resumed manually; ended rooms may not be resumed
- No new round may begin on stale summary/state after a failed checkpoint; the room remains paused for explicit human resolution

**MVP round-settle invariant:** In MVP mode, every round is checkpointed before the room can continue. A round is not fully settled until its post-round checkpoint completes or fails explicitly. No new round may open until the previous round's checkpoint has resolved. This ensures structured state is always current before the next seed message.

### 2. Memory Engine

**Responsibility:** Manages the three-layer memory model. Produces and versions summaries and structured state.

**Owns:**
- Raw transcript (append-only log)
- Working summary history (versioned checkpoint snapshots plus a current pointer)
- Structured state revision history (versioned JSON, diffable)
- Active human overrides on structured state
- Checkpoint history

**Key operations:**
- `append_transcript(round)` — stores a completed round
- `run_checkpoint(transcript_since_last, current_state)` — generates a checkpoint result with a new summary snapshot + updated structured state revision on success; preserves active human overrides
- `get_context_payload()` — returns the package an agent needs: working summary + current structured state (NOT the full transcript)
- `get_transcript(query?)` — RAG-style retrieval over raw transcript for specific lookups
- `apply_human_edit(field_path, new_value, author)` — records a human override on structured state; creates a new revision
- `clear_human_override(field_path, author)` — clears an active human override; creates a new revision
- `get_state_history()` — returns version log of structured state for diff/rollback

**Canonical contracts:** The structured state schema and related domain objects are defined in `contracts.md`.

**Summarization:** In V1, summarization is performed by making an LLM call (via the Provider Layer) with a dedicated summarization prompt. This is a room protocol function, not a participant. The prompt and model used for summarization are configurable but default to a fast, cheap model. Checkpoint output must preserve active human overrides when generating the next structured-state revision.

### 3. Provider Layer

**Responsibility:** Manages API keys, model routing, and LLM calls. Knows nothing about rooms or rounds — it just sends prompts and returns completions.

**Owns:**
- Key registry (discovered + manually entered keys)
- Provider adapters (one per provider API shape: Anthropic, OpenAI, Google, OpenRouter, etc.)
- Model catalog (available models per key)
- Retry/error handling policy

**Key operations:**
- `discover_keys()` — scans environment variables, returns found providers + models
- `register_key(provider, key)` — manual key entry
- `list_available_models()` — returns all models accessible across all registered keys
- `complete(model_id, messages, config?)` — sends a prompt, returns a `CompletionResult`; handles retries internally
- `get_provider_status(provider)` — health/rate-limit status

**Canonical contracts:** The provider adapter interface and normalized `CompletionResult` are defined in `contracts.md`.

**Error handling:**
- On failure: retry 1–2x with short backoff
- On persistent failure: return a `CompletionResult` with `status: "error"` and error details so the Protocol Manager can move the room into `awaiting_human_decision` and let the human decide

### 4. CLI Shell

**Responsibility:** Human interface. Renders room state, accepts input, issues commands. As thin as possible.

**Key operations:**
- Display round state (who's responded, who's pending)
- Accept seed messages from the human
- Display the structured state panel
- Accept human edits to structured state
- Accept commands: `/checkpoint`, `/swap <agent>`, `/status`, `/history`, `/edit <field_path>`, `/clear <field_path>`, `/metrics`
- Display agent error/unavailability notifications and collect human decision prompts for both agent failures and checkpoint failures

**Command semantics:**
- `/checkpoint` — only between rounds, when the room is settled
- `/swap <agent>` — only when the room is settled; swap executes at a checkpoint boundary
- `/status` — projects `get_room_status()`
- `/history` — projects transcript history plus checkpoint/state revision history
- `/edit <field_path>` — delegates to `apply_human_edit()`
- `/clear <field_path>` — delegates to `clear_human_override()`
- `/metrics` — projects the local metrics log

**Does NOT own:** Any logic. All commands delegate to the Room Engine API.
