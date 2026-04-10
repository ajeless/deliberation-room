from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from deliberation_room.domain import (
    ActionItem,
    ActiveOverride,
    Agent,
    CandidateSolution,
    Checkpoint,
    CheckpointStatus,
    Decision,
    Disagreement,
    DisagreementPosition,
    EditLogEntry,
    Message,
    OpenQuestion,
    Participant,
    ParticipantOutcome,
    ParticipantType,
    RevisionSource,
    Room,
    RoomConfig,
    RoomRuntimeState,
    RoomStatus,
    Round,
    RoundStatus,
    StructuredState,
    SummarySnapshot,
)
from deliberation_room.persistence import RoomStorage


FIXED_TIME = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)


def build_room() -> Room:
    human = Participant(
        participant_id="human_1",
        display_name="Human",
        participant_type=ParticipantType.HUMAN,
    )
    agent = Agent(
        participant_id="agent_1",
        display_name="Generalist",
        role="Generalist",
        system_prompt="Contribute broadly useful reasoning.",
        model_id="gpt-test",
        provider="openai",
    )
    seed = Message(
        author="human_1",
        content="How should we persist state?",
        timestamp=FIXED_TIME,
        round_number=1,
    )
    open_round = Round(
        round_number=1,
        seed_author="human_1",
        seed_message=seed,
        status=RoundStatus.OPEN,
        participant_outcomes={"agent_1": ParticipantOutcome.PENDING},
    )
    config = RoomConfig(
        room_id="room_1",
        name="Persistence",
        problem_statement="Define the on-disk persistence layout.",
        created_at=FIXED_TIME,
        participants=[human, agent],
        settings={"checkpoint_interval": 1},
    )
    state = RoomRuntimeState(
        room_id="room_1",
        status=RoomStatus.ACTIVE,
        created_at=FIXED_TIME,
        updated_at=FIXED_TIME,
        current_round=open_round,
    )
    return Room(config=config, state=state)


def build_transcript_record(status: RoundStatus) -> Round:
    seed = Message(
        author="human_1",
        content="Should we keep transcript rows immutable?",
        timestamp=FIXED_TIME,
        round_number=2,
    )
    responses = []
    outcome = ParticipantOutcome.UNAVAILABLE
    if status is RoundStatus.CLOSED:
        responses = [
            Message(
                author="agent_1",
                content="Yes. Track settlement elsewhere.",
                timestamp=FIXED_TIME + timedelta(minutes=1),
                round_number=2,
            )
        ]
        outcome = ParticipantOutcome.RESPONDED
    return Round(
        round_number=2,
        seed_author="human_1",
        seed_message=seed,
        status=status,
        responses=responses,
        participant_outcomes={"agent_1": outcome},
    )


def build_summary_snapshot() -> SummarySnapshot:
    return SummarySnapshot(
        summary_id="sum_0001",
        checkpoint_id="chk_0001",
        round_number=2,
        created_at=FIXED_TIME,
        content="Immutable transcript rows and mutable runtime state.",
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
        current_problem="Lock down persistence contracts.",
        candidate_solutions=[
            CandidateSolution(
                id="sol_1",
                description="Separate room_config and room_state files.",
                status="active",
                origin="system",
            )
        ],
        open_questions=[
            OpenQuestion(
                id="q_1",
                text="How should metrics rows be shaped?",
                raised_by="human_1",
                round_raised=2,
            )
        ],
        decisions=[
            Decision(
                id="dec_1",
                text="Keep transcript rows immutable.",
                round_decided=2,
                origin="system",
            )
        ],
        disagreements=[
            Disagreement(
                id="dis_1",
                description="Whether metrics need a canonical schema in Phase 1.",
                positions=[DisagreementPosition(participant="agent_1", stance="defer slightly")],
            )
        ],
        action_items=[ActionItem(id="act_1", text="Implement the storage class")],
        active_overrides=[
            ActiveOverride(
                field_path="/current_problem",
                value="Implement the storage skeleton.",
                author="human_1",
                created_at=FIXED_TIME,
            )
        ],
        edit_log=[
            EditLogEntry(
                field_path="/current_problem",
                old_value="Lock down persistence contracts.",
                new_value="Implement the storage skeleton.",
                author="human_1",
                source="human_edit",
                timestamp=FIXED_TIME,
            )
        ],
    )


class RoomStorageTests(unittest.TestCase):
    def test_room_storage_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "room_1"
            storage = RoomStorage(root)
            room = build_room()

            storage.initialize_room(room)
            self.assertEqual(storage.load_room_config(), room.config)
            self.assertEqual(storage.load_room_state(), room.state)

            closed_record = build_transcript_record(RoundStatus.CLOSED).to_transcript_record(
                recorded_at=FIXED_TIME + timedelta(minutes=2)
            )
            abandoned_record = build_transcript_record(RoundStatus.ABANDONED).to_transcript_record(
                recorded_at=FIXED_TIME + timedelta(minutes=3)
            )
            storage.append_transcript(closed_record)
            storage.append_transcript(abandoned_record)
            self.assertEqual(storage.read_transcript(), [closed_record, abandoned_record])

            checkpoint = Checkpoint(
                checkpoint_id="chk_0001",
                round_number=2,
                reason="round_close",
                created_at=FIXED_TIME + timedelta(minutes=4),
                status=CheckpointStatus.SUCCESS,
                summary_snapshot_id="sum_0001",
                structured_state_revision_id="state_0001",
            )
            storage.append_checkpoint(checkpoint)
            self.assertEqual(storage.read_checkpoints(), [checkpoint])

            summary = build_summary_snapshot()
            storage.write_summary_snapshot(summary)
            self.assertEqual(storage.load_summary_snapshot(summary.summary_id), summary)
            self.assertEqual(storage.load_current_summary(), summary)

            structured_state = build_structured_state()
            storage.write_structured_state_revision(structured_state)
            self.assertEqual(
                storage.load_structured_state_revision(structured_state.revision_id),
                structured_state,
            )
            self.assertEqual(storage.load_current_structured_state(), structured_state)

            metric = {"event": "checkpoint_duration_ms", "value": 512}
            storage.append_metric(metric)
            self.assertEqual(storage.read_metrics(), [metric])


if __name__ == "__main__":
    unittest.main()
