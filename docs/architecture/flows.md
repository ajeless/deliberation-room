# Deliberation Room — Architecture Flows

> **Scope:** This document owns the canonical runtime flows, persistence model, and instrumentation for the MVP. Module responsibilities live in `overview.md`. Data contracts live in `contracts.md`.

---

## Event Flow: Room Creation + First Round

```text
1. Human launches the CLI shell
2. CLI Shell discovers provider keys and available models via the Provider Layer
3. Human creates a room
   → names the room
   → describes the problem statement
   → selects or confirms the initial agent participants
4. CLI Shell writes the initial room artifacts
   → `room_config.json` persisted with room metadata, participants, and settings
   → `room_state.json` persisted with `status: "draft"` and no open round
5. Human submits the first seed message
   → Protocol Manager calls `start_round(seed_message, author)`
   → Room status transitions from `draft` to `active`
   → Round 1 opens under the standard blind-round flow
6. The room continues with the normal round-close and checkpoint flow
```

---

## Event Flow: A Single Round

**Blind-round invariant:** In MVP, the human seeds each round and all agent responses happen blind within that round. No participant sees another participant's response until the round closes and all outcomes are revealed simultaneously.

```text
1. Human calls start_round(seed_message)
   → Protocol Manager creates Round N, status=open
   → Room status=active
   → Human seed author is recorded as the seed author for the round
   → The human's turn for this round is now consumed
   → `room_state.json` is updated with the currently open round
   → Protocol Manager notifies all agent participants

2. For each Agent participant (order does not matter, blind responses):
   a. Memory Engine provides context_payload (summary + structured state)
   b. Agent's system prompt + context_payload + seed_message → sent to Provider Layer
   c. Provider Layer calls the agent's backing model, returns CompletionResult
   d. Response recorded → Protocol Manager records it via submit_response()
   (Agent responses are NOT visible to the human or other agents yet)

3. All non-seed participants have reached a terminal state
   (`responded`, `passed`, or `unavailable`)
   → Protocol Manager calls close_round()
   → Round status=closed
   → All responses revealed simultaneously to the human
   → Closed round transcript record appended to transcript history via append_transcript()
   → `current_round` is cleared from `room_state.json`

4. MVP checkpoint trigger fires
   → Room status=checkpointing
   → Memory Engine runs run_checkpoint()
     → Summarization LLM call via Provider Layer
     → Structured-state-generation LLM call via Provider Layer
   a. If checkpoint status=success:
      → New summary snapshot generated
      → Structured state updated + versioned while preserving active human overrides
      → Checkpoint record written with status=success
      → Round status transitions from "closed" to "settled"
      → Room status returns to active
   b. If checkpoint status=error:
      → No new summary snapshot or structured-state revision is committed
      → Checkpoint record written with status=error
      → Round status transitions from "closed" to "settled"
      → Room status=awaiting_human_decision
      → No new round may begin on stale summary/state

5. Next round may now begin (not before settled)
```

---

## Event Flow: Agent Swap

```text
1. Human issues /swap command (only allowed when the room is active and the last round is settled)
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
4. Protocol Manager moves the room to awaiting_human_decision
5. CLI displays: "[Agent X] completion failed — continue, wait once, swap next checkpoint, archive, or end?"
6. Human chooses:
   a. Continue → Protocol Manager marks Agent X as unavailable for this round
                  → room status returns to active
   b. Wait once → system performs one additional retry cycle for the same completion attempt
                 → room remains awaiting_human_decision until that cycle resolves
                 → if retry succeeds, the response is recorded and room status returns to active
                 → if retry fails again, `wait once` is no longer available for that failure episode
   c. Swap next checkpoint → Protocol Manager marks Agent X as unavailable for this round
                            → queues swap at the next checkpoint boundary
                            → room status returns to active
   d. Archive → if a round is open, it transitions to abandoned
              → abandoned round transcript record is appended before room status changes
              → room status=archived
   e. End → if a round is open, it transitions to abandoned
          → abandoned round transcript record is appended before room status changes
          → room status=ended
7. If all non-seed participants are now in terminal states
   (`responded`, `passed`, or `unavailable`)
   → the round closes normally
   → the standard post-round checkpoint flow still runs
8. After that checkpoint resolves, if there are no remaining available agent participants and no swap is queued
   → room status=awaiting_human_decision before another round can start
   → human must archive or end
```

---

## Event Flow: Checkpoint Failure

```text
1. A post-round checkpoint runs and returns a Checkpoint record with status=error
2. Round status transitions from closed to settled
3. Room status becomes awaiting_human_decision
4. CLI displays: "Checkpoint failed — retry checkpoint, archive, or end?"
5. Human chooses:
   a. Retry checkpoint → room status=checkpointing
                       → Memory Engine reruns the checkpoint for the same unresolved transcript window
                       → if retry succeeds, room status returns to active
                       → if retry fails again, room status returns to awaiting_human_decision
   b. Archive → room status=archived
   c. End → room status=ended
6. No new round may begin until a checkpoint succeeds or the room is archived or ended
```

---

## Event Flow: Room End / Archive

```text
1. Human intentionally quits
   → if a round is open, it transitions to abandoned
   → abandoned round transcript record is appended before room status changes
   → room status=ended

2. Human disconnects unexpectedly
   → if a round is open, it transitions to abandoned
   → abandoned round transcript record is appended before room status changes
   → room status=archived
   → local room files remain available for possible later manual restart

3. All agents become unavailable and the human chooses not to continue waiting
   → room status=archived or ended, depending on the human's choice
```

---

## Event Flow: Manual Resume

```text
1. Human selects an archived room to resume
2. Protocol Manager loads the latest persisted summary/state/checkpoint pointers
3. Any previously open round remains abandoned in history
4. Room status transitions from archived to active
5. No round is reopened automatically; the next step is a new human seed
```

---

## Persistence Model (V1)

All persistence is **local filesystem**, kept simple for MVP:

| Data | Storage |
|---|---|
| Raw transcript | Append-only JSONL file of immutable round transcript records (`closed` and `abandoned`) |
| Working summary | Versioned checkpoint summary files, plus a current-summary convenience file |
| Structured state | Versioned JSON revision files (checkpoint, human-edit, and human-clear revisions) |
| Room config | Single JSON file (room metadata, participant definitions, settings) |
| Room runtime state | Single mutable JSON file (`room_state.json`) containing lifecycle state, any currently open round, latest checkpoint pointers, and pending human-decision metadata |
| Checkpoint log | Append-only JSONL file (timestamp, reason, status, version pointers, error info) |
| Metrics | Append-only JSONL file |

No database in V1. Filesystem is sufficient and keeps dependencies minimal.
Local per-room persistence is in scope for durability and manual restart of a room. Global cross-room or cross-session history is out of scope for MVP.
`room_state.json` is the canonical resume source for a live or archived room. Transcript history is immutable and captures rounds only after they leave `open`.

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
The Phase 1 persistence skeleton intentionally leaves the per-row metrics event schema open; Phase 6 defines the canonical metrics JSONL row shape used for reporting and evaluation.

---

## What This Document Does NOT Cover (Yet)

- Specific system prompts for roles (Generalist, Skeptic, Code Reviewer, etc.)
- Summarization prompt design
- RAG implementation for transcript search
- Web, IDE, or desktop client design
- Multi-room support
- Authentication or multi-user access control
- Packaging or distribution
