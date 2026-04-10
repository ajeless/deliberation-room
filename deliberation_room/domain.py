"""Canonical domain objects and serialization helpers for Deliberation Room."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping

JSONScalar = str | int | float | bool | None
JSONValue = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]
JSONDict = dict[str, JSONValue]


def utc_now() -> datetime:
    """Return the current time as a UTC timestamp."""

    return datetime.now(timezone.utc)


def _serialize_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime values must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _deserialize_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _to_json_value(value: Any) -> JSONValue:
    if is_dataclass(value):
        return {item.name: _to_json_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return _serialize_datetime(value)
    if isinstance(value, list):
        return [_to_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_json_value(item) for key, item in value.items()}
    return value


class TransitionError(ValueError):
    """Raised when a lifecycle transition violates the canonical state machine."""


class ParticipantType(StrEnum):
    HUMAN = "human"
    AGENT = "agent"


class RoomStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    AWAITING_HUMAN_DECISION = "awaiting_human_decision"
    CHECKPOINTING = "checkpointing"
    ARCHIVED = "archived"
    ENDED = "ended"


class RoundStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    SETTLED = "settled"
    ABANDONED = "abandoned"


class ParticipantOutcome(StrEnum):
    PENDING = "pending"
    RESPONDED = "responded"
    PASSED = "passed"
    UNAVAILABLE = "unavailable"


class CheckpointStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"


class RevisionSource(StrEnum):
    CHECKPOINT = "checkpoint"
    HUMAN_EDIT = "human_edit"
    HUMAN_CLEAR = "human_clear"


class CompletionStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"


ROOM_STATUS_TRANSITIONS: dict[RoomStatus, frozenset[RoomStatus]] = {
    RoomStatus.DRAFT: frozenset({RoomStatus.ACTIVE, RoomStatus.ARCHIVED, RoomStatus.ENDED}),
    RoomStatus.ACTIVE: frozenset(
        {
            RoomStatus.AWAITING_HUMAN_DECISION,
            RoomStatus.CHECKPOINTING,
            RoomStatus.ARCHIVED,
            RoomStatus.ENDED,
        }
    ),
    RoomStatus.AWAITING_HUMAN_DECISION: frozenset(
        {RoomStatus.ACTIVE, RoomStatus.CHECKPOINTING, RoomStatus.ARCHIVED, RoomStatus.ENDED}
    ),
    RoomStatus.CHECKPOINTING: frozenset(
        {RoomStatus.ACTIVE, RoomStatus.AWAITING_HUMAN_DECISION, RoomStatus.ARCHIVED, RoomStatus.ENDED}
    ),
    RoomStatus.ARCHIVED: frozenset({RoomStatus.ACTIVE}),
    RoomStatus.ENDED: frozenset(),
}

ROUND_STATUS_TRANSITIONS: dict[RoundStatus, frozenset[RoundStatus]] = {
    RoundStatus.OPEN: frozenset({RoundStatus.CLOSED, RoundStatus.ABANDONED}),
    RoundStatus.CLOSED: frozenset({RoundStatus.SETTLED}),
    RoundStatus.SETTLED: frozenset(),
    RoundStatus.ABANDONED: frozenset(),
}

PARTICIPANT_OUTCOME_TRANSITIONS: dict[ParticipantOutcome, frozenset[ParticipantOutcome]] = {
    ParticipantOutcome.PENDING: frozenset(
        {ParticipantOutcome.RESPONDED, ParticipantOutcome.PASSED, ParticipantOutcome.UNAVAILABLE}
    ),
    ParticipantOutcome.RESPONDED: frozenset(),
    ParticipantOutcome.PASSED: frozenset(),
    ParticipantOutcome.UNAVAILABLE: frozenset(),
}


def _ensure_transition(
    current: StrEnum,
    target: StrEnum,
    allowed_transitions: Mapping[StrEnum, frozenset[StrEnum]],
    label: str,
) -> None:
    if target not in allowed_transitions[current]:
        raise TransitionError(f"invalid {label} transition: {current.value} -> {target.value}")


def ensure_room_status_transition(current: RoomStatus, target: RoomStatus) -> None:
    _ensure_transition(current, target, ROOM_STATUS_TRANSITIONS, "room status")


def ensure_round_status_transition(current: RoundStatus, target: RoundStatus) -> None:
    _ensure_transition(current, target, ROUND_STATUS_TRANSITIONS, "round status")


def ensure_participant_outcome_transition(
    current: ParticipantOutcome, target: ParticipantOutcome
) -> None:
    _ensure_transition(
        current,
        target,
        PARTICIPANT_OUTCOME_TRANSITIONS,
        "participant outcome",
    )


@dataclass(slots=True)
class Participant:
    participant_id: str
    display_name: str
    participant_type: ParticipantType

    def to_dict(self) -> JSONDict:
        return _to_json_value(self)  # type: ignore[return-value]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Participant":
        participant_type = ParticipantType(data["participant_type"])
        if participant_type is ParticipantType.AGENT:
            return Agent.from_dict(data)
        return cls(
            participant_id=str(data["participant_id"]),
            display_name=str(data["display_name"]),
            participant_type=participant_type,
        )


@dataclass(slots=True)
class Agent(Participant):
    role: str
    system_prompt: str
    model_id: str
    provider: str
    participant_type: ParticipantType = field(default=ParticipantType.AGENT, init=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Agent":
        return cls(
            participant_id=str(data["participant_id"]),
            display_name=str(data["display_name"]),
            role=str(data["role"]),
            system_prompt=str(data["system_prompt"]),
            model_id=str(data["model_id"]),
            provider=str(data["provider"]),
        )


@dataclass(slots=True)
class Message:
    author: str
    content: str
    timestamp: datetime
    round_number: int

    def to_dict(self) -> JSONDict:
        return _to_json_value(self)  # type: ignore[return-value]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Message":
        return cls(
            author=str(data["author"]),
            content=str(data["content"]),
            timestamp=_deserialize_datetime(str(data["timestamp"])),
            round_number=int(data["round_number"]),
        )


@dataclass(slots=True)
class Round:
    round_number: int
    seed_author: str
    seed_message: Message
    status: RoundStatus
    responses: list[Message] = field(default_factory=list)
    participant_outcomes: dict[str, ParticipantOutcome] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.seed_message.author != self.seed_author:
            raise ValueError("seed message author must match seed_author")
        if self.seed_message.round_number != self.round_number:
            raise ValueError("seed message round number must match round")
        seen_authors: set[str] = set()
        for response in self.responses:
            if response.round_number != self.round_number:
                raise ValueError("response round number must match round")
            if response.author == self.seed_author:
                raise ValueError("seed author cannot also submit a response in the same round")
            if response.author in seen_authors:
                raise ValueError("participant cannot respond twice in the same round")
            seen_authors.add(response.author)

    def transition_to(self, status: RoundStatus) -> None:
        ensure_round_status_transition(self.status, status)
        self.status = status

    def set_participant_outcome(self, participant_id: str, outcome: ParticipantOutcome) -> None:
        current = self.participant_outcomes.get(participant_id, ParticipantOutcome.PENDING)
        ensure_participant_outcome_transition(current, outcome)
        self.participant_outcomes[participant_id] = outcome

    def to_transcript_record(self, *, recorded_at: datetime | None = None) -> "RoundTranscriptRecord":
        if self.status not in {RoundStatus.CLOSED, RoundStatus.ABANDONED}:
            raise ValueError("round must be closed or abandoned before writing transcript history")
        return RoundTranscriptRecord(
            round_number=self.round_number,
            round_exit_status=self.status,
            seed_message=self.seed_message,
            responses=list(self.responses),
            participant_outcomes=dict(self.participant_outcomes),
            recorded_at=recorded_at or utc_now(),
        )

    def to_dict(self) -> JSONDict:
        return _to_json_value(self)  # type: ignore[return-value]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Round":
        return cls(
            round_number=int(data["round_number"]),
            seed_author=str(data["seed_author"]),
            seed_message=Message.from_dict(data["seed_message"]),
            status=RoundStatus(data["status"]),
            responses=[Message.from_dict(item) for item in data.get("responses", [])],
            participant_outcomes={
                str(participant_id): ParticipantOutcome(value)
                for participant_id, value in dict(data.get("participant_outcomes", {})).items()
            },
        )


@dataclass(slots=True)
class RoundTranscriptRecord:
    round_number: int
    round_exit_status: RoundStatus
    seed_message: Message
    responses: list[Message]
    participant_outcomes: dict[str, ParticipantOutcome]
    recorded_at: datetime

    def __post_init__(self) -> None:
        if self.round_exit_status not in {RoundStatus.CLOSED, RoundStatus.ABANDONED}:
            raise ValueError("transcript records must represent closed or abandoned rounds")
        if self.seed_message.round_number != self.round_number:
            raise ValueError("seed message round number must match transcript record")
        for response in self.responses:
            if response.round_number != self.round_number:
                raise ValueError("response round number must match transcript record")

    def to_dict(self) -> JSONDict:
        return _to_json_value(self)  # type: ignore[return-value]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RoundTranscriptRecord":
        return cls(
            round_number=int(data["round_number"]),
            round_exit_status=RoundStatus(data["round_exit_status"]),
            seed_message=Message.from_dict(data["seed_message"]),
            responses=[Message.from_dict(item) for item in data.get("responses", [])],
            participant_outcomes={
                str(participant_id): ParticipantOutcome(value)
                for participant_id, value in dict(data.get("participant_outcomes", {})).items()
            },
            recorded_at=_deserialize_datetime(str(data["recorded_at"])),
        )


@dataclass(slots=True)
class PendingHumanDecision:
    decision_type: str
    allowed_actions: list[str]
    participant_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> JSONDict:
        data = _to_json_value(self)  # type: ignore[assignment]
        data["type"] = data.pop("decision_type")
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PendingHumanDecision":
        decision_type = data.get("type", data.get("decision_type"))
        return cls(
            decision_type=str(decision_type),
            allowed_actions=[str(action) for action in data.get("allowed_actions", [])],
            participant_id=str(data["participant_id"]) if data.get("participant_id") is not None else None,
            error_code=str(data["error_code"]) if data.get("error_code") is not None else None,
            error_message=str(data["error_message"]) if data.get("error_message") is not None else None,
        )


@dataclass(slots=True)
class RoomConfig:
    room_id: str
    name: str
    problem_statement: str
    created_at: datetime
    participants: list[Participant]
    settings: JSONDict = field(default_factory=dict)

    def to_dict(self) -> JSONDict:
        return _to_json_value(self)  # type: ignore[return-value]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RoomConfig":
        return cls(
            room_id=str(data["room_id"]),
            name=str(data["name"]),
            problem_statement=str(data["problem_statement"]),
            created_at=_deserialize_datetime(str(data["created_at"])),
            participants=[Participant.from_dict(item) for item in data.get("participants", [])],
            settings=dict(data.get("settings", {})),
        )


@dataclass(slots=True)
class RoomRuntimeState:
    room_id: str
    status: RoomStatus
    created_at: datetime
    updated_at: datetime
    latest_transcript_round_number: int | None = None
    latest_checkpoint_id: str | None = None
    latest_summary_snapshot_id: str | None = None
    latest_structured_state_revision_id: str | None = None
    current_round: Round | None = None
    pending_human_decision: PendingHumanDecision | None = None
    queued_agent_swap: JSONDict | None = None

    def __post_init__(self) -> None:
        if self.current_round is not None and self.current_round.status is not RoundStatus.OPEN:
            raise ValueError("current_round must remain open while stored in room runtime state")
        if self.status in {RoomStatus.ARCHIVED, RoomStatus.ENDED} and self.current_round is not None:
            raise ValueError("archived or ended rooms cannot retain an open current_round")
        if self.pending_human_decision is not None and self.status is not RoomStatus.AWAITING_HUMAN_DECISION:
            raise ValueError("pending_human_decision requires awaiting_human_decision room status")

    def transition_to(self, status: RoomStatus, *, changed_at: datetime | None = None) -> None:
        ensure_room_status_transition(self.status, status)
        self.status = status
        self.updated_at = changed_at or utc_now()

    def to_dict(self) -> JSONDict:
        return _to_json_value(self)  # type: ignore[return-value]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RoomRuntimeState":
        return cls(
            room_id=str(data["room_id"]),
            status=RoomStatus(data["status"]),
            created_at=_deserialize_datetime(str(data["created_at"])),
            updated_at=_deserialize_datetime(str(data["updated_at"])),
            latest_transcript_round_number=(
                int(data["latest_transcript_round_number"])
                if data.get("latest_transcript_round_number") is not None
                else None
            ),
            latest_checkpoint_id=(
                str(data["latest_checkpoint_id"]) if data.get("latest_checkpoint_id") is not None else None
            ),
            latest_summary_snapshot_id=(
                str(data["latest_summary_snapshot_id"])
                if data.get("latest_summary_snapshot_id") is not None
                else None
            ),
            latest_structured_state_revision_id=(
                str(data["latest_structured_state_revision_id"])
                if data.get("latest_structured_state_revision_id") is not None
                else None
            ),
            current_round=Round.from_dict(data["current_round"]) if data.get("current_round") else None,
            pending_human_decision=(
                PendingHumanDecision.from_dict(data["pending_human_decision"])
                if data.get("pending_human_decision")
                else None
            ),
            queued_agent_swap=dict(data["queued_agent_swap"]) if data.get("queued_agent_swap") else None,
        )


@dataclass(slots=True)
class Room:
    config: RoomConfig
    state: RoomRuntimeState

    def __post_init__(self) -> None:
        if self.config.room_id != self.state.room_id:
            raise ValueError("room config and runtime state must share the same room_id")
        human_count = sum(
            participant.participant_type is ParticipantType.HUMAN for participant in self.config.participants
        )
        if human_count != 1:
            raise ValueError("MVP rooms must contain exactly one human participant")
        agent_count = sum(
            participant.participant_type is ParticipantType.AGENT for participant in self.config.participants
        )
        if self.state.status is not RoomStatus.DRAFT and agent_count < 1:
            raise ValueError("non-draft MVP rooms must contain at least one agent participant")

    def to_dict(self) -> JSONDict:
        return {
            "config": self.config.to_dict(),
            "state": self.state.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Room":
        return cls(
            config=RoomConfig.from_dict(data["config"]),
            state=RoomRuntimeState.from_dict(data["state"]),
        )


@dataclass(slots=True)
class Checkpoint:
    checkpoint_id: str
    round_number: int
    reason: str
    created_at: datetime
    status: CheckpointStatus
    summary_snapshot_id: str | None = None
    structured_state_revision_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> JSONDict:
        return _to_json_value(self)  # type: ignore[return-value]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Checkpoint":
        return cls(
            checkpoint_id=str(data["checkpoint_id"]),
            round_number=int(data["round_number"]),
            reason=str(data["reason"]),
            created_at=_deserialize_datetime(str(data["created_at"])),
            status=CheckpointStatus(data["status"]),
            summary_snapshot_id=(
                str(data["summary_snapshot_id"]) if data.get("summary_snapshot_id") is not None else None
            ),
            structured_state_revision_id=(
                str(data["structured_state_revision_id"])
                if data.get("structured_state_revision_id") is not None
                else None
            ),
            error_code=str(data["error_code"]) if data.get("error_code") is not None else None,
            error_message=str(data["error_message"]) if data.get("error_message") is not None else None,
        )


@dataclass(slots=True)
class SummarySnapshot:
    summary_id: str
    checkpoint_id: str
    round_number: int
    created_at: datetime
    content: str

    def to_dict(self) -> JSONDict:
        return _to_json_value(self)  # type: ignore[return-value]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SummarySnapshot":
        return cls(
            summary_id=str(data["summary_id"]),
            checkpoint_id=str(data["checkpoint_id"]),
            round_number=int(data["round_number"]),
            created_at=_deserialize_datetime(str(data["created_at"])),
            content=str(data["content"]),
        )


@dataclass(slots=True)
class CandidateSolution:
    id: str
    description: str
    status: str
    origin: str

    def to_dict(self) -> JSONDict:
        return _to_json_value(self)  # type: ignore[return-value]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CandidateSolution":
        return cls(
            id=str(data["id"]),
            description=str(data["description"]),
            status=str(data["status"]),
            origin=str(data["origin"]),
        )


@dataclass(slots=True)
class OpenQuestion:
    id: str
    text: str
    raised_by: str
    round_raised: int

    def to_dict(self) -> JSONDict:
        return _to_json_value(self)  # type: ignore[return-value]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OpenQuestion":
        return cls(
            id=str(data["id"]),
            text=str(data["text"]),
            raised_by=str(data["raised_by"]),
            round_raised=int(data["round_raised"]),
        )


@dataclass(slots=True)
class Decision:
    id: str
    text: str
    round_decided: int
    origin: str

    def to_dict(self) -> JSONDict:
        return _to_json_value(self)  # type: ignore[return-value]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Decision":
        return cls(
            id=str(data["id"]),
            text=str(data["text"]),
            round_decided=int(data["round_decided"]),
            origin=str(data["origin"]),
        )


@dataclass(slots=True)
class DisagreementPosition:
    participant: str
    stance: str

    def to_dict(self) -> JSONDict:
        return _to_json_value(self)  # type: ignore[return-value]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DisagreementPosition":
        return cls(
            participant=str(data["participant"]),
            stance=str(data["stance"]),
        )


@dataclass(slots=True)
class Disagreement:
    id: str
    description: str
    positions: list[DisagreementPosition]

    def to_dict(self) -> JSONDict:
        return _to_json_value(self)  # type: ignore[return-value]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Disagreement":
        return cls(
            id=str(data["id"]),
            description=str(data["description"]),
            positions=[DisagreementPosition.from_dict(item) for item in data.get("positions", [])],
        )


@dataclass(slots=True)
class ActionItem:
    id: str
    text: str
    assignee: str | None = None

    def to_dict(self) -> JSONDict:
        return _to_json_value(self)  # type: ignore[return-value]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ActionItem":
        return cls(
            id=str(data["id"]),
            text=str(data["text"]),
            assignee=str(data["assignee"]) if data.get("assignee") is not None else None,
        )


@dataclass(slots=True)
class ActiveOverride:
    field_path: str
    value: JSONValue
    author: str
    created_at: datetime

    def to_dict(self) -> JSONDict:
        return _to_json_value(self)  # type: ignore[return-value]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ActiveOverride":
        return cls(
            field_path=str(data["field_path"]),
            value=data.get("value"),
            author=str(data["author"]),
            created_at=_deserialize_datetime(str(data["created_at"])),
        )


@dataclass(slots=True)
class EditLogEntry:
    field_path: str
    old_value: JSONValue
    new_value: JSONValue
    author: str
    source: str
    timestamp: datetime

    def to_dict(self) -> JSONDict:
        return _to_json_value(self)  # type: ignore[return-value]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EditLogEntry":
        return cls(
            field_path=str(data["field_path"]),
            old_value=data.get("old_value"),
            new_value=data.get("new_value"),
            author=str(data["author"]),
            source=str(data["source"]),
            timestamp=_deserialize_datetime(str(data["timestamp"])),
        )


@dataclass(slots=True)
class StructuredState:
    schema_version: int
    revision_id: str
    previous_revision_id: str | None
    checkpoint_id: str | None
    updated_at: datetime
    updated_by: str
    revision_source: RevisionSource
    current_problem: str
    candidate_solutions: list[CandidateSolution] = field(default_factory=list)
    open_questions: list[OpenQuestion] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    disagreements: list[Disagreement] = field(default_factory=list)
    action_items: list[ActionItem] = field(default_factory=list)
    active_overrides: list[ActiveOverride] = field(default_factory=list)
    edit_log: list[EditLogEntry] = field(default_factory=list)

    def to_dict(self) -> JSONDict:
        return _to_json_value(self)  # type: ignore[return-value]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StructuredState":
        return cls(
            schema_version=int(data["schema_version"]),
            revision_id=str(data["revision_id"]),
            previous_revision_id=(
                str(data["previous_revision_id"]) if data.get("previous_revision_id") is not None else None
            ),
            checkpoint_id=str(data["checkpoint_id"]) if data.get("checkpoint_id") is not None else None,
            updated_at=_deserialize_datetime(str(data["updated_at"])),
            updated_by=str(data["updated_by"]),
            revision_source=RevisionSource(data["revision_source"]),
            current_problem=str(data["current_problem"]),
            candidate_solutions=[
                CandidateSolution.from_dict(item) for item in data.get("candidate_solutions", [])
            ],
            open_questions=[OpenQuestion.from_dict(item) for item in data.get("open_questions", [])],
            decisions=[Decision.from_dict(item) for item in data.get("decisions", [])],
            disagreements=[Disagreement.from_dict(item) for item in data.get("disagreements", [])],
            action_items=[ActionItem.from_dict(item) for item in data.get("action_items", [])],
            active_overrides=[
                ActiveOverride.from_dict(item) for item in data.get("active_overrides", [])
            ],
            edit_log=[EditLogEntry.from_dict(item) for item in data.get("edit_log", [])],
        )


@dataclass(slots=True)
class CompletionResult:
    content: str
    token_usage: dict[str, int]
    latency_ms: int
    status: CompletionStatus
    error_code: str | None = None
    error_message: str | None = None
    provider_metadata: JSONDict | None = None

    def to_dict(self) -> JSONDict:
        return _to_json_value(self)  # type: ignore[return-value]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CompletionResult":
        return cls(
            content=str(data["content"]),
            token_usage={str(key): int(value) for key, value in dict(data["token_usage"]).items()},
            latency_ms=int(data["latency_ms"]),
            status=CompletionStatus(data["status"]),
            error_code=str(data["error_code"]) if data.get("error_code") is not None else None,
            error_message=str(data["error_message"]) if data.get("error_message") is not None else None,
            provider_metadata=dict(data["provider_metadata"]) if data.get("provider_metadata") else None,
        )
