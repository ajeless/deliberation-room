# Deliberation Room — Architecture Overview (v2)

> **Scope:** This document describes module boundaries, domain objects, event flow, and interface contracts at a level sufficient for a coding agent to begin implementation. It is NOT code. It is the bridge between the MVP spec (v4) and implementation.

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
- Round state (seed, pending responses, passes, closed)
- Checkpoint trigger logic (every-N, compaction request, topic shift, pre-swap)
- Participant registry (who is in the room, human vs. agent)

**Key operations:**
- `start_round(seed_message, author)` — opens a new round
- `submit_response(participant_id, content | pass)` — records a response or pass
- `close_round()` — triggered automatically when all participants have responded/passed; returns the complete round
- `trigger_checkpoint(reason)` — initiates a checkpoint via the Memory Engine
- `add_participant(participant_config)` / `remove_participant(participant_id)`
- `get_room_status()` — returns current round state, participant statuses, checkpoint history

**Rules enforced:**
- No participant responds twice in a round
- Rounds are sequential (no new round until current closes)
- Checkpoint must complete before agent swap

**MVP round-settle invariant:** In MVP mode (checkpoint every round), a round is not fully settled until its post-round checkpoint completes or fails explicitly. No new round may open until the previous round's checkpoint has resolved. This ensures structured state is always current before the next seed message.

### 2. Memory Engine

**Responsibility:** Manages the three-layer memory model. Produces and versions summaries and structured state.

**Owns:**
- Raw transcript (append-only log)
- Working summary (regenerated at each checkpoint)
- Structured state object (versioned JSON, diffable)
- Checkpoint history

**Key operations:**
- `append_transcript(round)` — stores a completed round
- `run_checkpoint(transcript_since_last, current_state)` — generates new working summary + updated structured state; returns new version
- `get_context_payload()` — returns the package an agent needs: working summary + current structured state (NOT the full transcript)
- `get_transcript(query?)` — RAG-style retrieval over raw transcript for specific lookups
- `apply_human_edit(field, new_value, author)` — records a human override on structured state; creates new version
- `get_state_history()` — returns version log of structured state for diff/rollback

**Structured state schema (canonical fields):**
```json
{
  "version": 1,
  "checkpoint_id": "chk_0001",
  "updated_at": "timestamp",
  "updated_by": "system",
  "current_problem": "string",
  "candidate_solutions": [
    {
      "id": "sol_1",
      "description": "string",
      "status": "active",
      "origin": "system"
    }
  ],
  "open_questions": [
    {
      "id": "q_1",
      "text": "string",
      "raised_by": "participant_id",
      "round_raised": 1
    }
  ],
  "decisions": [
    {
      "id": "dec_1",
      "text": "string",
      "round_decided": 1,
      "origin": "system"
    }
  ],
  "disagreements": [
    {
      "id": "dis_1",
      "description": "string",
      "positions": [
        {
          "participant": "participant_id",
          "stance": "string"
        }
      ]
    }
  ],
  "action_items": [
    {
      "id": "act_1",
      "text": "string",
      "assignee": null
    }
  ],
  "edit_log": [
    {
      "field": "string",
      "old_value": null,
      "new_value": null,
      "author": "participant_id",
      "source": "system_checkpoint",
      "timestamp": "timestamp"
    }
  ]
}
```

**Summarization:** In V1, summarization is performed by making an LLM call (via the Provider Layer) with a dedicated summarization prompt. This is a room protocol function, not a participant. The prompt and model used for summarization are configurable but default to a fast, cheap model.

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

**Adapter interface (each provider implements):**
- `list_models(api_key)` — what models does this key unlock?
- `send(model, messages, config)` — make the API call
- `parse_response(raw)` — normalize to `CompletionResult`

**Normalized completion result (`CompletionResult`):**
```json
{
  "content": "string",
  "token_usage": { "input": 0, "output": 0 },
  "latency_ms": 0,
  "status": "success",
  "error_code": null,
  "provider_metadata": null
}
```

All modules consuming LLM output work with this normalized shape. Raw provider responses are never leaked beyond the adapter.

**Error handling:**
- On failure: retry 1–2x with short backoff
- On persistent failure: return a `CompletionResult` with `status: "error"` so the Protocol Manager can flag the agent as unavailable and let the human decide

### 4. CLI Shell

**Responsibility:** Human interface. Renders room state, accepts input, issues commands. As thin as possible.

**Key operations:**
- Display round state (who's responded, who's pending)
- Accept seed messages and responses from the human
- Display the structured state panel
- Accept human edits to structured state
- Accept commands: `/pass`, `/checkpoint`, `/swap <agent>`, `/status`, `/history`, `/edit <field>`
- Display agent error/unavailability notifications

