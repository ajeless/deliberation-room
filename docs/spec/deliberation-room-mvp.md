# Deliberation Room — MVP Spec (v4)

## One-Line Summary

A shared room where humans and LLMs participate as peers in structured, round-based discussion to solve problems together.

---

## Design Principles

1. **Structured over real-time.** Deliberation, not conversation simulation.
2. **Role-first, model-second.** Users think in roles; the system resolves models.
3. **Shared cognition over raw transcript.** The structured state is the product, not the chat log.
4. **Human-controlled source of truth.** The system maintains state; humans correct it.
5. **Protocol simplicity in V1.** One cadence, one room, one workflow. Complexity earns its way in later.

---

## Core Abstractions

### 1. Room Protocol

The room runs on a **round-based cadence**, not real-time chat.

- MVP supports **exactly one human participant** and one or more agent participants.
- A round consists of a **seed message** followed by **responses** from all other participants.
- In MVP, **every round must be seeded by the human participant**. The seed consumes the human's turn for that round; the human does not also respond or pass in the same round.
- Agents do not autonomously open rounds.
- Agent participants respond or **pass**.
- The round closes when every non-seed participant has responded, passed, or been explicitly marked unavailable after a surfaced provider failure.
- No participant speaks twice in a single round.
- Rounds are sequential — no overlapping rounds in V1.
- **Within-round visibility is blind by default** — agents respond only to the seed message and prior room state, not to other responses arriving in the same round. All responses are revealed when the round closes. Sequential visible responses may be explored later as an experimental mode.
- This human-seeded cadence is an MVP protocol rule, not a permanent data-model restriction.

**Checkpointing** occurs:
- Every round by default during MVP evaluation (maximizes observability and catches state drift early; this default may be relaxed based on testing)
- N is configurable for later tuning
- When any participant signals a compaction request
- On explicit topic shift (human-initiated)
- Before any agent swap

### 2. Agent Abstraction

An **agent** is a configuration, not a model.

| Field | Description |
|---|---|
| Role | Human-readable label (e.g., "Code Reviewer," "Skeptic," "Generalist") |
| System prompt | Instructions defining behavior and perspective |
| Backing model | The LLM powering the agent (selected automatically or overridden) |
| Provider | Which API key / provider serves this model |

- Agents are **hot-swappable** at checkpoints. The new agent reads the shared room state to catch up.
- Roles are a UX concept, not an enforcement mechanism — the system prompt does the actual work.

### 3. Provider Layer

Manages API keys, model discovery, and routing.

**Key discovery priority:**
1. Direct provider keys from environment variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, etc.)
2. Meta-provider keys (`OPENROUTER_API_KEY`, etc.)
3. Manual key entry

**Model selection:**
- Default: system picks a sensible model per role based on available keys.
- Advanced: user can override model choice per agent.
- The full model catalog is never the default view.

---

## Shared Memory Model

Three layers, serving both agents and humans:

