from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from deliberation_room.domain import (
    Agent,
    CompletionResult,
    CompletionStatus,
    Participant,
    ParticipantOutcome,
    ParticipantType,
    Room,
    RoomConfig,
    RoomRuntimeState,
    RoomStatus,
    Round,
    RoundStatus,
)
from deliberation_room.memory import MemoryEngine
from deliberation_room.persistence import RoomStorage


FIXED_TIME = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)


class FakeProviderLayer:
    def __init__(self, completions: list[CompletionResult]) -> None:
        self._completions = list(completions)
        self.calls: list[tuple[str, list[dict[str, str]]]] = []

    def complete(self, model_id: str, messages: list[dict[str, str]], config=None) -> CompletionResult:
        self.calls.append((model_id, messages))
        return self._completions.pop(0)

    def list_available_models(self):  # pragma: no cover - not used when explicit model IDs are provided
        return []


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
        system_prompt="Offer broad reasoning.",
        model_id="gpt-test",
        provider="openai",
    )
    config = RoomConfig(
        room_id="room_1",
        name="Memory",
        problem_statement="Track the evolving structured state.",
        created_at=FIXED_TIME,
        participants=[human, agent],
        settings={"checkpoint_interval": 1},
    )
    state = RoomRuntimeState(
        room_id="room_1",
        status=RoomStatus.ACTIVE,
        created_at=FIXED_TIME,
        updated_at=FIXED_TIME,
    )
    return Room(config=config, state=state)


def build_closed_round(round_number: int, seed_text: str, response_text: str) -> Round:
    from deliberation_room.domain import Message

    seed = Message(
        author="human_1",
        content=seed_text,
        timestamp=FIXED_TIME + timedelta(minutes=round_number),
        round_number=round_number,
    )
    response = Message(
        author="agent_1",
        content=response_text,
        timestamp=FIXED_TIME + timedelta(minutes=round_number, seconds=30),
        round_number=round_number,
    )
    return Round(
        round_number=round_number,
        seed_author="human_1",
        seed_message=seed,
        status=RoundStatus.CLOSED,
        responses=[response],
        participant_outcomes={"agent_1": ParticipantOutcome.RESPONDED},
    )


def state_completion_payload(current_problem: str, candidate_description: str) -> str:
    return json.dumps(
        {
            "current_problem": current_problem,
            "candidate_solutions": [
                {
                    "id": "sol_1",
                    "description": candidate_description,
                    "status": "active",
                    "origin": "system",
                }
            ],
            "open_questions": [],
            "decisions": [],
            "disagreements": [],
            "action_items": [],
        }
    )


