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

- A round consists of a **seed message** (from any participant) followed by **responses** from all other participants.
- Participants respond or **pass**.
- The round closes when everyone has responded or passed.
- No participant speaks twice in a single round.
- Rounds are sequential — no overlapping rounds in V1.
- **Within-round visibility is blind by default** — agents respond only to the seed message and prior room state, not to other responses arriving in the same round. All responses are revealed when the round closes. Sequential visible responses may be explored later as an experimental mode.

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
- Searchable / retrievable via RAG if a specific earlier point becomes relevant.

### Layer 2 — Working Summary
- A compressed narrative of the conversation so far.
- Maintained in-context for all agents.
- Regenerated at each checkpoint.

### Layer 3 — Structured State Object
- Canonical internal representation is a **structured JSON schema**. Rendering/export layers (markdown, formatted UI, potentially binary for performance) sit on top. The schema is the source of truth; display formats are projections of it.
- The highest-value layer. Contains:
  - **Current problem** — what the room is trying to solve
  - **Candidate solutions** — proposals on the table
  - **Open questions** — unresolved points
  - **Decisions made** — things the room has agreed on
  - **Unresolved disagreements** — where participants still differ
  - **Action items** — if applicable
- Updated by the system at each checkpoint. **Revisions are versioned** — each checkpoint produces a new version, enabling diff and rollback.
- **Human-editable** — humans can correct or override any field. Edits are tracked and marked as human-authored. Principle: system-maintained, human-correctable.
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
- 1+ humans, 1+ LLM agents in a shared room
- Round-based turns with pass
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

### Explicitly Out (V1)
- Fast-pass / selective-response mode
- Visible within-round responses (sequential mode)
- Browser-session reuse of consumer chat subscriptions
- Provider signup wizard
- Multiple simultaneous rooms
- Persistent cross-session room history
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
2. **Structured state editability:** System-maintained, human-correctable. Human edits are tracked as overrides.
3. **Summarizer role:** Built into the room protocol for V1, not a visible participant. A dedicated facilitator/summarizer agent is a future option.
4. **Provider failures mid-round:** Bounded retry (1–2 attempts with short backoff). If still failing, flag agent as unavailable for the round. Human decides: continue without, wait, or swap at next checkpoint. No silent auto-pass.
5. **Interface priority:** Headless core with CLI shell first. The room engine is built as a reusable library. Web, IDE, and native clients attach to the same core later.
6. **Structured state format:** Canonical JSON schema internally, with flexible rendering/export layers on top.
7. **Within-round visibility:** Blind by default. All responses revealed on round close.
8. **Naming:** "Deliberation Room" retained as working code name. Product naming deferred.

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
