from __future__ import annotations

import io
import json
import tempfile
import unittest

from deliberation_room.cli import CliShell
from deliberation_room.domain import (
    Agent,
    CompletionResult,
    CompletionStatus,
    RoomStatus,
)
from deliberation_room.provider import KeySource, ProviderModel, ProviderStatus


def completion_success(content: str) -> CompletionResult:
    return CompletionResult(
        content=content,
        token_usage={"input": 10, "output": 10},
        latency_ms=8,
        status=CompletionStatus.SUCCESS,
    )


def completion_error(message: str, *, code: str = "provider_error") -> CompletionResult:
    return CompletionResult(
        content="",
        token_usage={"input": 0, "output": 0},
        latency_ms=4,
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


class FakeProviderLayer:
    def __init__(self, completions: list[CompletionResult]) -> None:
        self._completions = list(completions)
        self._models = [
            ProviderModel(
                provider="openai",
                model_id="gpt-test",
                display_name="GPT Test",
                key_source=KeySource.MANUAL,
            )
        ]

    def discover_keys(self):
        return []

    def list_available_models(self):
        return list(self._models)

    def get_provider_status(self, provider: str):
        registered = provider == "openai"
        return ProviderStatus(
            provider=provider,
            registered=registered,
            key_source=KeySource.MANUAL if registered else None,
            source_name="fake" if registered else None,
            model_count=len(self._models) if registered else 0,
            last_error=None,
        )

    def complete(self, model_id: str, messages, config=None) -> CompletionResult:
        del model_id, messages, config
        if not self._completions:
            raise AssertionError("provider completion queue exhausted")
        return self._completions.pop(0)


def build_agent(display_name: str = "Generalist") -> Agent:
    return Agent(
        participant_id="agent_1",
        display_name=display_name,
        role=display_name,
        system_prompt=f"{display_name} prompt.",
        provider="openai",
        model_id="gpt-test",
    )


class CliShellTests(unittest.TestCase):
    def test_create_room_and_process_round_reveal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stdout = io.StringIO()
            stderr = io.StringIO()
            provider = FakeProviderLayer(
                [
                    completion_success("Propose a thin CLI shell."),
                    completion_success("Summary after round one."),
                    completion_success(
                        structured_state_payload(
                            "Build the CLI shell.",
                            "Keep the shell thin and testable.",
                        )
                    ),
                ]
            )
            shell = CliShell(
                rooms_root=temp_dir,
                stdin=io.StringIO(),
                stdout=stdout,
                stderr=stderr,
            )

            session = shell.create_room(
                name="CLI Room",
                problem_statement="Build the CLI shell.",
                agent=build_agent(),
                provider_layer=provider,
            )
            shell.process_line(session, "How should the CLI work?")

            output = stdout.getvalue()
            self.assertIn("Created room CLI Room (cli-room).", output)
            self.assertIn("Round 1 seed recorded.", output)
            self.assertIn("Waiting on: Generalist", output)
            self.assertIn("Round 1 revealed.", output)
            self.assertIn("Generalist: Propose a thin CLI shell.", output)
            self.assertIn("Checkpoint chk_0001 completed.", output)
            self.assertIn("- current_problem: Build the CLI shell.", output)
            self.assertEqual(stderr.getvalue(), "")

    def test_resume_room_reactivates_archived_room(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = FakeProviderLayer([])
            shell = CliShell(
                rooms_root=temp_dir,
                stdin=io.StringIO(),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
            session = shell.create_room(
                name="Resume Room",
                problem_statement="Resume archived state.",
                agent=build_agent(),
                provider_layer=provider,
            )
            session.protocol.archive_room(reason="test archive")

            resumed_stdout = io.StringIO()
            resumed_shell = CliShell(
                rooms_root=temp_dir,
                stdin=io.StringIO(),
                stdout=resumed_stdout,
                stderr=io.StringIO(),
            )
            resumed = resumed_shell.resume_room("resume-room", provider_layer=FakeProviderLayer([]))

            self.assertEqual(resumed.storage.load_room_state().status, RoomStatus.ACTIVE)
            self.assertIn("Resumed archived room Resume Room (resume-room).", resumed_stdout.getvalue())

    def test_run_session_handles_provider_failure_and_no_available_agents(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stdin = io.StringIO("Seed the round\ncontinue\nend\n")
            stdout = io.StringIO()
            stderr = io.StringIO()
            provider = FakeProviderLayer(
                [
                    completion_error("model unavailable", code="rate_limit"),
                    completion_success("Summary after continue."),
                    completion_success(
                        structured_state_payload(
                            "Handle provider failures.",
                            "Surface a decision prompt and continue only by human choice.",
                        )
                    ),
                ]
            )
            shell = CliShell(
                rooms_root=temp_dir,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
            )
            session = shell.create_room(
                name="Failure Room",
                problem_statement="Handle provider failures.",
                agent=build_agent(),
                provider_layer=provider,
            )

            exit_code = shell.run_session(session)

            self.assertEqual(exit_code, 0)
            output = stdout.getvalue()
            self.assertIn("Generalist completion failed: model unavailable", output)
            self.assertIn("Allowed actions: continue, wait_once, swap_next_checkpoint, archive, end", output)
            self.assertIn("No available agents remain in the room.", output)
            self.assertIn("Room status is now ended.", output)
            self.assertEqual(stderr.getvalue(), "")

    def test_swap_command_replaces_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stdin = io.StringIO("\nSkeptic 2\nSkeptic\n1\n\n")
            stdout = io.StringIO()
            stderr = io.StringIO()
            provider = FakeProviderLayer(
                [
                    completion_success("First round response."),
                    completion_success("Summary after round one."),
                    completion_success(
                        structured_state_payload(
                            "Swap the active agent.",
                            "Allow agent replacement from the CLI.",
                        )
                    ),
                ]
            )
            shell = CliShell(
                rooms_root=temp_dir,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
            )
            session = shell.create_room(
                name="Swap Room",
                problem_statement="Swap the active agent.",
                agent=build_agent(),
                provider_layer=provider,
            )
            shell.process_line(session, "Warm up the room.")
            shell.process_line(session, "/swap agent_1")

            participants = session.storage.load_room_config().participants
            swapped_agent = next(
                participant for participant in participants if isinstance(participant, Agent)
            )
            self.assertEqual(swapped_agent.display_name, "Skeptic 2")
            self.assertEqual(swapped_agent.role, "Skeptic")
            self.assertIn("Swapped Generalist for Skeptic 2.", stdout.getvalue())
            self.assertEqual(stderr.getvalue(), "")

    def test_status_history_edit_clear_and_metrics_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stdout = io.StringIO()
            stderr = io.StringIO()
            provider = FakeProviderLayer(
                [
                    completion_success("Initial response."),
                    completion_success("Summary after round one."),
                    completion_success(
                        structured_state_payload(
                            "Project room state.",
                            "Render shell projections for state and history.",
                        )
                    ),
                ]
            )
            shell = CliShell(
                rooms_root=temp_dir,
                stdin=io.StringIO(),
                stdout=stdout,
                stderr=stderr,
            )
            session = shell.create_room(
                name="Projection Room",
                problem_statement="Project room state.",
                agent=build_agent(),
                provider_layer=provider,
            )
            shell.process_line(session, "Populate state.")
            session.storage.append_metric({"event": "checkpoint", "status": "success"})

            shell.process_line(session, "/status")
            shell.process_line(session, "/history")
            shell.process_line(session, "/edit current_problem Revised problem")
            current_state = session.storage.load_current_structured_state()
            self.assertEqual(current_state.current_problem, "Revised problem")
            shell.process_line(session, "/clear current_problem")
            current_state = session.storage.load_current_structured_state()
            self.assertEqual(current_state.current_problem, "Project room state.")
            shell.process_line(session, "/metrics")

            output = stdout.getvalue()
            self.assertIn("Room: Projection Room (projection-room)", output)
            self.assertIn("Transcript history:", output)
            self.assertIn("Updated current_problem in revision", output)
            self.assertIn("Cleared current_problem in revision", output)
            self.assertIn('{"event": "checkpoint", "status": "success"}', output)
            self.assertEqual(stderr.getvalue(), "")