class MemoryEngineTests(unittest.TestCase):
    def test_run_checkpoint_persists_summary_and_structured_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = RoomStorage(Path(temp_dir) / "room_1")
            storage.initialize_room(build_room())
            provider = FakeProviderLayer(
                [
                    CompletionResult(
                        content="Summary after round one.",
                        token_usage={"input": 10, "output": 20},
                        latency_ms=12,
                        status=CompletionStatus.SUCCESS,
                    ),
                    CompletionResult(
                        content=state_completion_payload(
                            "Track the evolving structured state.",
                            "Use a dedicated memory engine.",
                        ),
                        token_usage={"input": 20, "output": 30},
                        latency_ms=14,
                        status=CompletionStatus.SUCCESS,
                    ),
                ]
            )
            engine = MemoryEngine(storage, provider, summary_model_id="openai:summary", state_model_id="openai:state")
            engine.append_transcript(
                build_closed_round(1, "How should memory work?", "Store transcript, summary, and state.")
            )

            result = engine.run_checkpoint(reason="round_close")

            self.assertEqual(result.checkpoint.status.value, "success")
            self.assertEqual(result.summary_snapshot.content, "Summary after round one.")
            self.assertEqual(result.structured_state.current_problem, "Track the evolving structured state.")
            self.assertEqual(len(storage.read_checkpoints()), 1)
            self.assertEqual(storage.load_current_summary().summary_id, result.summary_snapshot.summary_id)
            self.assertEqual(
                storage.load_current_structured_state().revision_id,
                result.structured_state.revision_id,
            )
            context_payload = engine.get_context_payload()
            self.assertEqual(context_payload.summary, "Summary after round one.")
            self.assertEqual(
                context_payload.structured_state["candidate_solutions"][0]["description"],
                "Use a dedicated memory engine.",
            )
            room_state = storage.load_room_state()
            self.assertEqual(room_state.latest_transcript_round_number, 1)
            self.assertEqual(room_state.latest_checkpoint_id, result.checkpoint.checkpoint_id)
            self.assertEqual(room_state.latest_summary_snapshot_id, result.summary_snapshot.summary_id)
            self.assertEqual(
                room_state.latest_structured_state_revision_id,
                result.structured_state.revision_id,
            )

    def test_run_checkpoint_records_error_without_partial_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = RoomStorage(Path(temp_dir) / "room_1")
            storage.initialize_room(build_room())
            provider = FakeProviderLayer(
                [
                    CompletionResult(
                        content="Summary before failure.",
                        token_usage={"input": 10, "output": 20},
                        latency_ms=12,
                        status=CompletionStatus.SUCCESS,
                    ),
                    CompletionResult(
                        content="",
                        token_usage={"input": 0, "output": 0},
                        latency_ms=9,
                        status=CompletionStatus.ERROR,
                        error_code="parse_failure",
                        error_message="state generation failed",
                    ),
                ]
            )
            engine = MemoryEngine(storage, provider, summary_model_id="openai:summary", state_model_id="openai:state")
            engine.append_transcript(build_closed_round(1, "Seed", "Response"))

            result = engine.run_checkpoint(reason="round_close")

            self.assertEqual(result.checkpoint.status.value, "error")
            self.assertIsNone(result.summary_snapshot)
            self.assertIsNone(result.structured_state)
            self.assertIsNone(storage.load_current_summary())
            self.assertIsNone(storage.load_current_structured_state())
            checkpoints = storage.read_checkpoints()
            self.assertEqual(len(checkpoints), 1)
            self.assertEqual(checkpoints[0].error_code, "parse_failure")
            room_state = storage.load_room_state()
            self.assertEqual(room_state.latest_checkpoint_id, checkpoints[0].checkpoint_id)
            self.assertIsNone(room_state.latest_summary_snapshot_id)
            self.assertIsNone(room_state.latest_structured_state_revision_id)

    def test_run_checkpoint_records_parse_failure_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = RoomStorage(Path(temp_dir) / "room_1")
            storage.initialize_room(build_room())
            provider = FakeProviderLayer(
                [
                    CompletionResult(
                        content="Summary before invalid JSON.",
                        token_usage={"input": 10, "output": 20},
                        latency_ms=12,
                        status=CompletionStatus.SUCCESS,
                    ),
                    CompletionResult(
                        content="not valid json",
                        token_usage={"input": 20, "output": 30},
                        latency_ms=14,
                        status=CompletionStatus.SUCCESS,
                    ),
                ]
            )
            engine = MemoryEngine(storage, provider, summary_model_id="openai:summary", state_model_id="openai:state")
            engine.append_transcript(build_closed_round(1, "Seed", "Response"))

            result = engine.run_checkpoint(reason="round_close")

            self.assertEqual(result.checkpoint.status.value, "error")
            self.assertIsNotNone(result.checkpoint.error_message)
            self.assertIsNone(storage.load_current_summary())
            self.assertIsNone(storage.load_current_structured_state())

    def test_human_edits_and_clears_are_versioned_and_overrides_survive_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = RoomStorage(Path(temp_dir) / "room_1")
            storage.initialize_room(build_room())
            provider = FakeProviderLayer(
                [
                    CompletionResult(
                        content="Summary after round one.",
                        token_usage={"input": 10, "output": 20},
                        latency_ms=12,
                        status=CompletionStatus.SUCCESS,
                    ),
                    CompletionResult(
                        content=state_completion_payload("Base problem", "Initial system proposal"),
                        token_usage={"input": 20, "output": 30},
                        latency_ms=14,
                        status=CompletionStatus.SUCCESS,
                    ),
                    CompletionResult(
                        content="Summary after round two.",
                        token_usage={"input": 11, "output": 21},
                        latency_ms=13,
                        status=CompletionStatus.SUCCESS,
                    ),
                    CompletionResult(
                        content=state_completion_payload("System changed problem", "Updated system proposal"),
                        token_usage={"input": 21, "output": 31},
                        latency_ms=15,
                        status=CompletionStatus.SUCCESS,
                    ),
                ]
            )
            engine = MemoryEngine(storage, provider, summary_model_id="openai:summary", state_model_id="openai:state")
            engine.append_transcript(build_closed_round(1, "Seed 1", "Response 1"))
            first_checkpoint = engine.run_checkpoint(reason="round_close")

            edited_state = engine.apply_human_edit("/current_problem", "Human override", author="human_1")
            self.assertEqual(edited_state.current_problem, "Human override")
            self.assertEqual(edited_state.revision_source.value, "human_edit")
            self.assertEqual(len(edited_state.active_overrides), 1)
            self.assertEqual(edited_state.edit_log[-1].source, "human_edit")

            engine.append_transcript(build_closed_round(2, "Seed 2", "Response 2"))
            second_checkpoint = engine.run_checkpoint(reason="round_close")
            self.assertEqual(second_checkpoint.structured_state.current_problem, "Human override")
            self.assertEqual(len(second_checkpoint.structured_state.active_overrides), 1)
            self.assertEqual(len(second_checkpoint.structured_state.edit_log), 1)

            cleared_state = engine.clear_human_override("/current_problem", author="human_1")
            self.assertEqual(cleared_state.current_problem, "Base problem")
            self.assertEqual(cleared_state.revision_source.value, "human_clear")
            self.assertEqual(cleared_state.active_overrides, [])
            self.assertEqual(cleared_state.edit_log[-1].source, "human_clear")

            history = engine.get_state_history()
            self.assertEqual(len(history), 4)
            self.assertEqual(history[0].revision_id, first_checkpoint.structured_state.revision_id)
            diffs = engine.diff_state_revisions(edited_state.revision_id, cleared_state.revision_id)
            self.assertEqual(diffs["/current_problem"]["from"], "Human override")
            self.assertEqual(diffs["/current_problem"]["to"], "Base problem")


if __name__ == "__main__":
    unittest.main()
