"""CLI shell for running a Deliberation Room session."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence, TextIO

from .domain import (
    Agent,
    JSONValue,
    Participant,
    ParticipantType,
    Room,
    RoomConfig,
    RoomRuntimeState,
    RoomStatus,
    Round,
    RoundStatus,
    utc_now,
)
from .memory import MemoryEngine
from .persistence import RoomStorage
from .protocol import (
    DECISION_TYPE_CHECKPOINT_FAILURE,
    DECISION_TYPE_NO_AVAILABLE_AGENTS,
    DECISION_TYPE_PROVIDER_FAILURE,
    ProtocolActionResult,
    ProtocolManager,
)
from .provider import DEFAULT_ADAPTERS, ProviderLayer, ProviderModel


ROOMS_ROOT = Path("rooms")
SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class AgentTemplate:
    key: str
    display_name: str
    role: str
    system_prompt: str


DEFAULT_AGENT_TEMPLATES: tuple[AgentTemplate, ...] = (
    AgentTemplate(
        key="generalist",
        display_name="Generalist",
        role="Generalist",
        system_prompt="Offer broad reasoning. Surface plausible options and tradeoffs.",
    ),
    AgentTemplate(
        key="skeptic",
        display_name="Skeptic",
        role="Skeptic",
        system_prompt="Stress test assumptions. Identify hidden risks and weak evidence.",
    ),
    AgentTemplate(
        key="synthesizer",
        display_name="Synthesizer",
        role="Synthesizer",
        system_prompt="Integrate the room state into a crisp synthesis with next actions.",
    ),
)


@dataclass(slots=True)
class ShellSession:
    room_root: Path
    storage: RoomStorage
    provider_layer: ProviderLayer
    memory_engine: MemoryEngine
    protocol: ProtocolManager
    human_participant_id: str


class CliShell:
    """Thin interactive shell layered over the room engine."""

    def __init__(
        self,
        *,
        rooms_root: str | Path = ROOMS_ROOT,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        provider_layer_factory: Callable[[], ProviderLayer] | None = None,
    ) -> None:
        self.rooms_root = Path(rooms_root)
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stdout = stdout if stdout is not None else sys.stdout
        self.stderr = stderr if stderr is not None else sys.stderr
        self.provider_layer_factory = provider_layer_factory or ProviderLayer

    def run(self, argv: Sequence[str] | None = None) -> int:
        parser = self._build_parser()
        args = parser.parse_args(list(argv) if argv is not None else None)
        self.rooms_root = Path(args.rooms_root)

        if args.command == "list-archived":
            self._render_archived_rooms()
            return 0

        try:
            if args.command == "new":
                session = self.create_room(
                    name=args.name,
                    problem_statement=args.problem,
                    human_name=args.human_name,
                    checkpoint_interval=args.checkpoint_interval,
                    agent=self._agent_from_args(args) if self._args_define_agent(args) else None,
                )
            elif args.command == "resume":
                session = self.resume_room(args.room_id)
            else:
                session = self.launch()
        except (EOFError, FileNotFoundError, ValueError, KeyError) as exc:
            self._error(str(exc))
            return 1

        return self.run_session(session)

    def launch(self) -> ShellSession:
        archived_rooms = self.list_archived_rooms()
        if archived_rooms:
            self._write("Archived rooms:")
            for room_id in archived_rooms:
                self._write(f"- {room_id}")
            selected = self._readline(
                "Enter an archived room id to resume, or press Enter to create a new room: "
            )
            if selected:
                return self.resume_room(selected.strip())
        return self.create_room()

    def create_room(
        self,
        *,
        name: str | None = None,
        problem_statement: str | None = None,
        human_name: str = "Human",
        checkpoint_interval: int = 1,
        agent: Agent | None = None,
        provider_layer: ProviderLayer | None = None,
    ) -> ShellSession:
        provider = provider_layer or self.provider_layer_factory()
        self._discover_and_render_provider_status(provider)

        models = list(provider.list_available_models())
        if not models:
            raise ValueError("no models are available; export a provider API key before creating a room")

        room_name = name or self._require_input("Room name: ")
        problem = problem_statement or self._require_input("Problem statement: ")
        selected_agents = [agent] if agent is not None else self._prompt_for_agents(models)

        room_id = self._next_room_id(room_name)
        room_root = self.rooms_root / room_id
        storage = RoomStorage(room_root)
        room = Room(
            config=RoomConfig(
                room_id=room_id,
                name=room_name,
                problem_statement=problem,
                created_at=utc_now(),
                participants=[
                    Participant(
                        participant_id="human_1",
                        display_name=human_name,
                        participant_type=ParticipantType.HUMAN,
                    ),
                    *selected_agents,
                ],
                settings={"checkpoint_interval": max(1, checkpoint_interval)},
            ),
            state=RoomRuntimeState(
                room_id=room_id,
                status=RoomStatus.DRAFT,
                created_at=utc_now(),
                updated_at=utc_now(),
            ),
        )
        storage.initialize_room(room)

        checkpoint_model_id = self._catalog_id(models[0])
        session = ShellSession(
            room_root=room_root,
            storage=storage,
            provider_layer=provider,
            memory_engine=MemoryEngine(
                storage,
                provider,
                summary_model_id=checkpoint_model_id,
                state_model_id=checkpoint_model_id,
            ),
            protocol=None,  # type: ignore[arg-type]
            human_participant_id="human_1",
        )
        session.protocol = ProtocolManager(storage, session.memory_engine, provider)

        self._write(f"Created room {room_name} ({room_id}).")
        self._render_participants(storage.load_room_config().participants)
        return session

    def resume_room(
        self,
        room_id: str | None = None,
        *,
        provider_layer: ProviderLayer | None = None,
    ) -> ShellSession:
        resolved_room_id = room_id or self._prompt_for_archived_room()
        room_root = self.rooms_root / resolved_room_id
        storage = RoomStorage(room_root)
        config = storage.load_room_config()
        state = storage.load_room_state()

        provider = provider_layer or self.provider_layer_factory()
        self._discover_and_render_provider_status(provider)
        models = list(provider.list_available_models())
        checkpoint_model_id = self._catalog_id(models[0]) if models else None
        memory_engine = MemoryEngine(
            storage,
            provider,
            summary_model_id=checkpoint_model_id,
            state_model_id=checkpoint_model_id,
        )
        protocol = ProtocolManager(storage, memory_engine, provider)

        if state.status is RoomStatus.ARCHIVED:
            protocol.resume_room()
            self._write(f"Resumed archived room {config.name} ({config.room_id}).")
        elif state.status is RoomStatus.ENDED:
            raise ValueError("ended rooms cannot be resumed")
        else:
            self._write(f"Opened room {config.name} ({config.room_id}).")

        return ShellSession(
            room_root=room_root,
            storage=storage,
            provider_layer=provider,
            memory_engine=memory_engine,
            protocol=protocol,
            human_participant_id=self._human_participant_id(config),
        )

    def run_session(self, session: ShellSession) -> int:
        config = session.storage.load_room_config()
        self._write(f"Room: {config.name}")
        self._write(f"Problem: {config.problem_statement}")
        self._render_structured_state_panel(session)
        self._render_help()

        while True:
            state = session.storage.load_room_state()
            if state.status in {RoomStatus.ARCHIVED, RoomStatus.ENDED}:
                self._write(f"Room status is now {state.status.value}.")
                return 0

            if state.pending_human_decision is not None:
                if not self._resolve_pending_decision(session):
                    return 0
                continue

            if state.queued_agent_swap is not None and state.current_round is None:
                if not self._resolve_queued_agent_swap(session):
                    return 0
                continue

            prompt = "seed> " if state.current_round is None else "room> "
            line = self._readline(prompt)
            if line is None:
                session.protocol.end_room(reason="human quit")
                self._write("Room ended.")
                return 0
            stripped = line.strip()
            if not stripped:
                continue
            if not self.process_line(session, stripped):
                return 0

    def process_line(self, session: ShellSession, line: str) -> bool:
        try:
            if line.startswith("/"):
                return self._process_command(session, line)
            return self._process_seed(session, line)
        except EOFError:
            session.protocol.end_room(reason="human quit")
            self._write("Room ended.")
            return False
        except (ValueError, KeyError, FileNotFoundError) as exc:
            self._error(str(exc))
            return True

    def list_archived_rooms(self) -> list[str]:
        if not self.rooms_root.exists():
            return []
        archived: list[str] = []
        for candidate in sorted(self.rooms_root.iterdir()):
            if not candidate.is_dir():
                continue
            try:
                storage = RoomStorage(candidate)
                if storage.load_room_state().status is RoomStatus.ARCHIVED:
                    archived.append(candidate.name)
            except FileNotFoundError:
                continue
        return archived

    def _build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog="deliberation-room")
        parser.add_argument("--rooms-root", default=str(self.rooms_root))
        subparsers = parser.add_subparsers(dest="command")

        new_parser = subparsers.add_parser("new")
        new_parser.add_argument("--name")
        new_parser.add_argument("--problem")
        new_parser.add_argument("--human-name", default="Human")
        new_parser.add_argument("--checkpoint-interval", type=int, default=1)
        new_parser.add_argument("--participant-id")
        new_parser.add_argument("--display-name")
        new_parser.add_argument("--role")
        new_parser.add_argument("--model")
        new_parser.add_argument("--system-prompt")

        resume_parser = subparsers.add_parser("resume")
        resume_parser.add_argument("room_id", nargs="?")

        subparsers.add_parser("list-archived")
        return parser

    def _args_define_agent(self, args: argparse.Namespace) -> bool:
        return any(
            getattr(args, name) is not None
            for name in ("participant_id", "display_name", "role", "model", "system_prompt")
        )

    def _agent_from_args(self, args: argparse.Namespace) -> Agent:
        provider = self.provider_layer_factory()
        provider.discover_keys()
        models = list(provider.list_available_models())
        if not models:
            raise ValueError("no models are available for agent selection")

        template = self._template_for_role(args.role or "generalist")
        model = self._select_model(models, args.model, default=models[0])
        return Agent(
            participant_id=args.participant_id or "agent_1",
            display_name=args.display_name or template.display_name,
            role=args.role or template.role,
            system_prompt=args.system_prompt or template.system_prompt,
            provider=model.provider,
            model_id=model.model_id,
        )

    def _process_command(self, session: ShellSession, line: str) -> bool:
        command, _, remainder = line.partition(" ")
        remainder = remainder.strip()

        if command == "/help":
            self._render_help()
            return True
        if command == "/status":
            self._render_status(session)
            return True
        if command == "/history":
            self._render_history(session, query=remainder or None)
            return True
        if command == "/metrics":
            self._render_metrics(session)
            return True
        if command == "/checkpoint":
            result = session.protocol.request_compaction()
            self._render_action_result(session, result)
            return True
        if command == "/edit":
            field_path, value_text = self._split_edit_args(remainder)
            field_path = self._normalize_field_path(field_path)
            field_label = self._display_field_path(field_path)
            if value_text is None:
                value_text = self._require_input(f"New value for {field_label}: ")
            value = self._parse_value(value_text)
            state = session.memory_engine.apply_human_edit(
                field_path,
                value,
                session.human_participant_id,
            )
            self._write(f"Updated {field_label} in revision {state.revision_id}.")
            self._render_structured_state_panel(session)
            return True
        if command == "/clear":
            field_path = self._normalize_field_path(
                remainder or self._require_input("Field path to clear: ")
            )
            field_label = self._display_field_path(field_path)
            state = session.memory_engine.clear_human_override(
                field_path,
                session.human_participant_id,
            )
            self._write(f"Cleared {field_label} in revision {state.revision_id}.")
            self._render_structured_state_panel(session)
            return True
        if command == "/swap":
            participant_id = remainder or self._require_input("Agent participant id to swap: ")
            self._swap_agent(session, participant_id)
            return True
        if command in {"/archive", "/end", "/quit"}:
            action = "archive" if command == "/archive" else "end"
            result = (
                session.protocol.archive_room(reason="human request")
                if action == "archive"
                else session.protocol.end_room(reason="human quit")
            )
            self._render_action_result(session, result)
            return False

        raise ValueError(f"unknown command '{command}'")

    def _process_seed(self, session: ShellSession, seed_message: str) -> bool:
        state = session.storage.load_room_state()
        if state.current_round is not None:
            raise ValueError("cannot start a new round while another round is open")

        round_number = (state.latest_transcript_round_number or 0) + 1
        waiting_on = ", ".join(self._agent_display_names(session))
        self._write(f"Round {round_number} seed recorded.")
        self._write(f"Waiting on: {waiting_on}")

        result = session.protocol.start_round(seed_message, session.human_participant_id)
        self._render_action_result(session, result)
        return True

    def _render_action_result(
        self,
        session: ShellSession,
        result: ProtocolActionResult,
    ) -> None:
        if result.round is not None and result.round.status in {RoundStatus.CLOSED, RoundStatus.SETTLED}:
            self._render_round_reveal(session, result.round)
        if result.checkpoint_result is not None:
            checkpoint = result.checkpoint_result.checkpoint
            if checkpoint.status.value == "success":
                self._write(f"Checkpoint {checkpoint.checkpoint_id} completed.")
            else:
                self._write(
                    f"Checkpoint {checkpoint.checkpoint_id} failed: {checkpoint.error_message or checkpoint.error_code}"
                )
        if result.room_state.pending_human_decision is not None:
            self._render_pending_decision_notice(session)
        elif result.room_state.status is RoomStatus.ACTIVE and result.round is not None:
            self._render_structured_state_panel(session)

    def _resolve_pending_decision(self, session: ShellSession) -> bool:
        state = session.storage.load_room_state()
        pending = state.pending_human_decision
        if pending is None:
            return True

        self._render_pending_decision_notice(session)
        action = self._prompt_choice(pending.allowed_actions)
        if action is None:
            session.protocol.end_room(reason="human quit")
            self._write("Room ended.")
            return False

        if pending.decision_type == DECISION_TYPE_PROVIDER_FAILURE:
            result = session.protocol.resolve_provider_failure(pending.participant_id or "", action)
        elif pending.decision_type == DECISION_TYPE_CHECKPOINT_FAILURE:
            result = session.protocol.resolve_checkpoint_failure(action)
        elif pending.decision_type == DECISION_TYPE_NO_AVAILABLE_AGENTS:
            result = session.protocol.resolve_no_available_agents(action)
        else:  # pragma: no cover - defensive
            raise ValueError(f"unsupported pending decision type '{pending.decision_type}'")

        self._render_action_result(session, result)
        return True

    def _resolve_queued_agent_swap(self, session: ShellSession) -> bool:
        state = session.storage.load_room_state()
        queued_swap = state.queued_agent_swap or {}
        participant_id = queued_swap.get("participant_id")
        if participant_id is None:
            return True
        self._write(f"Queued swap pending for {participant_id}.")
        try:
            self._swap_agent(session, participant_id)
        except EOFError:
            session.protocol.end_room(reason="human quit")
            self._write("Room ended.")
            return False
        return True

    def _swap_agent(self, session: ShellSession, participant_id: str) -> None:
        current_agent = self._find_agent(session, participant_id)
        replacement = self._prompt_for_replacement_agent(session, current_agent)
        config = session.protocol.swap_agent(participant_id, replacement)
        self._write(f"Swapped {current_agent.display_name} for {replacement.display_name}.")
        self._render_participants(config.participants)

    def _prompt_for_agents(self, models: Sequence[ProviderModel]) -> list[Agent]:
        default_agent = self._agent_from_template(DEFAULT_AGENT_TEMPLATES[0], models[0], 1)
        use_default = self._readline(
            f"Use default agent {default_agent.display_name} ({self._catalog_id(models[0])})? [Y/n]: "
        )
        if use_default is None:
            raise EOFError
        if use_default.strip().lower() in {"", "y", "yes"}:
            return [default_agent]

        agents: list[Agent] = []
        while True:
            agents.append(self._prompt_for_new_agent(models, len(agents) + 1))
            add_more = self._readline("Add another agent? [y/N]: ")
            if add_more is None or add_more.strip().lower() not in {"y", "yes"}:
                break
        if not agents:
            raise ValueError("at least one agent is required")
        return agents

    def _prompt_for_new_agent(
        self,
        models: Sequence[ProviderModel],
        index: int,
    ) -> Agent:
        template = self._template_for_role(
            self._readline("Agent role [generalist/skeptic/synthesizer]: ") or "generalist"
        )
        participant_id = self._readline(f"Participant id [agent_{index}]: ") or f"agent_{index}"
        display_name = self._readline(f"Display name [{template.display_name}]: ") or template.display_name
        model = self._prompt_for_model(models, default=models[0])
        system_prompt = self._readline("System prompt [press Enter to use the role default]: ")
        return Agent(
            participant_id=participant_id,
            display_name=display_name,
            role=template.role,
            system_prompt=system_prompt or template.system_prompt,
            provider=model.provider,
            model_id=model.model_id,
        )

    def _prompt_for_replacement_agent(self, session: ShellSession, current_agent: Agent) -> Agent:
        models = list(session.provider_layer.list_available_models())
        if not models:
            raise ValueError("no models are available for agent swap")

        self._write("Available models:")
        for index, model in enumerate(models, start=1):
            self._write(f"{index}. {self._catalog_id(model)}")

        participant_id = self._readline(
            f"Replacement participant id [{current_agent.participant_id}]: "
        )
        if participant_id is None:
            raise EOFError
        display_name = self._readline(
            f"Replacement display name [{current_agent.display_name}]: "
        )
        if display_name is None:
            raise EOFError
        role = self._readline(f"Replacement role [{current_agent.role}]: ")
        if role is None:
            raise EOFError
        model = self._prompt_for_model(
            models,
            default=self._match_existing_model(models, current_agent),
        )
        system_prompt = self._readline("Replacement system prompt [press Enter to keep the current prompt]: ")
        if system_prompt is None:
            raise EOFError
        return Agent(
            participant_id=participant_id.strip() or current_agent.participant_id,
            display_name=display_name.strip() or current_agent.display_name,
            role=role.strip() or current_agent.role,
            system_prompt=system_prompt or current_agent.system_prompt,
            provider=model.provider,
            model_id=model.model_id,
        )

    def _match_existing_model(
        self,
        models: Sequence[ProviderModel],
        current_agent: Agent,
    ) -> ProviderModel:
        for model in models:
            if model.provider == current_agent.provider and model.model_id == current_agent.model_id:
                return model
        return models[0]

    def _prompt_for_model(
        self,
        models: Sequence[ProviderModel],
        *,
        default: ProviderModel,
    ) -> ProviderModel:
        self._write("Available models:")
        for index, model in enumerate(models, start=1):
            self._write(f"{index}. {self._catalog_id(model)}")

        while True:
            selection = self._readline(f"Model [{self._catalog_id(default)}]: ")
            if selection is None:
                raise EOFError
            selected = selection.strip()
            if not selected:
                return default
            try:
                return self._select_model(models, selected, default=default)
            except ValueError as exc:
                self._error(str(exc))

    def _select_model(
        self,
        models: Sequence[ProviderModel],
        selection: str | None,
        *,
        default: ProviderModel,
    ) -> ProviderModel:
        if selection is None or not selection.strip():
            return default
        selected = selection.strip()
        if selected.isdigit():
            index = int(selected) - 1
            if 0 <= index < len(models):
                return models[index]
            raise ValueError(f"model selection '{selected}' is out of range")

        for model in models:
            if selected == self._catalog_id(model):
                return model
        matching = [model for model in models if model.model_id == selected]
        if len(matching) == 1:
            return matching[0]
        if len(matching) > 1:
            raise ValueError(f"model '{selected}' is ambiguous across providers")
        raise ValueError(f"unknown model '{selected}'")

    def _template_for_role(self, value: str) -> AgentTemplate:
        normalized = value.strip().lower()
        for template in DEFAULT_AGENT_TEMPLATES:
            if normalized in {template.key, template.role.lower()}:
                return template
        raise ValueError(f"unknown role '{value}'")

    def _agent_from_template(
        self,
        template: AgentTemplate,
        model: ProviderModel,
        index: int,
    ) -> Agent:
        return Agent(
            participant_id=f"agent_{index}",
            display_name=template.display_name,
            role=template.role,
            system_prompt=template.system_prompt,
            provider=model.provider,
            model_id=model.model_id,
        )

    def _render_status(self, session: ShellSession) -> None:
        config = session.storage.load_room_config()
        status = session.protocol.get_room_status()
        self._write(f"Room: {config.name} ({config.room_id})")
        self._write(f"Status: {status['status']}")
        self._render_participants(config.participants)
        current_round = status["current_round"]
        if current_round is None:
            self._write("Current round: none")
        else:
            self._write(
                f"Current round: {current_round['round_number']} ({current_round['status']})"
            )
            for participant_id, outcome in current_round["participant_outcomes"].items():
                self._write(f"- {participant_id}: {outcome}")
        pending = status["pending_human_decision"]
        if pending is not None:
            self._write(
                f"Pending decision: {pending['type']} [{'/'.join(pending['allowed_actions'])}]"
            )
        self._render_structured_state_panel(session)

    def _render_history(self, session: ShellSession, *, query: str | None) -> None:
        transcript = session.memory_engine.get_transcript(query)
        checkpoints = session.storage.read_checkpoints()
        revisions = session.memory_engine.get_state_history()

        if not transcript:
            self._write("Transcript history: none")
        else:
            self._write("Transcript history:")
            for record in transcript:
                self._write(
                    f"- round {record.round_number} [{record.round_exit_status.value}] {record.seed_message.content}"
                )
        if not checkpoints:
            self._write("Checkpoint history: none")
        else:
            self._write("Checkpoint history:")
            for checkpoint in checkpoints:
                self._write(
                    f"- {checkpoint.checkpoint_id} round {checkpoint.round_number} {checkpoint.status.value} ({checkpoint.reason})"
                )
        if not revisions:
            self._write("Structured state history: none")
        else:
            self._write("Structured state history:")
            for revision in revisions:
                self._write(
                    f"- {revision.revision_id} {revision.revision_source.value} by {revision.updated_by}"
                )

    def _render_metrics(self, session: ShellSession) -> None:
        metrics = session.storage.read_metrics()
        if not metrics:
            self._write("Metrics: none recorded")
            return
        self._write("Metrics:")
        for metric in metrics:
            self._write(f"- {json.dumps(metric, sort_keys=True)}")

    def _render_round_reveal(self, session: ShellSession, round_data: Round) -> None:
        labels = self._participant_labels(session)
        self._write(f"Round {round_data.round_number} revealed.")
        self._write(
            f"Seed [{labels.get(round_data.seed_author, round_data.seed_author)}]: {round_data.seed_message.content}"
        )
        for response in round_data.responses:
            self._write(f"- {labels.get(response.author, response.author)}: {response.content}")
        for participant_id, outcome in round_data.participant_outcomes.items():
            if outcome.value == "responded":
                continue
            self._write(
                f"- {labels.get(participant_id, participant_id)}: {outcome.value}"
            )

    def _render_structured_state_panel(self, session: ShellSession) -> None:
        payload = session.memory_engine.get_context_payload()
        if payload.structured_state is None:
            self._write("Structured state: not initialized")
            return
        state = payload.structured_state
        self._write("Structured state:")
        self._write(f"- current_problem: {state.get('current_problem', '')}")
        self._write(
            f"- candidate_solutions: {len(state.get('candidate_solutions', []))}"
        )
        self._write(f"- open_questions: {len(state.get('open_questions', []))}")
        self._write(f"- decisions: {len(state.get('decisions', []))}")
        self._write(f"- disagreements: {len(state.get('disagreements', []))}")
        self._write(f"- action_items: {len(state.get('action_items', []))}")
        active_overrides = state.get("active_overrides", [])
        if isinstance(active_overrides, list):
            self._write(f"- active_overrides: {len(active_overrides)}")

    def _render_pending_decision_notice(self, session: ShellSession) -> None:
        pending = session.storage.load_room_state().pending_human_decision
        if pending is None:
            return

        labels = self._participant_labels(session)
        if pending.decision_type == DECISION_TYPE_PROVIDER_FAILURE:
            label = labels.get(pending.participant_id or "", pending.participant_id or "agent")
            self._write(
                f"{label} completion failed: {pending.error_message or pending.error_code or 'unknown error'}"
            )
        elif pending.decision_type == DECISION_TYPE_CHECKPOINT_FAILURE:
            self._write(
                f"Checkpoint failed: {pending.error_message or pending.error_code or 'unknown error'}"
            )
        elif pending.decision_type == DECISION_TYPE_NO_AVAILABLE_AGENTS:
            self._write("No available agents remain in the room.")
        self._write(f"Allowed actions: {', '.join(pending.allowed_actions)}")

    def _render_help(self) -> None:
        self._write(
            "Commands: /checkpoint /swap <agent> /status /history [/query] /edit <field_path> <value> /clear <field_path> /metrics /archive /end /help"
        )

    def _render_participants(self, participants: Sequence[Participant]) -> None:
        self._write("Participants:")
        for participant in participants:
            if isinstance(participant, Agent):
                self._write(
                    f"- {participant.participant_id}: {participant.display_name} [{participant.role}] via {participant.provider}:{participant.model_id}"
                )
            else:
                self._write(
                    f"- {participant.participant_id}: {participant.display_name} [{participant.participant_type.value}]"
                )

    def _render_archived_rooms(self) -> None:
        archived = self.list_archived_rooms()
        if not archived:
            self._write("No archived rooms found.")
            return
        self._write("Archived rooms:")
        for room_id in archived:
            self._write(f"- {room_id}")

    def _discover_and_render_provider_status(self, provider: ProviderLayer) -> None:
        provider.discover_keys()
        self._write("Provider keys:")
        for adapter in DEFAULT_ADAPTERS:
            status = provider.get_provider_status(adapter.provider)
            if not status.registered:
                self._write(f"- {adapter.provider}: not detected")
                continue
            source = status.source_name or status.key_source.value if status.key_source else "unknown"
            suffix = f"{status.model_count} models"
            if status.last_error:
                suffix += f"; discovery error: {status.last_error}"
            self._write(f"- {adapter.provider}: {source}, {suffix}")

    def _split_edit_args(self, remainder: str) -> tuple[str, str | None]:
        if not remainder:
            raise ValueError("usage: /edit <field_path> <value>")
        field_path, _, value_text = remainder.partition(" ")
        return field_path, value_text if value_text else None

    def _normalize_field_path(self, field_path: str) -> str:
        normalized = field_path.strip()
        if not normalized:
            raise ValueError("field_path is required")
        if normalized.startswith("/"):
            return normalized
        return f"/{normalized}"

    def _display_field_path(self, field_path: str) -> str:
        return field_path[1:] if field_path.startswith("/") else field_path

    def _parse_value(self, raw: str) -> JSONValue:
        stripped = raw.strip()
        if stripped == "":
            return ""
        if stripped[0] in {'"', "{", "["} or stripped in {"true", "false", "null"}:
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return stripped
        if re.fullmatch(r"-?\d+", stripped):
            return int(stripped)
        if re.fullmatch(r"-?\d+\.\d+", stripped):
            return float(stripped)
        return stripped

    def _prompt_choice(self, allowed_actions: Sequence[str]) -> str | None:
        while True:
            choice = self._readline(f"Action [{'/'.join(allowed_actions)}]: ")
            if choice is None:
                return None
            normalized = choice.strip()
            if normalized in allowed_actions:
                return normalized
            self._error(f"invalid action '{normalized}'")

    def _prompt_for_archived_room(self) -> str:
        archived = self.list_archived_rooms()
        if not archived:
            raise FileNotFoundError("no archived rooms are available to resume")
        self._render_archived_rooms()
        selected = self._require_input("Archived room id: ")
        if selected not in archived:
            raise FileNotFoundError(f"archived room '{selected}' does not exist")
        return selected

    def _next_room_id(self, name: str) -> str:
        base = SLUG_RE.sub("-", name.strip().lower()).strip("-") or "room"
        candidate = base
        counter = 2
        while (self.rooms_root / candidate).exists():
            candidate = f"{base}-{counter}"
            counter += 1
        return candidate

    def _participant_labels(self, session: ShellSession) -> dict[str, str]:
        return {
            participant.participant_id: participant.display_name
            for participant in session.storage.load_room_config().participants
        }

    def _agent_display_names(self, session: ShellSession) -> list[str]:
        return [
            participant.display_name
            for participant in session.storage.load_room_config().participants
            if isinstance(participant, Agent)
        ]

    def _find_agent(self, session: ShellSession, participant_id: str) -> Agent:
        for participant in session.storage.load_room_config().participants:
            if participant.participant_id == participant_id and isinstance(participant, Agent):
                return participant
        raise KeyError(f"agent participant '{participant_id}' does not exist")

    def _human_participant_id(self, config: RoomConfig) -> str:
        for participant in config.participants:
            if participant.participant_type is ParticipantType.HUMAN:
                return participant.participant_id
        raise ValueError("room has no human participant")

    def _catalog_id(self, model: ProviderModel) -> str:
        return f"{model.provider}:{model.model_id}"

    def _require_input(self, prompt: str) -> str:
        line = self._readline(prompt)
        if line is None:
            raise EOFError
        value = line.strip()
        if not value:
            raise ValueError("input is required")
        return value

    def _readline(self, prompt: str) -> str | None:
        self.stdout.write(prompt)
        self.stdout.flush()
        line = self.stdin.readline()
        if line == "":
            return None
        return line.rstrip("\n")

    def _write(self, text: str) -> None:
        self.stdout.write(f"{text}\n")
        self.stdout.flush()

    def _error(self, text: str) -> None:
        self.stderr.write(f"{text}\n")
        self.stderr.flush()


def main(argv: Sequence[str] | None = None) -> int:
    return CliShell().run(argv)