**Does NOT own:** Any logic. All commands delegate to the Room Engine API.

---

## Domain Objects

| Object | Description |
|---|---|
| **Room** | Top-level container. Has participants, rounds, memory layers, config. |
| **Participant** | Either a Human or an Agent. Has an ID, display name, type. |
| **Agent** | Extends Participant. Has role, system prompt, model_id, provider. |
| **Round** | A seed message + list of responses/passes. Has a round number, status (open/closed/settled). |
| **Message** | Content + author + timestamp + round number. |
| **Checkpoint** | A snapshot: working summary + structured state version + round number + reason + checkpoint_id. |
| **StructuredState** | The versioned JSON object described above. |
| **CompletionResult** | Normalized LLM response: content, token usage, latency, status, error info. |

---

## Event Flow: A Single Round

**Blind-round invariant:** All participants — both agents and humans — respond blind within a round. No participant sees any other participant's response until the round closes and all responses are revealed simultaneously.

```text
1. Human (or system) calls start_round(seed_message)
   → Protocol Manager creates Round N, status=open
   → Protocol Manager notifies all other participants

2. For each Agent participant (order does not matter, blind responses):
   a. Memory Engine provides context_payload (summary + structured state)
   b. Agent's system prompt + context_payload + seed_message → sent to Provider Layer
   c. Provider Layer calls the agent's backing model, returns CompletionResult
   d. Response recorded → Protocol Manager records it via submit_response()
   (Agent responses are NOT visible to the human or other agents yet)

3. Human responds (or passes) via CLI — also blind to agent responses
   → submit_response()

4. All participants have responded/passed
   → Protocol Manager calls close_round()
   → All responses revealed simultaneously to the human
   → Completed round sent to Memory Engine via append_transcript()

5. Checkpoint trigger evaluated:
   → If triggered: Memory Engine runs run_checkpoint()
     → Summarization LLM call via Provider Layer
     → New working summary generated
     → Structured state updated + versioned
     → Round status transitions from "closed" to "settled"
   → If not triggered: round status transitions directly to "settled"

6. Next round may now begin (not before settled)
```

---

## Event Flow: Agent Swap

```text
1. Human issues /swap command (only allowed when last round is settled)
2. Protocol Manager triggers checkpoint if one hasn't just occurred
3. Protocol Manager removes old agent, adds new agent config
4. New agent receives context_payload from Memory Engine
   (summary + structured state — NOT full transcript)
5. Room continues with next round
```

---

## Event Flow: Provider Failure

```text
1. Provider Layer attempts completion for Agent X
2. Failure → retry 1–2x with backoff
3. Still failing → Provider Layer returns CompletionResult with status: "error"
4. Protocol Manager marks Agent X as "unavailable this round"
5. CLI displays: "[Agent X] unavailable this round — continue, wait, or swap?"
6. Human chooses:
   a. Continue → round closes without Agent X's response
   b. Wait → system retries after a delay
   c. Swap → triggers agent swap flow at next checkpoint
```

---

## Persistence Model (V1)

All persistence is **local filesystem**, kept simple for MVP:

| Data | Storage |
|---|---|
| Raw transcript | Append-only JSONL file |
| Working summary | Single file, overwritten at each checkpoint |
| Structured state | Versioned JSON files (one per checkpoint, named by checkpoint_id) |
| Room config | Single JSON file (participants, settings) |
| Checkpoint log | Append-only JSONL file (timestamp, reason, version pointers) |

No database in V1. Filesystem is sufficient and keeps dependencies minimal.

---

## Instrumentation (V1)

Track from day one, even in MVP:

| Metric | How |
|---|---|
| Token cost per checkpoint | Sum input+output tokens from summarization + state generation CompletionResults |
| Token cost per agent response | From CompletionResult.token_usage per call |
| Round duration (wall clock) | Timestamp delta from round open to settled |
| Checkpoint duration | Timestamp delta for summarization pipeline |
| Provider errors | Count per provider per session, from CompletionResult.status |
| Human edits to structured state | Count per session from edit_log |
| Latency per LLM call | From CompletionResult.latency_ms |

Logged to a local metrics JSONL file. No external telemetry in V1.

---

## What This Document Does NOT Cover (Yet)

- Specific system prompts for roles (Generalist, Skeptic, Code Reviewer, etc.)
- Summarization prompt design
- RAG implementation for transcript search
- Web, IDE, or desktop client design
- Multi-room support
- Authentication or multi-user access control
- Packaging or distribution