### Layer 1 — Raw Transcript
- Complete record of every message in every round.
- Stored externally (not in any agent's context window).
- Open-round state is kept separately from transcript history until a round closes or is abandoned; abandoned rounds still preserve the seed and any captured responses in history.
- Searchable / retrievable via RAG if a specific earlier point becomes relevant.

### Layer 2 — Working Summary
- A compressed narrative of the conversation so far.
- Maintained in-context for all agents.
- Regenerated at each checkpoint.
- Stored as versioned checkpoint snapshots, with a current summary projection for normal reads.

### Layer 3 — Structured State Object
- Canonical internal representation is a **structured JSON schema**. Rendering/export layers (markdown, formatted UI, potentially binary for performance) sit on top. The schema is the source of truth; display formats are projections of it.
- The highest-value layer. Contains:
  - **Current problem** — what the room is trying to solve
  - **Candidate solutions** — proposals on the table
  - **Open questions** — unresolved points
  - **Decisions made** — things the room has agreed on
  - **Unresolved disagreements** — where participants still differ
  - **Action items** — if applicable
- Updated by the system at each checkpoint. **Revisions are versioned** — each checkpoint produces a new state revision, enabling diff and rollback.
- **Human-editable** — humans can correct or override any field. Every human edit also creates a new state revision. Edits are tracked and marked as human-authored.
- Human edits target canonical `field_path` addresses. Repeated collections are addressed by stable item IDs rather than numeric list indexes.
- Human edits are treated as **active overrides**. Later checkpoints must preserve those overrides until a human changes or clears them.
- Clearing an override is an explicit human action and also creates a new state revision.
- Visible to all participants (including humans) as a persistent side panel.

---

## Onboarding Flow (V1)

1. **Key detection:** Scan environment variables for known provider keys. Present results: "Found access to: Claude, GPT-4o, Gemini."
2. **Manual entry:** Option to add keys not found automatically, including meta-provider keys.
3. **Room creation:** User names the room, describes the problem in a sentence or two.
4. **Agent setup:** System suggests 2 agents with default roles (e.g., Generalist + Skeptic). User can accept defaults, change roles, or add/remove agents.
5. **Start:** Three clicks from launch to a working session.

---

## MVP Scope

### In
- Exactly 1 human, 1+ LLM agents in a shared room
- Human-seeded rounds with agent pass
- Blind within-round responses (revealed on round close)
- Role-first agent setup with default model selection
- API key auto-discovery (direct + meta-provider)
- Manual key entry
- Three-layer shared memory (transcript, summary, structured state)
- Protocol-driven checkpointing with versioned structured state
- Agent-initiated compaction requests
- Structured state visible as a side panel, human-editable
- Hot-swap agents at checkpoints
- Summarization built into room protocol (not a separate agent)
- Provider failure handling: bounded retry, then flag + human decision
- Local per-room filesystem persistence for durability and manual restart of archived rooms

### Explicitly Out (V1)
- Fast-pass / selective-response mode
- Visible within-round responses (sequential mode)
- Browser-session reuse of consumer chat subscriptions
- Provider signup wizard
- Multiple human participants in one room
- Multiple simultaneous rooms
- Global cross-room or cross-session history beyond a room's own local files
- Voice or multimedia input
- Fine-grained permissions / roles for human participants
- Dedicated summarizer/facilitator agent

### Anti-Goals
- This is not a real-time chat product. Do not optimize for speed of exchange.
- This is not a model comparison tool. Side-by-side output comparison is incidental, not the point.
- This is not an autonomous agent swarm. Humans remain in the loop and in control.

---

## First Test Scenario

**Setup:** 1 human, 2 agents (Generalist + Skeptic), problem framed as a coding architecture decision.

**Success criteria:**
- The human finds the structured output (decisions, open questions, disagreements) more useful than a comparable single-agent chat session.
- Checkpointing and compaction work without losing critical context.
- Onboarding from zero to working room takes under 2 minutes.
- The round-based flow feels organized, not sluggish.
- At least one moment per session where the multi-agent setup surfaces something a single agent would have missed.

---

## Resolved Decisions

1. **Checkpoint frequency:** Default to every round during MVP evaluation for maximum observability. Configurable N for later. Track token cost per checkpoint to inform when to relax.
2. **Structured state editability:** System-maintained, human-correctable. Every checkpoint and every human edit creates a new structured-state revision. Human edits are tracked as active overrides and preserved until a human changes or clears them.
3. **Summarizer role:** Built into the room protocol for V1, not a visible participant. A dedicated facilitator/summarizer agent is a future option.
4. **Provider failures mid-round:** Bounded retry (1–2 attempts with short backoff). If still failing, surface a human decision. The human may continue and mark the agent unavailable for the round, wait for one additional retry cycle exactly once for that failure episode, queue a swap at the next checkpoint, archive the room, or end it. No silent auto-pass.
5. **Checkpoint failures:** A round is considered settled once its post-round checkpoint succeeds or fails explicitly. If the checkpoint fails, the room moves to `awaiting_human_decision`. In MVP, no new round may begin on stale summary/state; the human must retry the checkpoint, archive the room, or end it.
6. **Interface priority:** Headless core with CLI shell first. The room engine is built as a reusable library. Web, IDE, and native clients attach to the same core later.
7. **Structured state format:** Canonical JSON schema internally, with flexible rendering/export layers on top.
8. **Within-round visibility:** Blind by default. All responses revealed on round close.
9. **Round ownership:** In MVP, every round is human-seeded. The seed consumes the human's turn for that round. Agents do not autonomously open rounds. This is an MVP protocol rule, not a permanent data-model restriction.
10. **MVP human participation:** MVP supports exactly one human participant per room.
11. **Room exit semantics:** Intentional human quit ends the room. Unexpected human disconnect archives the room for possible later manual restart. Archived rooms may be resumed manually from local room files; ended rooms may not be resumed.
12. **Naming:** "Deliberation Room" retained as working code name. Product naming deferred.

---

## Evaluation Plan

### Metrics to Track
- **Token cost per round/checkpoint** — needed to calibrate checkpoint frequency; measured as total tokens consumed by summarization + structured state generation per checkpoint
- **Time to first useful structured state** — rounds elapsed before the structured state contains at least one decision or candidate solution
- **Human correction rate** — edits per checkpoint; high rates may indicate poor summarization
- **Agent pass rate** — percentage of rounds where agents pass; high rates suggest too many participants or too narrow a problem
- **Duplication across agents** — are agents saying the same thing? Indicates role prompts need sharpening
- **State drift** — does structured state diverge from transcript content? Measured by periodic human audit

### Success Indicators
- Human rates structured state as more useful than single-agent chat
- Checkpointing preserves critical context (no "the room forgot X" moments)
- Onboarding under 2 minutes
- Round-based flow feels organized, not sluggish
- Multi-agent setup surfaces at least one insight per session that a single agent would have missed

### Failure Patterns to Watch For
- Agents converge too quickly (groupthink despite blind rounds)
- Structured state becomes stale and humans stop trusting it
- Round cadence feels like bureaucracy
- Token costs make per-round checkpointing impractical
- Users ignore the structured state panel and just read transcript
