"""Protocol manager for round lifecycle, checkpointing, and agent orchestration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .domain import (
    Agent,
    JSONDict,
    Message,
    Participant,
    ParticipantOutcome,
    ParticipantType,
    PendingHumanDecision,
    Room,
    RoomConfig,
    RoomRuntimeState,
    RoomStatus,
    Round,
    RoundStatus,
    utc_now,
)
from .memory import CheckpointRunResult, MemoryEngine
from .persistence import RoomStorage
from .provider import ProviderLayer


CHECKPOINT_REASON_ROUND_CLOSE = "round_close"
CHECKPOINT_REASON_COMPACTION_REQUEST = "compaction_request"
CHECKPOINT_REASON_TOPIC_SHIFT = "topic_shift"
CHECKPOINT_REASON_PRE_SWAP = "pre_swap"

DECISION_TYPE_PROVIDER_FAILURE = "provider_failure"
DECISION_TYPE_CHECKPOINT_FAILURE = "checkpoint_failure"
DECISION_TYPE_NO_AVAILABLE_AGENTS = "no_available_agents"

PROVIDER_FAILURE_ACTIONS = [
    "continue",
    "wait_once",
    "swap_next_checkpoint",
    "archive",
    "end",
]
CHECKPOINT_FAILURE_ACTIONS = ["retry_checkpoint", "archive", "end"]
NO_AVAILABLE_AGENT_ACTIONS = ["archive", "end"]

PASS_TOKEN = "PASS"


@dataclass(slots=True)
class ProtocolActionResult:
    room_state: RoomRuntimeState
    round: Round | None = None
    checkpoint_result: CheckpointRunResult | None = None


class ProtocolManager:
    """Owns room lifecycle, round cadence, and checkpoint triggering."""

    def __init__(
        self,
        storage: RoomStorage,
        memory_engine: MemoryEngine,
        provider_layer: ProviderLayer | None = None,
    ) -> None:
        self.storage = storage
        self.memory_engine = memory_engine
        self.provider_layer = provider_layer or memory_engine.provider_layer

    def start_round(
        self,
        seed_message: str,
        author: str,
        *,
        run_agents: bool = True,
    ) -> ProtocolActionResult:
        room = self._load_room()
        state = room.state
        human = self._human_participant(room.config)
        agents = self._agent_participants(room.config)

        if human.participant_id != author:
            raise ValueError("in MVP, every round seed must be authored by the human participant")
        if not agents:
            raise ValueError("cannot start a round without at least one agent participant")
        if state.current_round is not None:
            raise ValueError("cannot open a new round while another round is still open")
        if state.status is RoomStatus.DRAFT:
            state.transition_to(RoomStatus.ACTIVE)
        elif state.status is not RoomStatus.ACTIVE:
            raise ValueError(f"cannot start a round while room status is {state.status.value}")

        round_number = (state.latest_transcript_round_number or 0) + 1
        seed = Message(
            author=author,
            content=seed_message,
            timestamp=utc_now(),
            round_number=round_number,
        )
        current_round = Round(
            round_number=round_number,
            seed_author=author,
            seed_message=seed,
            status=RoundStatus.OPEN,
            participant_outcomes={
                agent.participant_id: ParticipantOutcome.PENDING for agent in agents
            },
        )
        state.current_round = current_round
        state.pending_human_decision = None
        state.updated_at = utc_now()
        self.storage.save_room_state(state)

        if run_agents:
            return self._run_pending_agents()
        return ProtocolActionResult(room_state=self.storage.load_room_state(), round=current_round)

    def submit_response(
        self,
        participant_id: str,
        content: str | None = None,
        *,
        passed: bool = False,
    ) -> ProtocolActionResult:
        state = self.storage.load_room_state()
        current_round = self._require_open_round(state)
        if state.status is not RoomStatus.ACTIVE:
            raise ValueError("responses can only be submitted while the room is active")
        if participant_id == current_round.seed_author:
            raise ValueError("the seed author cannot respond again in the same round")
        if participant_id not in current_round.participant_outcomes:
            raise ValueError(f"participant '{participant_id}' is not eligible to respond in this round")

        current_outcome = current_round.participant_outcomes.get(
            participant_id,
            ParticipantOutcome.PENDING,
        )
        if current_outcome is not ParticipantOutcome.PENDING:
            raise ValueError(f"participant '{participant_id}' already has a terminal round outcome")
        if passed and content is not None:
            raise ValueError("a pass cannot also include response content")
        if not passed and content is None:
            raise ValueError("response content is required unless the participant passes")

        if passed:
            current_round.set_participant_outcome(participant_id, ParticipantOutcome.PASSED)
        else:
            current_round.responses.append(
                Message(
                    author=participant_id,
                    content=content,
                    timestamp=utc_now(),
                    round_number=current_round.round_number,
                )
            )
            current_round.set_participant_outcome(participant_id, ParticipantOutcome.RESPONDED)

        state.current_round = current_round
        state.updated_at = utc_now()
        self.storage.save_room_state(state)
        return ProtocolActionResult(room_state=state, round=current_round)

    def close_round(self) -> ProtocolActionResult:
        state = self.storage.load_room_state()
        current_round = self._require_open_round(state)
        if not self._round_is_ready_to_close(current_round):
            raise ValueError("cannot close a round until every non-seed participant is terminal")

        current_round.transition_to(RoundStatus.CLOSED)
        state.current_round = None
        state.updated_at = utc_now()
        self.storage.save_room_state(state)
        self.memory_engine.append_transcript(current_round)

        checkpoint_result = None
        if self._should_trigger_post_round_checkpoint(current_round.round_number):
            checkpoint_action = self.trigger_checkpoint(reason=CHECKPOINT_REASON_ROUND_CLOSE)
            checkpoint_result = checkpoint_action.checkpoint_result
        current_round.transition_to(RoundStatus.SETTLED)

        latest_state = self.storage.load_room_state()
        if checkpoint_result is not None and checkpoint_result.checkpoint.status.value == "success":
            latest_state = self._pause_if_no_agents_completed_round(latest_state, current_round)

        return ProtocolActionResult(
            room_state=latest_state,
            round=current_round,
            checkpoint_result=checkpoint_result,
        )

    def trigger_checkpoint(self, *, reason: str) -> ProtocolActionResult:
        state = self.storage.load_room_state()
        if state.current_round is not None:
            raise ValueError("cannot run a checkpoint while a round is still open")
        if state.status not in {RoomStatus.ACTIVE, RoomStatus.AWAITING_HUMAN_DECISION}:
            raise ValueError(f"cannot run checkpoint while room status is {state.status.value}")

        if state.status is not RoomStatus.CHECKPOINTING:
            state.transition_to(RoomStatus.CHECKPOINTING)
        state.pending_human_decision = None
        state.updated_at = utc_now()
        self.storage.save_room_state(state)

        checkpoint_result = self.memory_engine.run_checkpoint(reason=reason)
        state = self.storage.load_room_state()
        if checkpoint_result.checkpoint.status.value == "success":
            state.transition_to(RoomStatus.ACTIVE)
            state.pending_human_decision = None
        else:
            state.transition_to(RoomStatus.AWAITING_HUMAN_DECISION)
            state.pending_human_decision = PendingHumanDecision(
                decision_type=DECISION_TYPE_CHECKPOINT_FAILURE,
                allowed_actions=list(CHECKPOINT_FAILURE_ACTIONS),
                error_code=checkpoint_result.checkpoint.error_code,
                error_message=checkpoint_result.checkpoint.error_message,
            )
        state.updated_at = utc_now()
        self.storage.save_room_state(state)
        return ProtocolActionResult(room_state=state, checkpoint_result=checkpoint_result)

    def resolve_provider_failure(self, participant_id: str, action: str) -> ProtocolActionResult:
        state = self.storage.load_room_state()
        pending = state.pending_human_decision
        if pending is None or pending.decision_type != DECISION_TYPE_PROVIDER_FAILURE:
            raise ValueError("there is no pending provider failure to resolve")
        if pending.participant_id != participant_id:
            raise ValueError("pending provider failure does not match the requested participant")
        if action not in pending.allowed_actions:
            raise ValueError(f"action '{action}' is not allowed for this provider failure")

        if action == "continue":
            self._mark_round_participant_unavailable(state, participant_id)
            self._clear_pending_human_decision_and_activate(state)
            return self._run_pending_agents()
        if action == "wait_once":
            return self._retry_failed_participant(participant_id)
        if action == "swap_next_checkpoint":
            self._mark_round_participant_unavailable(state, participant_id)
            state = self.storage.load_room_state()
            state.queued_agent_swap = {
                "participant_id": participant_id,
                "requested_at": utc_now().isoformat().replace("+00:00", "Z"),
                "reason": "provider_failure",
            }
            self._clear_pending_human_decision_and_activate(state)
            return self._run_pending_agents()
        if action == "archive":
            return self.archive_room(reason=f"provider failure for {participant_id}")
        if action == "end":
            return self.end_room(reason=f"provider failure for {participant_id}")
        raise ValueError(f"unsupported provider failure action '{action}'")

    def resolve_checkpoint_failure(self, action: str) -> ProtocolActionResult:
        state = self.storage.load_room_state()
        pending = state.pending_human_decision
        if pending is None or pending.decision_type != DECISION_TYPE_CHECKPOINT_FAILURE:
            raise ValueError("there is no pending checkpoint failure to resolve")
        if action not in pending.allowed_actions:
            raise ValueError(f"action '{action}' is not allowed for this checkpoint failure")

        if action == "retry_checkpoint":
            latest_checkpoint = self._latest_checkpoint()
            if latest_checkpoint is None or latest_checkpoint.status.value != "error":
                raise ValueError("no failed checkpoint exists to retry")
            return self.trigger_checkpoint(reason=latest_checkpoint.reason)
        if action == "archive":
            return self.archive_room(reason="checkpoint failure")
        if action == "end":
            return self.end_room(reason="checkpoint failure")
        raise ValueError(f"unsupported checkpoint failure action '{action}'")

    def archive_room(self, *, reason: str) -> ProtocolActionResult:
        del reason  # reserved for future metrics/logging
        state = self.storage.load_room_state()
        round_result = self._abandon_open_round_if_present(state)
        state = self.storage.load_room_state()
        if state.status is not RoomStatus.ARCHIVED:
            state.transition_to(RoomStatus.ARCHIVED)
        state.pending_human_decision = None
        state.updated_at = utc_now()
        self.storage.save_room_state(state)
        return ProtocolActionResult(room_state=state, round=round_result)

    def end_room(self, *, reason: str) -> ProtocolActionResult:
        del reason  # reserved for future metrics/logging
        state = self.storage.load_room_state()
        round_result = self._abandon_open_round_if_present(state)
        state = self.storage.load_room_state()
        if state.status is not RoomStatus.ENDED:
            state.transition_to(RoomStatus.ENDED)
        state.pending_human_decision = None
        state.updated_at = utc_now()
        self.storage.save_room_state(state)
        return ProtocolActionResult(room_state=state, round=round_result)

    def resume_room(self) -> ProtocolActionResult:
        state = self.storage.load_room_state()
        if state.status is not RoomStatus.ARCHIVED:
            raise ValueError("only archived rooms may be resumed")
        if state.current_round is not None:
            raise ValueError("archived rooms must not retain an open round on resume")
        state.transition_to(RoomStatus.ACTIVE)
        state.pending_human_decision = None
        state.updated_at = utc_now()
        self.storage.save_room_state(state)
        return ProtocolActionResult(room_state=state)

    def add_participant(self, participant_config: Participant) -> RoomConfig:
        room = self._load_room()
        if room.state.current_round is not None:
            raise ValueError("cannot modify participants while a round is open")
        if any(
            existing.participant_id == participant_config.participant_id
            for existing in room.config.participants
        ):
            raise ValueError(f"participant '{participant_config.participant_id}' already exists")
        if participant_config.participant_type is ParticipantType.HUMAN:
            raise ValueError("MVP supports exactly one human participant per room")

        room.config.participants.append(participant_config)
        self.storage.save_room_config(room.config)
        return room.config

    def remove_participant(self, participant_id: str) -> RoomConfig:
        room = self._load_room()
        if room.state.current_round is not None:
            raise ValueError("cannot modify participants while a round is open")

        remaining = [
            participant
            for participant in room.config.participants
            if participant.participant_id != participant_id
        ]
        if len(remaining) == len(room.config.participants):
            raise KeyError(f"participant '{participant_id}' does not exist")
        if not any(
            participant.participant_type is ParticipantType.HUMAN for participant in remaining
        ):
            raise ValueError("rooms must retain exactly one human participant")
        if room.state.status is not RoomStatus.DRAFT and not any(
            participant.participant_type is ParticipantType.AGENT for participant in remaining
        ):
            raise ValueError("non-draft MVP rooms must retain at least one agent participant")

        room.config.participants = remaining
        self.storage.save_room_config(room.config)
        return room.config

    def get_room_status(self) -> JSONDict:
        room = self._load_room()
        state = room.state
        checkpoints = [checkpoint.to_dict() for checkpoint in self.storage.read_checkpoints()]
        return {
            "room_id": state.room_id,
            "status": state.status.value,
            "latest_transcript_round_number": state.latest_transcript_round_number,
            "latest_checkpoint_id": state.latest_checkpoint_id,
            "latest_summary_snapshot_id": state.latest_summary_snapshot_id,
            "latest_structured_state_revision_id": state.latest_structured_state_revision_id,
            "participants": [participant.to_dict() for participant in room.config.participants],
            "current_round": self._round_status_view(state.current_round),
            "pending_human_decision": (
                state.pending_human_decision.to_dict()
                if state.pending_human_decision is not None
                else None
            ),
            "queued_agent_swap": state.queued_agent_swap,
            "checkpoint_history": checkpoints,
        }

    def request_compaction(self) -> ProtocolActionResult:
        return self.trigger_checkpoint(reason=CHECKPOINT_REASON_COMPACTION_REQUEST)

    def request_topic_shift_checkpoint(self) -> ProtocolActionResult:
        return self.trigger_checkpoint(reason=CHECKPOINT_REASON_TOPIC_SHIFT)

    def request_pre_swap_checkpoint(self) -> ProtocolActionResult:
        return self.trigger_checkpoint(reason=CHECKPOINT_REASON_PRE_SWAP)

    def _run_pending_agents(self) -> ProtocolActionResult:
        while True:
            room = self._load_room()
            state = room.state
            current_round = self._require_open_round(state)
            pending_agents = [
                agent
                for agent in self._agent_participants(room.config)
                if current_round.participant_outcomes.get(
                    agent.participant_id,
                    ParticipantOutcome.PENDING,
                )
                is ParticipantOutcome.PENDING
            ]
            if not pending_agents:
                return self.close_round()

            agent = pending_agents[0]
            completion = self._complete_agent(room.config, current_round, agent)
            if completion.status.value == "error":
                state.transition_to(RoomStatus.AWAITING_HUMAN_DECISION)
                state.pending_human_decision = PendingHumanDecision(
                    decision_type=DECISION_TYPE_PROVIDER_FAILURE,
                    participant_id=agent.participant_id,
                    allowed_actions=list(PROVIDER_FAILURE_ACTIONS),
                    error_code=completion.error_code,
                    error_message=completion.error_message,
                )
                state.updated_at = utc_now()
                self.storage.save_room_state(state)
                return ProtocolActionResult(room_state=state, round=current_round)

            content = completion.content.strip()
            if content.upper() == PASS_TOKEN:
                self.submit_response(agent.participant_id, passed=True)
            else:
                self.submit_response(agent.participant_id, content=completion.content)

    def _retry_failed_participant(self, participant_id: str) -> ProtocolActionResult:
        room = self._load_room()
        state = room.state
        current_round = self._require_open_round(state)
        agent = self._require_agent(room.config, participant_id)

        completion = self._complete_agent(room.config, current_round, agent)
        if completion.status.value == "error":
            allowed_actions = [
                action for action in PROVIDER_FAILURE_ACTIONS if action != "wait_once"
            ]
            state.pending_human_decision = PendingHumanDecision(
                decision_type=DECISION_TYPE_PROVIDER_FAILURE,
                participant_id=participant_id,
                allowed_actions=allowed_actions,
                error_code=completion.error_code,
                error_message=completion.error_message,
            )
            state.updated_at = utc_now()
            self.storage.save_room_state(state)
            return ProtocolActionResult(room_state=state, round=current_round)

        self._clear_pending_human_decision_and_activate(state)
        content = completion.content.strip()
        if content.upper() == PASS_TOKEN:
            self.submit_response(participant_id, passed=True)
        else:
            self.submit_response(participant_id, content=completion.content)
        return self._run_pending_agents()

    def _complete_agent(
        self,
        room_config: RoomConfig,
        current_round: Round,
        agent: Agent,
    ):
        context_payload = self.memory_engine.get_context_payload()
        messages = self._build_agent_messages(room_config, current_round.seed_message, agent, context_payload)
        return self.provider_layer.complete(self._agent_model_id(agent), messages)

    def _build_agent_messages(
        self,
        room_config: RoomConfig,
        seed_message: Message,
        agent: Agent,
        context_payload: Any,
    ) -> list[dict[str, str]]:
        user_payload = {
            "room_name": room_config.name,
            "problem_statement": room_config.problem_statement,
            "working_summary": context_payload.summary,
            "structured_state": context_payload.structured_state,
            "seed_message": seed_message.to_dict(),
            "instructions": (
                "Respond to the seed message using the shared summary and structured state. "
                f"If you have no meaningful contribution for this round, reply with exactly {PASS_TOKEN}."
            ),
        }
        return [
            {"role": "system", "content": agent.system_prompt},
            {"role": "user", "content": json.dumps(user_payload, indent=2, sort_keys=True)},
        ]

    def _agent_model_id(self, agent: Agent) -> str:
        if ":" in agent.model_id:
            return agent.model_id
        return f"{agent.provider}:{agent.model_id}"

    def _pause_if_no_agents_completed_round(
        self,
        state: RoomRuntimeState,
        round_data: Round,
    ) -> RoomRuntimeState:
        if state.queued_agent_swap is not None:
            return state
        outcomes = list(round_data.participant_outcomes.values())
        if not outcomes or any(outcome is not ParticipantOutcome.UNAVAILABLE for outcome in outcomes):
            return state

        state.transition_to(RoomStatus.AWAITING_HUMAN_DECISION)
        state.pending_human_decision = PendingHumanDecision(
            decision_type=DECISION_TYPE_NO_AVAILABLE_AGENTS,
            allowed_actions=list(NO_AVAILABLE_AGENT_ACTIONS),
        )
        state.updated_at = utc_now()
        self.storage.save_room_state(state)
        return state

    def _abandon_open_round_if_present(self, state: RoomRuntimeState) -> Round | None:
        if state.current_round is None:
            return None
        current_round = state.current_round
        current_round.transition_to(RoundStatus.ABANDONED)
        state.current_round = None
        state.pending_human_decision = None
        state.updated_at = utc_now()
        self.storage.save_room_state(state)
        self.memory_engine.append_transcript(current_round)
        return current_round

    def _mark_round_participant_unavailable(
        self,
        state: RoomRuntimeState,
        participant_id: str,
    ) -> None:
        current_round = self._require_open_round(state)
        if participant_id not in current_round.participant_outcomes:
            raise ValueError(f"participant '{participant_id}' is not eligible in this round")
        current_round.set_participant_outcome(participant_id, ParticipantOutcome.UNAVAILABLE)
        state.current_round = current_round
        state.updated_at = utc_now()
        self.storage.save_room_state(state)

    def _clear_pending_human_decision_and_activate(self, state: RoomRuntimeState) -> None:
        state.pending_human_decision = None
        if state.status is not RoomStatus.ACTIVE:
            state.transition_to(RoomStatus.ACTIVE)
        state.updated_at = utc_now()
        self.storage.save_room_state(state)

    def _round_is_ready_to_close(self, current_round: Round) -> bool:
        return all(
            outcome in {
                ParticipantOutcome.RESPONDED,
                ParticipantOutcome.PASSED,
                ParticipantOutcome.UNAVAILABLE,
            }
            for outcome in current_round.participant_outcomes.values()
        )

    def _should_trigger_post_round_checkpoint(self, round_number: int) -> bool:
        config = self.storage.load_room_config()
        raw_interval = config.settings.get("checkpoint_interval", 1)
        try:
            interval = max(1, int(raw_interval))
        except (TypeError, ValueError):
            interval = 1
        return round_number % interval == 0

    def _round_status_view(self, current_round: Round | None) -> JSONDict | None:
        if current_round is None:
            return None
        view: JSONDict = {
            "round_number": current_round.round_number,
            "status": current_round.status.value,
            "seed_author": current_round.seed_author,
            "seed_message": current_round.seed_message.to_dict(),
            "participant_outcomes": {
                participant_id: outcome.value
                for participant_id, outcome in current_round.participant_outcomes.items()
            },
        }
        if current_round.status is RoundStatus.OPEN:
            view["response_count"] = len(current_round.responses)
        else:
            view["responses"] = [response.to_dict() for response in current_round.responses]
        return view

    def _load_room(self) -> Room:
        return Room(
            config=self.storage.load_room_config(),
            state=self.storage.load_room_state(),
        )

    def _latest_checkpoint(self):
        checkpoints = self.storage.read_checkpoints()
        if not checkpoints:
            return None
        return checkpoints[-1]

    def _human_participant(self, config: RoomConfig) -> Participant:
        for participant in config.participants:
            if participant.participant_type is ParticipantType.HUMAN:
                return participant
        raise ValueError("room has no human participant")

    def _agent_participants(self, config: RoomConfig) -> list[Agent]:
        return [
            participant
            for participant in config.participants
            if isinstance(participant, Agent)
        ]

    def _require_agent(self, config: RoomConfig, participant_id: str) -> Agent:
        for participant in self._agent_participants(config):
            if participant.participant_id == participant_id:
                return participant
        raise KeyError(f"agent participant '{participant_id}' does not exist")

    def _require_open_round(self, state: RoomRuntimeState) -> Round:
        if state.current_round is None:
            raise ValueError("there is no open round")
        return state.current_round
