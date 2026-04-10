from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from deliberation_room.domain import (
    ActionItem,
    ActiveOverride,
    Agent,
    CandidateSolution,
    Checkpoint,
    CheckpointStatus,
    CompletionResult,
    CompletionStatus,
    Decision,
    Disagreement,
    DisagreementPosition,
    EditLogEntry,
    Message,
    OpenQuestion,
    Participant,
    ParticipantOutcome,
    ParticipantType,
    PendingHumanDecision,
    RevisionSource,
    Room,
    RoomConfig,
    RoomRuntimeState,
    RoomStatus,
    Round,
    RoundStatus,
    StructuredState,
    SummarySnapshot,
    TransitionError,
    ensure_participant_outcome_transition,
    ensure_room_status_transition,
    ensure_round_status_transition,
)


FIXED_TIME = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)


def build_human() -> Participant:
    return Participant(
        participant_id="human_1",
        display_name="Human",
        participant_type=ParticipantType.HUMAN,
    )


def build_agent() -> Agent:
    return Agent(
        participant_id="agent_1",
        display_name="Skeptic",
        role="Skeptic",
        system_prompt="Challenge assumptions.",
        model_id="gpt-test",
        provider="openai",
    )


def build_round(status: RoundStatus = RoundStatus.OPEN) -> Round:
    seed = Message(
        author="human_1",
        content="How should we structure the engine?",
        timestamp=FIXED_TIME,
        round_number=1,
    )
    response = Message(
        author="agent_1",
        content="Keep the engine headless.",
        timestamp=FIXED_TIME + timedelta(minutes=1),
        round_number=1,
    )
    return Round(
        round_number=1,
        seed_author="human_1",
        seed_message=seed,
        status=status,
        responses=[response] if status is not RoundStatus.OPEN else [],
        participant_outcomes=(
            {"agent_1": ParticipantOutcome.RESPONDED}
            if status is not RoundStatus.OPEN
            else {"agent_1": ParticipantOutcome.PENDING}
        ),
    )


def build_structured_state() -> StructuredState:
    return StructuredState(
        schema_version=1,
        revision_id="state_0001",
        previous_revision_id=None,
        checkpoint_id="chk_0001",
        updated_at=FIXED_TIME,
        updated_by="system",
        revision_source=RevisionSource.CHECKPOINT,
        current_problem="Define the MVP architecture.",
        candidate_solutions=[
            CandidateSolution(
                id="sol_1",
                description="Framework-free Python engine.",
                status="active",
                origin="system",
            )
        ],
        open_questions=[
            OpenQuestion(
                id="q_1",
                text="How should checkpoint retries be modeled?",
                raised_by="agent_1",
                round_raised=1,
            )
        ],
        decisions=[
            Decision(
                id="dec_1",
                text="Use local filesystem persistence in V1.",
                round_decided=1,
                origin="system",
            )
        ],
        disagreements=[
            Disagreement(
                id="dis_1",
                description="Whether to add a web API during MVP.",
                positions=[
                    DisagreementPosition(participant="human_1", stance="not yet"),
                    DisagreementPosition(participant="agent_1", stance="avoid it"),
                ],
            )
        ],
        action_items=[ActionItem(id="act_1", text="Implement persistence skeleton", assignee="human_1")],
        active_overrides=[
            ActiveOverride(
                field_path="/current_problem",
                value="Define the implementation skeleton.",
                author="human_1",
                created_at=FIXED_TIME,
            )
        ],
        edit_log=[
            EditLogEntry(
                field_path="/current_problem",
                old_value="Define the MVP architecture.",
                new_value="Define the implementation skeleton.",
                author="human_1",
                source="human_edit",
                timestamp=FIXED_TIME + timedelta(minutes=2),
            )
        ],
    )


