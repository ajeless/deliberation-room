# Deliberation Room — Architecture Contracts

> **Scope:** This document owns the canonical data contracts and interface shapes used by the Room Engine, Provider Layer, Memory Engine, and future clients. Runtime behavior and sequencing live in `flows.md`.

---

## Domain Objects

| Object | Description |
|---|---|
| **Room** | Top-level container. Has participants, rounds, memory layers, config, and a room lifecycle status. MVP supports exactly one human participant per room. |
| **Participant** | Either the single Human participant or an Agent. Has an ID, display name, and type. |
| **Agent** | Extends Participant. Has role, system prompt, model_id, provider. |
| **Round** | A seed message + per-participant outcomes for all non-seed participants. Has a round number, seed author, and status (`open`/`closed`/`settled`/`abandoned`). |
| **Message** | Content + author + timestamp + round number. |
| **Checkpoint** | A checkpoint attempt/result. Records round number, reason, `status` (`success` or `error`), and version pointers to any produced summary snapshot and structured-state revision. |
| **StructuredState** | The versioned JSON object described below. Each checkpoint, human edit, and human clear creates a new revision. |
| **CompletionResult** | Normalized LLM response: content, token usage, latency, status, error info. |

---

## Canonical State Enums

**Room status:**
- `draft` — room exists but no active deliberation has started
- `active` — room may accept a new human seed or wait for agent responses inside an open round
- `awaiting_human_decision` — round progression is paused pending a human decision after a provider failure or failed checkpoint
- `checkpointing` — room is running a checkpoint and may not start a new round
- `archived` — room is inactive but persisted for possible later manual restart
- `ended` — room has been intentionally ended and is terminal

**Allowed room status transitions:**
- `draft -> active | archived | ended`
- `active -> awaiting_human_decision | checkpointing | archived | ended`
- `awaiting_human_decision -> active | checkpointing | archived | ended`
- `checkpointing -> active | awaiting_human_decision | archived | ended`
- `archived -> active`
- `ended` has no outgoing transitions

**Round status:**
- `open` — round exists and is collecting agent outcomes
- `closed` — round finished collecting outcomes and has been revealed
- `settled` — post-round checkpoint has completed or failed explicitly
- `abandoned` — round cannot complete and the room is being archived or ended

**Allowed round status transitions:**
- `open -> closed | abandoned`
- `closed -> settled`
- `settled` has no outgoing transitions
- `abandoned` has no outgoing transitions

**Participant outcome within a round (non-seed participants only):**
- `pending` — no terminal outcome yet
- `responded` — participant submitted content
- `passed` — participant explicitly passed
- `unavailable` — participant failed to complete and was explicitly marked unavailable by human decision

**Participant outcome transitions:**
- `pending -> responded | passed | unavailable`
- `responded`, `passed`, and `unavailable` are terminal for that round

---

## Checkpoint Contract

**Checkpoint record (canonical fields):**

```json
{
  "checkpoint_id": "chk_0001",
  "round_number": 1,
  "reason": "round_close",
  "created_at": "timestamp",
  "status": "success",
  "summary_snapshot_id": "sum_0001",
  "structured_state_revision_id": "state_0001",
  "error_code": null,
  "error_message": null
}
```

**Checkpoint semantics:**
- `status` is `success` or `error`
- `summary_snapshot_id` and `structured_state_revision_id` are nullable when the checkpoint fails before producing new artifacts
- Each checkpoint attempt is logged, including failures

---

## Summary Snapshot Contract

**Summary snapshot (canonical fields):**

```json
{
  "summary_id": "sum_0001",
  "checkpoint_id": "chk_0001",
  "round_number": 1,
  "created_at": "timestamp",
  "content": "string"
}
```

**Summary snapshot semantics:**
- Summary snapshots are persisted as versioned JSON artifacts, not plain text files
- `summary_id` is the canonical identifier referenced by `Checkpoint.summary_snapshot_id`
- `checkpoint_id` links the snapshot to the checkpoint attempt that produced it
- `round_number` records the latest round incorporated into the summary snapshot
- `content` is the human-readable working summary used in `get_context_payload()`
- The current-summary convenience file, if materialized, is a projection of the latest summary snapshot rather than a separate schema

---

## Structured State Schema

**Structured state schema (canonical fields):**

```json
{
  "schema_version": 1,
  "revision_id": "state_0001",
  "previous_revision_id": null,
  "checkpoint_id": "chk_0001",
  "updated_at": "timestamp",
  "updated_by": "system",
  "revision_source": "checkpoint",
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
  "active_overrides": [
    {
      "field_path": "/current_problem",
      "value": null,
      "author": "participant_id",
      "created_at": "timestamp"
    }
  ],
  "edit_log": [
    {
      "field_path": "/current_problem",
      "old_value": null,
      "new_value": null,
      "author": "participant_id",
      "source": "checkpoint",
      "timestamp": "timestamp"
    }
  ]
}
```

**`field_path` semantics:**
- `field_path` is the canonical address for human edits and overrides
- Paths use slash-delimited JSON-style segments such as `/current_problem` or `/candidate_solutions/sol_1/description`
- Repeated collections must be addressed by stable item IDs, not numeric list indexes
- If a repeated nested object has no stable `id`, the containing object is the smallest editable unit in MVP

**Revision semantics:**
- `schema_version` tracks the schema shape; `revision_id` tracks per-room state history
- Every checkpoint, human edit, and human clear creates a new `StructuredState` revision
- `checkpoint_id` is nullable for revisions created by human edits or human clears between checkpoints
- `updated_by` is `system` for checkpoint-generated revisions and a participant ID for human-edit or human-clear revisions
- `revision_source` is `checkpoint`, `human_edit`, or `human_clear`
- `active_overrides` contains only currently active overrides; clearing an override removes it from `active_overrides` in the new revision and records the clear in `edit_log`
- `active_overrides` are authoritative and must be preserved by later checkpoints until a human changes or clears them
- Rollback, if exposed, creates a new human-authored revision derived from an earlier revision; historical revision files remain immutable

---

## Provider Adapter Interface

**Adapter interface (each provider implements):**
- `list_models(api_key)` — what models does this key unlock?
- `send(model, messages, config)` — make the API call
- `parse_response(raw)` — normalize to `CompletionResult`

---

## CompletionResult

**Normalized completion result (`CompletionResult`):**

```json
{
  "content": "string",
  "token_usage": { "input": 0, "output": 0 },
  "latency_ms": 0,
  "status": "success",
  "error_code": null,
  "error_message": null,
  "provider_metadata": null
}
```

All modules consuming LLM output work with this normalized shape. `status` is `success` or `error`. Raw provider responses are never leaked beyond the adapter.
