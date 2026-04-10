from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from deliberation_room.domain import (
    Agent,
    CompletionResult,
    CompletionStatus,
    Participant,
    ParticipantType,
    Room,
    RoomConfig,
    RoomRuntimeState,
    RoomStatus,
)
from deliberation_room.memory import MemoryEngine
from deliberation_room.persistence import RoomStorage
from deliberation_room.protocol import (
    CHECKPOINT_REASON_TOPIC_SHIFT,
    DECISION_TYPE_CHECKPOINT_FAILURE,
    DECISION_TYPE_NO_AVAILABLE_AGENTS,
    DECISION_TYPE_PROVIDER_FAILURE,
    PASS_TOKEN,
    ProtocolManager,
)


FIXED_TIME = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)


def completion_success(content: str) -> CompletionResult:
    return CompletionResult(
        content=content,
        token_usage={"input": 10, "output": 15},
        latency_ms=12,
        status=CompletionStatus.SUCCESS,
    )


def completion_error(message: str, *, code: str = "provider_error") -> CompletionResult:
    return CompletionResult(
        content="",
        token_usage={"input": 0, "output": 0},
        latency_ms=9,
        status=CompletionStatus.ERROR,
        error_code=code,
        error_message=message,
    )


def structured_state_payload(current_problem: str, description: str) -> str:
    return json.dumps(
        {
            "current_problem": current_problem,
            "candidate_solutions": [
                {
                    "id": "sol_1",
                    "description": description,
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


class ScriptedProviderLayer:
    def __init__(self, completions: list[CompletionResult]) -> None:
        self._completions = list(completions)
        self.calls: list[tuple[str, list[dict[str, str]]]] = []

    def complete(self, model_id: str, messages: list[dict[str, str]], config=None) -> CompletionResult:
        del config
        self.calls.append((model_id, messages))
        if not self._completions:
            raise AssertionError("provider completion queue exhausted")
        return self._completions.pop(0)

    def list_available_models(self):  # pragma: no cover - not used by these tests
        return []


def build_room(agent_count: int = 1, *, status: RoomStatus = RoomStatus.DRAFT) -> Room:
    human = Participant(
        participant_id="human_1",
        display_name="Human",
        participant_type=ParticipantType.HUMAN,
    )
    agents = [
        Agent(
            participant_id=f"agent_{index}",
            display_name=f"Agent {index}",
            role=f"Role {index}",
            system_prompt=f"Agent {index} system prompt.",
            model_id=f"gpt-agent-{index}",
            provider="openai",
        )
        for index in range(1, agent_count + 1)
    ]
    config = RoomConfig(
        room_id="room_1",
        name="Protocol",
        problem_statement="Run a structured deliberation round.",
        created_at=FIXED_TIME,
        participants=[human, *agents],
        settings={"checkpoint_interval": 1},
    )
    state = RoomRuntimeState(
        room_id="room_1",
        status=status,
        created_at=FIXED_TIME,
        updated_at=FIXED_TIME,
    )
    return Room(config=config, state=state)


def build_protocol(
    temp_dir: str,
    *,
    completions: list[CompletionResult],
    agent_count: int = 1,
    room_status: RoomStatus = RoomStatus.DRAFT,
) -> tuple[RoomStorage, ScriptedProviderLayer, ProtocolManager]:
    storage = RoomStorage(Path(temp_dir) / "room_1")
    storage.initialize_room(build_room(agent_count=agent_count, status=room_status))
    provider = ScriptedProviderLayer(completions)
    memory = MemoryEngine(
        storage,
        provider,
        summary_model_id="openai:summary",
        state_model_id="openai:state",
    )
    protocol = ProtocolManager(storage, memory, provider)
    return storage, provider, protocol


class ProtocolManagerTests(unittest.TestCase):
    def test_start_round_runs_complete_round_and_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage, provider, protocol = build_protocol(
                temp_dir,
                agent_count=2,
                completions=[
                    completion_success("First agent response."),
                    completion_success(PASS_TOKEN),
                    completion_success("Summary after the round."),
                    completion_success(
                        structured_state_payload(
                            "Run a structured deliberation round.",
                            "Keep protocol logic separate from memory logic.",
                        )
                    ),
                ],
            )

            result = protocol.start_round("How should phase 4 work?", "human_1")

            self.assertEqual(result.round.status.value, "settled")
            self.assertEqual(result.room_state.status, RoomStatus.ACTIVE)
            transcript = storage.read_transcript()
            self.assertEqual(len(transcript), 1)
            self.assertEqual(transcript[0].round_exit_status.value, "closed")
            self.assertEqual(set(transcript[0].participant_outcomes), {"agent_1", "agent_2"})
            self.assertEqual(transcript[0].participant_outcomes["agent_1"].value, "responded")
            self.assertEqual(transcript[0].participant_outcomes["agent_2"].value, "passed")
            self.assertEqual(len(transcript[0].responses), 1)
            self.assertEqual(storage.load_room_state().current_round, None)
            self.assertEqual(storage.read_checkpoints()[-1].status.value, "success")
            self.assertEqual(provider.calls[0][0], "openai:gpt-agent-1")
            self.assertEqual(provider.calls[1][0], "openai:gpt-agent-2")

    def test_get_room_status_hides_open_round_responses(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _, _, protocol = build_protocol(temp_dir, agent_count=2, completions=[])

            protocol.start_round("Seed", "human_1", run_agents=False)
            protocol.submit_response("agent_1", "Hidden until reveal.")
            with self.assertRaises(ValueError):
                protocol.submit_response("agent_1", "Second answer")

            status = protocol.get_room_status()

            self.assertEqual(status["status"], "active")
            self.assertEqual(status["current_round"]["response_count"], 1)
            self.assertNotIn("responses", status["current_round"])
            self.assertEqual(status["current_round"]["participant_outcomes"]["agent_1"], "responded")
            self.assertEqual(status["current_round"]["participant_outcomes"]["agent_2"], "pending")

    def test_start_round_requires_human_seed_and_blocks_overlapping_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _, _, protocol = build_protocol(temp_dir, agent_count=1, completions=[])

            with self.assertRaises(ValueError):
                protocol.start_round("Seed", "agent_1", run_agents=False)

            protocol.start_round("Seed", "human_1", run_agents=False)
            with self.assertRaises(ValueError):
                protocol.start_round("Another seed", "human_1", run_agents=False)

    def test_provider_failure_continue_marks_unavailable_and_resumes_remaining_agents(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage, _, protocol = build_protocol(
                temp_dir,
                agent_count=2,
                completions=[
                    completion_error("agent one failed", code="rate_limit"),
                    completion_success("Agent two responds after continue."),
                    completion_success("Summary after continue."),
                    completion_success(
                        structured_state_payload(
                            "Continue after provider failure.",
                            "Allow the round to finish after explicit human approval.",
                        )
                    ),
                ],
            )

            initial = protocol.start_round("Seed", "human_1")
            self.assertEqual(initial.room_state.status, RoomStatus.AWAITING_HUMAN_DECISION)
            self.assertEqual(
                initial.room_state.pending_human_decision.decision_type,
                DECISION_TYPE_PROVIDER_FAILURE,
            )

            resolved = protocol.resolve_provider_failure("agent_1", "continue")

            self.assertEqual(resolved.room_state.status, RoomStatus.ACTIVE)
            transcript = storage.read_transcript()
            self.assertEqual(len(transcript), 1)
            self.assertEqual(transcript[0].participant_outcomes["agent_1"].value, "unavailable")
            self.assertEqual(transcript[0].participant_outcomes["agent_2"].value, "responded")

    def test_wait_once_retries_same_agent_and_settles_round(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage, _, protocol = build_protocol(
                temp_dir,
                completions=[
                    completion_error("temporary failure", code="server_error"),
                    completion_success("Recovered after wait once."),
                    completion_success("Summary after retry."),
                    completion_success(
                        structured_state_payload(
                            "Retry a failed participant once.",
                            "Allow a single additional retry cycle.",
                        )
                    ),
                ],
            )

            initial = protocol.start_round("Seed", "human_1")
            self.assertEqual(initial.room_state.status, RoomStatus.AWAITING_HUMAN_DECISION)

            resolved = protocol.resolve_provider_failure("agent_1", "wait_once")

            self.assertEqual(resolved.room_state.status, RoomStatus.ACTIVE)
            transcript = storage.read_transcript()
            self.assertEqual(transcript[0].participant_outcomes["agent_1"].value, "responded")
            self.assertEqual(transcript[0].responses[0].content, "Recovered after wait once.")

    def test_wait_once_failure_removes_wait_once_from_allowed_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _, _, protocol = build_protocol(
                temp_dir,
                completions=[
                    completion_error("temporary failure", code="server_error"),
                    completion_error("still failing", code="server_error"),
                ],
            )

            protocol.start_round("Seed", "human_1")
            result = protocol.resolve_provider_failure("agent_1", "wait_once")

            self.assertEqual(result.room_state.status, RoomStatus.AWAITING_HUMAN_DECISION)
            self.assertEqual(
                result.room_state.pending_human_decision.allowed_actions,
                ["continue", "swap_next_checkpoint", "archive", "end"],
            )

    def test_swap_next_checkpoint_queues_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage, _, protocol = build_protocol(
                temp_dir,
                completions=[
                    completion_error("provider down", code="provider_error"),
                    completion_success("Summary after queueing swap."),
                    completion_success(
                        structured_state_payload(
                            "Queue an agent swap at checkpoint time.",
                            "Keep the room active when a swap is queued.",
                        )
                    ),
                ],
            )

            protocol.start_round("Seed", "human_1")
            result = protocol.resolve_provider_failure("agent_1", "swap_next_checkpoint")

            self.assertEqual(result.room_state.status, RoomStatus.ACTIVE)
            self.assertIsNotNone(result.room_state.queued_agent_swap)
            self.assertEqual(
                result.room_state.queued_agent_swap["participant_id"],
                "agent_1",
            )
            self.assertEqual(storage.read_transcript()[0].participant_outcomes["agent_1"].value, "unavailable")

    def test_checkpoint_failure_can_be_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage, _, protocol = build_protocol(
                temp_dir,
                completions=[
                    completion_success("Agent response."),
                    completion_success("Summary before failure."),
                    completion_error("state generation failed", code="parse_failure"),
                    completion_success("Summary after retry."),
                    completion_success(
                        structured_state_payload(
                            "Retry failed checkpoints.",
                            "Allow a human-triggered rerun of the unresolved window.",
                        )
                    ),
                ],
            )

            result = protocol.start_round("Seed", "human_1")
            self.assertEqual(result.room_state.status, RoomStatus.AWAITING_HUMAN_DECISION)
            self.assertEqual(
                result.room_state.pending_human_decision.decision_type,
                DECISION_TYPE_CHECKPOINT_FAILURE,
            )

            retried = protocol.resolve_checkpoint_failure("retry_checkpoint")

            self.assertEqual(retried.room_state.status, RoomStatus.ACTIVE)
            checkpoints = storage.read_checkpoints()
            self.assertEqual(len(checkpoints), 2)
            self.assertEqual(checkpoints[0].status.value, "error")
            self.assertEqual(checkpoints[1].status.value, "success")

    def test_archive_and_resume_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage, _, protocol = build_protocol(temp_dir, completions=[])

            protocol.start_round("Seed", "human_1", run_agents=False)
            archived = protocol.archive_room(reason="manual archive")

            self.assertEqual(archived.room_state.status, RoomStatus.ARCHIVED)
            transcript = storage.read_transcript()
            self.assertEqual(len(transcript), 1)
            self.assertEqual(transcript[0].round_exit_status.value, "abandoned")
            self.assertIsNone(storage.load_room_state().current_round)

            resumed = protocol.resume_room()

            self.assertEqual(resumed.room_state.status, RoomStatus.ACTIVE)
            self.assertIsNone(resumed.room_state.current_round)

    def test_no_available_agents_after_checkpoint_pauses_room(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _, _, protocol = build_protocol(
                temp_dir,
                completions=[
                    completion_error("provider down", code="provider_error"),
                    completion_success("Summary after everyone is unavailable."),
                    completion_success(
                        structured_state_payload(
                            "No agents remained available for the room.",
                            "Pause the room for archive-or-end human resolution.",
                        )
                    ),
                ],
            )

            protocol.start_round("Seed", "human_1")
            result = protocol.resolve_provider_failure("agent_1", "continue")

            self.assertEqual(result.room_state.status, RoomStatus.AWAITING_HUMAN_DECISION)
            self.assertEqual(
                result.room_state.pending_human_decision.decision_type,
                DECISION_TYPE_NO_AVAILABLE_AGENTS,
            )

    def test_add_and_remove_participants_updates_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage, _, protocol = build_protocol(
                temp_dir,
                completions=[],
                room_status=RoomStatus.ACTIVE,
            )

            extra_agent = Agent(
                participant_id="agent_2",
                display_name="Agent 2",
                role="Role 2",
                system_prompt="Second agent.",
                model_id="gpt-agent-2",
                provider="openai",
            )
            updated = protocol.add_participant(extra_agent)
            self.assertEqual(len(updated.participants), 3)

            removed = protocol.remove_participant("agent_2")
            self.assertEqual(len(removed.participants), 2)

            with self.assertRaises(ValueError):
                protocol.remove_participant("agent_1")
            self.assertEqual(len(storage.load_room_config().participants), 2)

    def test_topic_shift_checkpoint_can_be_requested_between_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage, _, protocol = build_protocol(
                temp_dir,
                completions=[
                    completion_success("Initial response."),
                    completion_success("Initial summary."),
                    completion_success(
                        structured_state_payload(
                            "Base state after the first round.",
                            "Initial structured proposal.",
                        )
                    ),
                    completion_success("Summary after topic shift."),
                    completion_success(
                        structured_state_payload(
                            "State after topic shift.",
                            "Refresh context after a topic change.",
                        )
                    ),
                ],
            )

            protocol.start_round("Seed", "human_1")
            requested = protocol.request_topic_shift_checkpoint()

            self.assertEqual(requested.room_state.status, RoomStatus.ACTIVE)
            checkpoints = storage.read_checkpoints()
            self.assertEqual(len(checkpoints), 2)
            self.assertEqual(checkpoints[-1].reason, CHECKPOINT_REASON_TOPIC_SHIFT)


if __name__ == "__main__":
    unittest.main()