class DomainModelTests(unittest.TestCase):
    def test_room_status_transitions_validate(self) -> None:
        ensure_room_status_transition(RoomStatus.DRAFT, RoomStatus.ACTIVE)
        with self.assertRaises(TransitionError):
            ensure_room_status_transition(RoomStatus.ENDED, RoomStatus.ACTIVE)

    def test_round_status_transitions_validate(self) -> None:
        ensure_round_status_transition(RoundStatus.OPEN, RoundStatus.CLOSED)
        with self.assertRaises(TransitionError):
            ensure_round_status_transition(RoundStatus.CLOSED, RoundStatus.OPEN)

    def test_participant_outcome_transitions_validate(self) -> None:
        ensure_participant_outcome_transition(ParticipantOutcome.PENDING, ParticipantOutcome.PASSED)
        with self.assertRaises(TransitionError):
            ensure_participant_outcome_transition(
                ParticipantOutcome.RESPONDED,
                ParticipantOutcome.UNAVAILABLE,
            )

    def test_room_requires_exactly_one_human(self) -> None:
        config = RoomConfig(
            room_id="room_1",
            name="Architecture",
            problem_statement="Decide how to build the MVP.",
            created_at=FIXED_TIME,
            participants=[build_agent()],
        )
        state = RoomRuntimeState(
            room_id="room_1",
            status=RoomStatus.DRAFT,
            created_at=FIXED_TIME,
            updated_at=FIXED_TIME,
        )
        with self.assertRaises(ValueError):
            Room(config=config, state=state)

    def test_round_rejects_double_response(self) -> None:
        duplicate = Message(
            author="agent_1",
            content="Second answer",
            timestamp=FIXED_TIME,
            round_number=1,
        )
        seed = Message(
            author="human_1",
            content="Seed",
            timestamp=FIXED_TIME,
            round_number=1,
        )
        with self.assertRaises(ValueError):
            Round(
                round_number=1,
                seed_author="human_1",
                seed_message=seed,
                status=RoundStatus.CLOSED,
                responses=[duplicate, duplicate],
                participant_outcomes={"agent_1": ParticipantOutcome.RESPONDED},
            )

    def test_round_to_transcript_record_requires_terminal_status(self) -> None:
        round_obj = build_round(status=RoundStatus.OPEN)
        with self.assertRaises(ValueError):
            round_obj.to_transcript_record(recorded_at=FIXED_TIME)

    def test_serialization_round_trip_for_canonical_objects(self) -> None:
        room = Room(
            config=RoomConfig(
                room_id="room_1",
                name="Architecture",
                problem_statement="Decide how to build the MVP.",
                created_at=FIXED_TIME,
                participants=[build_human(), build_agent()],
                settings={"checkpoint_interval": 1},
            ),
            state=RoomRuntimeState(
                room_id="room_1",
                status=RoomStatus.AWAITING_HUMAN_DECISION,
                created_at=FIXED_TIME,
                updated_at=FIXED_TIME,
                latest_transcript_round_number=1,
                latest_checkpoint_id="chk_0001",
                latest_summary_snapshot_id="sum_0001",
                latest_structured_state_revision_id="state_0001",
                current_round=build_round(status=RoundStatus.OPEN),
                pending_human_decision=PendingHumanDecision(
                    decision_type="provider_failure",
                    participant_id="agent_1",
                    allowed_actions=["continue", "wait_once", "archive"],
                ),
            ),
        )
        checkpoint = Checkpoint(
            checkpoint_id="chk_0001",
            round_number=1,
            reason="round_close",
            created_at=FIXED_TIME,
            status=CheckpointStatus.SUCCESS,
            summary_snapshot_id="sum_0001",
            structured_state_revision_id="state_0001",
        )
        summary = SummarySnapshot(
            summary_id="sum_0001",
            checkpoint_id="chk_0001",
            round_number=1,
            created_at=FIXED_TIME,
            content="The room prefers a framework-free engine.",
        )
        completion = CompletionResult(
            content="Keep the room engine headless.",
            token_usage={"input": 120, "output": 55},
            latency_ms=430,
            status=CompletionStatus.SUCCESS,
            provider_metadata={"provider": "openai"},
        )
        structured_state = build_structured_state()

        self.assertEqual(Room.from_dict(room.to_dict()), room)
        self.assertEqual(Checkpoint.from_dict(checkpoint.to_dict()), checkpoint)
        self.assertEqual(SummarySnapshot.from_dict(summary.to_dict()), summary)
        self.assertEqual(CompletionResult.from_dict(completion.to_dict()), completion)
        self.assertEqual(StructuredState.from_dict(structured_state.to_dict()), structured_state)


if __name__ == "__main__":
    unittest.main()
