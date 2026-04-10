"""Memory engine for transcript, summary, and structured state management."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .domain import (
    ActiveOverride,
    Checkpoint,
    CheckpointStatus,
    EditLogEntry,
    JSONDict,
    JSONValue,
    RevisionSource,
    RoomRuntimeState,
    Round,
    RoundTranscriptRecord,
    StructuredState,
    SummarySnapshot,
    utc_now,
)
from .persistence import RoomStorage
from .provider import ProviderLayer


SUMMARY_PROMPT = """You maintain the working summary for a Deliberation Room.

Produce a concise, durable summary of the room so far.
- Preserve key decisions, candidate solutions, unresolved questions, disagreements, and next actions.
- Prefer factual compression over narrative flourish.
- Keep it useful as context for future model calls.
- Return plain text only.
"""

STATE_GENERATION_PROMPT = """You maintain the canonical structured state for a Deliberation Room.

Return JSON only. Do not wrap it in Markdown fences.

The JSON object must contain exactly these top-level semantic fields:
- current_problem: string
- candidate_solutions: array of objects with id, description, status, origin
- open_questions: array of objects with id, text, raised_by, round_raised
- decisions: array of objects with id, text, round_decided, origin
- disagreements: array of objects with id, description, positions
- action_items: array of objects with id, text, assignee

Use stable IDs when possible. Preserve semantically valid prior items when they still apply.
"""

JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)
RESERVED_STATE_PATHS = {
    "schema_version",
    "revision_id",
    "previous_revision_id",
    "checkpoint_id",
    "updated_at",
    "updated_by",
    "revision_source",
    "active_overrides",
    "edit_log",
}


@dataclass(slots=True)
class ContextPayload:
    summary: str | None
    structured_state: JSONDict | None


@dataclass(slots=True)
class CheckpointRunResult:
    checkpoint: Checkpoint
    summary_snapshot: SummarySnapshot | None = None
    structured_state: StructuredState | None = None


class MemoryEngine:
    """Memory engine implementation for the MVP."""

    def __init__(
        self,
        storage: RoomStorage,
        provider_layer: ProviderLayer,
        *,
        summary_model_id: str | None = None,
        state_model_id: str | None = None,
    ) -> None:
        self.storage = storage
        self.provider_layer = provider_layer
        self.summary_model_id = summary_model_id
        self.state_model_id = state_model_id

    def append_transcript(self, round_data: Round | RoundTranscriptRecord) -> RoundTranscriptRecord:
        record = (
            round_data.to_transcript_record()
            if isinstance(round_data, Round)
            else round_data
        )
        self.storage.append_transcript(record)
        self._update_room_state_transcript_pointer(record.round_number)
        return record

    def run_checkpoint(
        self,
        *,
        reason: str,
        transcript_since_last: Sequence[RoundTranscriptRecord] | None = None,
        summary_model_id: str | None = None,
        state_model_id: str | None = None,
    ) -> CheckpointRunResult:
        checkpoint_id = self._next_identifier("chk", len(self.storage.read_checkpoints()) + 1)
        transcript_window = (
            list(transcript_since_last)
            if transcript_since_last is not None
            else self._transcript_since_last_summary()
        )
        current_summary = self.storage.load_current_summary()
        current_state = self.storage.load_current_structured_state()
        room_config = self.storage.load_room_config()
        room_state = self._try_load_room_state()
        checkpoint_round_number = self._checkpoint_round_number(transcript_window, current_summary)

        try:
            summary_completion = self.provider_layer.complete(
                summary_model_id or self.summary_model_id or self._default_model_id(),
                self._build_summary_messages(
                    room_name=room_config.name,
                    problem_statement=room_config.problem_statement,
                    current_summary=current_summary.content if current_summary else None,
                    transcript_window=transcript_window,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return self._record_checkpoint_error(
                checkpoint_id=checkpoint_id,
                round_number=checkpoint_round_number,
                reason=reason,
                room_state=room_state,
                error_code=self._error_code_for_exception(exc),
                error_message=str(exc),
            )
        if summary_completion.status.value == "error":
            return self._record_checkpoint_error(
                checkpoint_id=checkpoint_id,
                round_number=checkpoint_round_number,
                reason=reason,
                room_state=room_state,
                error_code=summary_completion.error_code,
                error_message=summary_completion.error_message or "summary generation failed",
            )

        try:
            state_completion = self.provider_layer.complete(
                state_model_id
                or self.state_model_id
                or summary_model_id
                or self.summary_model_id
                or self._default_model_id(),
                self._build_state_messages(
                    problem_statement=room_config.problem_statement,
                    summary_text=summary_completion.content,
                    prior_state=current_state,
                    transcript_window=transcript_window,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return self._record_checkpoint_error(
                checkpoint_id=checkpoint_id,
                round_number=checkpoint_round_number,
                reason=reason,
                room_state=room_state,
                error_code=self._error_code_for_exception(exc),
                error_message=str(exc),
            )
        if state_completion.status.value == "error":
            return self._record_checkpoint_error(
                checkpoint_id=checkpoint_id,
                round_number=checkpoint_round_number,
                reason=reason,
                room_state=room_state,
                error_code=state_completion.error_code,
                error_message=state_completion.error_message or "structured-state generation failed",
            )

        try:
            generated_state_payload = self._parse_generated_state(state_completion.content)
            summary_snapshot = SummarySnapshot(
                summary_id=self._next_identifier("sum", len(self.storage.list_summary_snapshots()) + 1),
                checkpoint_id=checkpoint_id,
                round_number=checkpoint_round_number,
                created_at=utc_now(),
                content=summary_completion.content.strip(),
            )
            structured_state = self._build_checkpoint_state(
                generated_state_payload=generated_state_payload,
                checkpoint_id=checkpoint_id,
                current_state=current_state,
                fallback_problem=room_config.problem_statement,
            )
        except Exception as exc:  # noqa: BLE001
            return self._record_checkpoint_error(
                checkpoint_id=checkpoint_id,
                round_number=checkpoint_round_number,
                reason=reason,
                room_state=room_state,
                error_code=self._error_code_for_exception(exc),
                error_message=str(exc),
            )
        checkpoint = Checkpoint(
            checkpoint_id=checkpoint_id,
            round_number=checkpoint_round_number,
            reason=reason,
            created_at=utc_now(),
            status=CheckpointStatus.SUCCESS,
            summary_snapshot_id=summary_snapshot.summary_id,
            structured_state_revision_id=structured_state.revision_id,
        )

        self.storage.write_summary_snapshot(summary_snapshot)
        self.storage.write_structured_state_revision(structured_state)
        self.storage.append_checkpoint(checkpoint)
        if room_state is not None:
            self._update_room_state_checkpoint_pointers(
                room_state,
                checkpoint,
                summary_snapshot,
                structured_state,
            )

        return CheckpointRunResult(
            checkpoint=checkpoint,
            summary_snapshot=summary_snapshot,
            structured_state=structured_state,
        )

    def get_context_payload(self) -> ContextPayload:
        summary = self.storage.load_current_summary()
        structured_state = self.storage.load_current_structured_state()
        return ContextPayload(
            summary=summary.content if summary else None,
            structured_state=structured_state.to_dict() if structured_state else None,
        )

    def get_transcript(self, query: str | None = None) -> list[RoundTranscriptRecord]:
        transcript = self.storage.read_transcript()
        if not query:
            return transcript
        needle = query.lower()
        filtered: list[RoundTranscriptRecord] = []
        for record in transcript:
            haystacks = [record.seed_message.content, *[response.content for response in record.responses]]
            if any(needle in haystack.lower() for haystack in haystacks):
                filtered.append(record)
        return filtered

    def apply_human_edit(self, field_path: str, new_value: JSONValue, author: str) -> StructuredState:
        current_state = self._require_current_state()
        timestamp = utc_now()
        state_payload = current_state.to_dict()
        old_value = self._get_field_value(state_payload, field_path)
        self._set_field_value(state_payload, field_path, copy.deepcopy(new_value))

        overrides = self._serialize_active_overrides(current_state)
        overrides[field_path] = ActiveOverride(
            field_path=field_path,
            value=copy.deepcopy(new_value),
            author=author,
            created_at=timestamp,
        )
        edit_log = list(current_state.edit_log)
        edit_log.append(
            EditLogEntry(
                field_path=field_path,
                old_value=copy.deepcopy(old_value),
                new_value=copy.deepcopy(new_value),
                author=author,
                source=RevisionSource.HUMAN_EDIT.value,
                timestamp=timestamp,
            )
        )

        state_body = self._coerce_state_from_payload(state_payload)
        new_state = StructuredState(
            schema_version=current_state.schema_version,
            revision_id=self._next_identifier("state", len(self.storage.list_structured_state_revisions()) + 1),
            previous_revision_id=current_state.revision_id,
            checkpoint_id=None,
            updated_at=timestamp,
            updated_by=author,
            revision_source=RevisionSource.HUMAN_EDIT,
            current_problem=state_body.current_problem,
            candidate_solutions=state_body.candidate_solutions,
            open_questions=state_body.open_questions,
            decisions=state_body.decisions,
            disagreements=state_body.disagreements,
            action_items=state_body.action_items,
            active_overrides=list(overrides.values()),
            edit_log=edit_log,
        )
        self.storage.write_structured_state_revision(new_state)
        return new_state

    def clear_human_override(self, field_path: str, author: str) -> StructuredState:
        current_state = self._require_current_state()
        timestamp = utc_now()
        overrides = self._serialize_active_overrides(current_state)
        if field_path not in overrides:
            raise KeyError(f"no active override exists for {field_path}")

        state_payload = current_state.to_dict()
        current_value = self._get_field_value(state_payload, field_path)
        fallback_value = self._fallback_value_for_clear(current_state, field_path, current_value)
        self._set_field_value(state_payload, field_path, copy.deepcopy(fallback_value))
        overrides.pop(field_path)

        edit_log = list(current_state.edit_log)
        edit_log.append(
            EditLogEntry(
                field_path=field_path,
                old_value=copy.deepcopy(current_value),
                new_value=copy.deepcopy(fallback_value),
                author=author,
                source=RevisionSource.HUMAN_CLEAR.value,
                timestamp=timestamp,
            )
        )

        state_body = self._coerce_state_from_payload(state_payload)
        new_state = StructuredState(
            schema_version=current_state.schema_version,
            revision_id=self._next_identifier("state", len(self.storage.list_structured_state_revisions()) + 1),
            previous_revision_id=current_state.revision_id,
            checkpoint_id=None,
            updated_at=timestamp,
            updated_by=author,
            revision_source=RevisionSource.HUMAN_CLEAR,
            current_problem=state_body.current_problem,
            candidate_solutions=state_body.candidate_solutions,
            open_questions=state_body.open_questions,
            decisions=state_body.decisions,
            disagreements=state_body.disagreements,
            action_items=state_body.action_items,
            active_overrides=list(overrides.values()),
            edit_log=edit_log,
        )
        self.storage.write_structured_state_revision(new_state)
        return new_state

    def get_state_history(self) -> list[StructuredState]:
        return self.storage.list_structured_state_revisions()

    def diff_state_revisions(self, left_revision_id: str, right_revision_id: str) -> dict[str, dict[str, JSONValue]]:
        left = self.storage.load_structured_state_revision(left_revision_id).to_dict()
        right = self.storage.load_structured_state_revision(right_revision_id).to_dict()
        diffs: dict[str, dict[str, JSONValue]] = {}
        self._collect_diffs(left, right, "", diffs)
        return diffs

    def _default_model_id(self) -> str:
        models = self.provider_layer.list_available_models()
        if not models:
            raise ValueError("no models are available for checkpoint generation")
        return models[0].catalog_id

    def _checkpoint_round_number(
        self,
        transcript_window: Sequence[RoundTranscriptRecord],
        current_summary: SummarySnapshot | None,
    ) -> int:
        if transcript_window:
            return transcript_window[-1].round_number
        if current_summary is not None:
            return current_summary.round_number
        return 0

    def _transcript_since_last_summary(self) -> list[RoundTranscriptRecord]:
        transcript = self.storage.read_transcript()
        current_summary = self.storage.load_current_summary()
        if current_summary is None:
            return transcript
        return [record for record in transcript if record.round_number > current_summary.round_number]

    def _build_summary_messages(
        self,
        *,
        room_name: str,
        problem_statement: str,
        current_summary: str | None,
        transcript_window: Sequence[RoundTranscriptRecord],
    ) -> list[dict[str, str]]:
        transcript_payload = [
            {
                "round_number": record.round_number,
                "round_exit_status": record.round_exit_status.value,
                "seed_message": record.seed_message.to_dict(),
                "responses": [response.to_dict() for response in record.responses],
                "participant_outcomes": {
                    participant_id: outcome.value
                    for participant_id, outcome in record.participant_outcomes.items()
                },
            }
            for record in transcript_window
        ]
        user_prompt = {
            "room_name": room_name,
            "problem_statement": problem_statement,
            "current_summary": current_summary,
            "transcript_window": transcript_payload,
        }
        return [
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user", "content": json.dumps(user_prompt, indent=2, sort_keys=True)},
        ]

    def _build_state_messages(
        self,
        *,
        problem_statement: str,
        summary_text: str,
        prior_state: StructuredState | None,
        transcript_window: Sequence[RoundTranscriptRecord],
    ) -> list[dict[str, str]]:
        transcript_payload = [
            {
                "round_number": record.round_number,
                "round_exit_status": record.round_exit_status.value,
                "seed_message": record.seed_message.to_dict(),
                "responses": [response.to_dict() for response in record.responses],
                "participant_outcomes": {
                    participant_id: outcome.value
                    for participant_id, outcome in record.participant_outcomes.items()
                },
            }
            for record in transcript_window
        ]
        prior_state_payload = None
        if prior_state is not None:
            prior_state_payload = {
                "current_problem": prior_state.current_problem,
                "candidate_solutions": [item.to_dict() for item in prior_state.candidate_solutions],
                "open_questions": [item.to_dict() for item in prior_state.open_questions],
                "decisions": [item.to_dict() for item in prior_state.decisions],
                "disagreements": [item.to_dict() for item in prior_state.disagreements],
                "action_items": [item.to_dict() for item in prior_state.action_items],
            }
        user_prompt = {
            "problem_statement": problem_statement,
            "working_summary": summary_text,
            "prior_state": prior_state_payload,
            "transcript_window": transcript_payload,
        }
        return [
            {"role": "system", "content": STATE_GENERATION_PROMPT},
            {"role": "user", "content": json.dumps(user_prompt, indent=2, sort_keys=True)},
        ]

    def _parse_generated_state(self, content: str) -> JSONDict:
        candidate = content.strip()
        match = JSON_BLOCK_RE.search(candidate)
        if match:
            candidate = match.group(1)
        payload = json.loads(candidate)
        if not isinstance(payload, dict):
            raise ValueError("structured-state generator did not return a JSON object")
        return {
            "current_problem": str(payload.get("current_problem", "")),
            "candidate_solutions": list(payload.get("candidate_solutions", [])),
            "open_questions": list(payload.get("open_questions", [])),
            "decisions": list(payload.get("decisions", [])),
            "disagreements": list(payload.get("disagreements", [])),
            "action_items": list(payload.get("action_items", [])),
        }

    def _build_checkpoint_state(
        self,
        *,
        generated_state_payload: JSONDict,
        checkpoint_id: str,
        current_state: StructuredState | None,
        fallback_problem: str,
    ) -> StructuredState:
        timestamp = utc_now()
        base_payload: JSONDict = {
            "schema_version": 1,
            "revision_id": self._next_identifier("state", len(self.storage.list_structured_state_revisions()) + 1),
            "previous_revision_id": current_state.revision_id if current_state is not None else None,
            "checkpoint_id": checkpoint_id,
            "updated_at": timestamp.isoformat().replace("+00:00", "Z"),
            "updated_by": "system",
            "revision_source": RevisionSource.CHECKPOINT.value,
            "current_problem": str(generated_state_payload.get("current_problem") or fallback_problem),
            "candidate_solutions": list(generated_state_payload.get("candidate_solutions", [])),
            "open_questions": list(generated_state_payload.get("open_questions", [])),
            "decisions": list(generated_state_payload.get("decisions", [])),
            "disagreements": list(generated_state_payload.get("disagreements", [])),
            "action_items": list(generated_state_payload.get("action_items", [])),
            "active_overrides": [],
            "edit_log": [],
        }

        active_overrides = current_state.active_overrides if current_state is not None else []
        source_payload = current_state.to_dict() if current_state is not None else None
        for override in active_overrides:
            if source_payload is not None:
                self._ensure_path_present(base_payload, source_payload, self._path_parts(override.field_path))
            self._set_field_value(base_payload, override.field_path, copy.deepcopy(override.value))
        base_payload["active_overrides"] = [override.to_dict() for override in active_overrides]
        base_payload["edit_log"] = (
            [entry.to_dict() for entry in current_state.edit_log] if current_state is not None else []
        )
        return StructuredState.from_dict(base_payload)

    def _record_checkpoint_error(
        self,
        *,
        checkpoint_id: str,
        round_number: int,
        reason: str,
        room_state: RoomRuntimeState | None,
        error_code: str | None,
        error_message: str,
    ) -> CheckpointRunResult:
        checkpoint = Checkpoint(
            checkpoint_id=checkpoint_id,
            round_number=round_number,
            reason=reason,
            created_at=utc_now(),
            status=CheckpointStatus.ERROR,
            error_code=error_code,
            error_message=error_message,
        )
        self.storage.append_checkpoint(checkpoint)
        if room_state is not None:
            self._update_room_state_checkpoint_pointers(room_state, checkpoint)
        return CheckpointRunResult(checkpoint=checkpoint)

    def _coerce_state_from_payload(self, payload: Mapping[str, Any]) -> StructuredState:
        normalized = copy.deepcopy(dict(payload))
        normalized.setdefault("schema_version", 1)
        normalized.setdefault("revision_id", "state_temp")
        normalized.setdefault("previous_revision_id", None)
        normalized.setdefault("checkpoint_id", None)
        normalized.setdefault("updated_at", utc_now().isoformat().replace("+00:00", "Z"))
        normalized.setdefault("updated_by", "system")
        normalized.setdefault("revision_source", RevisionSource.CHECKPOINT.value)
        normalized.setdefault("current_problem", "")
        normalized.setdefault("candidate_solutions", [])
        normalized.setdefault("open_questions", [])
        normalized.setdefault("decisions", [])
        normalized.setdefault("disagreements", [])
        normalized.setdefault("action_items", [])
        normalized.setdefault("active_overrides", [])
        normalized.setdefault("edit_log", [])
        return StructuredState.from_dict(normalized)

    def _serialize_active_overrides(self, state: StructuredState) -> dict[str, ActiveOverride]:
        return {override.field_path: copy.deepcopy(override) for override in state.active_overrides}

    def _fallback_value_for_clear(
        self,
        state: StructuredState,
        field_path: str,
        current_value: JSONValue,
    ) -> JSONValue:
        for entry in reversed(state.edit_log):
            if entry.field_path == field_path and entry.source == RevisionSource.HUMAN_EDIT.value:
                return copy.deepcopy(entry.old_value)
        return copy.deepcopy(current_value)

    def _set_field_value(self, state_payload: JSONDict, field_path: str, value: JSONValue) -> None:
        parts = self._path_parts(field_path)
        parent, token = self._resolve_parent(state_payload, parts)
        if isinstance(parent, dict):
            parent[token] = value
            return
        if isinstance(parent, list):
            index = self._index_for_list_token(parent, token)
            parent[index] = value
            return
        raise KeyError(f"cannot set path {field_path}")

    def _ensure_path_present(
        self,
        target_payload: JSONDict,
        source_payload: Mapping[str, Any],
        parts: list[str],
    ) -> None:
        current_target: Any = target_payload
        current_source: Any = source_payload
        for token in parts[:-1]:
            if isinstance(current_target, dict):
                if token not in current_target:
                    if not isinstance(current_source, Mapping) or token not in current_source:
                        raise KeyError(f"unknown field_path /{'/'.join(parts)}")
                    current_target[token] = copy.deepcopy(current_source[token])
                current_target = current_target[token]
                current_source = current_source[token] if isinstance(current_source, Mapping) else None
                continue
            if isinstance(current_target, list):
                target_index = self._maybe_index_for_list_token(current_target, token)
                if target_index is None:
                    if not isinstance(current_source, Sequence):
                        raise KeyError(f"unknown list item id '{token}'")
                    source_index = self._index_for_list_token(current_source, token)
                    current_target.append(copy.deepcopy(current_source[source_index]))
                    target_index = len(current_target) - 1
                    current_source = current_source[source_index]
                else:
                    current_source = (
                        current_source[self._index_for_list_token(current_source, token)]
                        if isinstance(current_source, Sequence)
                        else None
                    )
                current_target = current_target[target_index]
                continue
            raise KeyError(f"unknown field_path /{'/'.join(parts)}")

    def _get_field_value(self, state_payload: Mapping[str, Any], field_path: str) -> JSONValue:
        current: Any = state_payload
        for token in self._path_parts(field_path):
            if isinstance(current, dict):
                if token not in current:
                    raise KeyError(f"unknown field_path {field_path}")
                current = current[token]
                continue
            if isinstance(current, list):
                current = current[self._index_for_list_token(current, token)]
                continue
            raise KeyError(f"unknown field_path {field_path}")
        return copy.deepcopy(current)

    def _resolve_parent(self, state_payload: JSONDict, parts: list[str]) -> tuple[Any, str]:
        if not parts:
            raise ValueError("field_path cannot be empty")
        if parts[0] in RESERVED_STATE_PATHS:
            raise ValueError(f"field_path '/{'/'.join(parts)}' targets system-managed state")

        current: Any = state_payload
        for token in parts[:-1]:
            if isinstance(current, dict):
                if token not in current:
                    raise KeyError(f"unknown field_path /{'/'.join(parts)}")
                current = current[token]
                continue
            if isinstance(current, list):
                current = current[self._index_for_list_token(current, token)]
                continue
            raise KeyError(f"unknown field_path /{'/'.join(parts)}")
        return current, parts[-1]

    def _path_parts(self, field_path: str) -> list[str]:
        if not field_path.startswith("/"):
            raise ValueError("field_path must start with '/'")
        parts = [part for part in field_path.split("/") if part]
        if not parts:
            raise ValueError("field_path must address a concrete field")
        return parts

    def _index_for_list_token(self, items: Sequence[Any], token: str) -> int:
        for index, item in enumerate(items):
            if isinstance(item, dict) and str(item.get("id")) == token:
                return index
        raise KeyError(f"unknown list item id '{token}'")

    def _maybe_index_for_list_token(self, items: Sequence[Any], token: str) -> int | None:
        for index, item in enumerate(items):
            if isinstance(item, dict) and str(item.get("id")) == token:
                return index
        return None

    def _collect_diffs(
        self,
        left: JSONValue,
        right: JSONValue,
        path: str,
        diffs: dict[str, dict[str, JSONValue]],
    ) -> None:
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right)):
                next_path = f"{path}/{key}" if path else f"/{key}"
                self._collect_diffs(left.get(key), right.get(key), next_path, diffs)
            return
        if isinstance(left, list) and isinstance(right, list):
            if self._lists_are_id_addressable(left) and self._lists_are_id_addressable(right):
                left_map = {str(item["id"]): item for item in left if isinstance(item, dict)}
                right_map = {str(item["id"]): item for item in right if isinstance(item, dict)}
                for key in sorted(set(left_map) | set(right_map)):
                    next_path = f"{path}/{key}" if path else f"/{key}"
                    self._collect_diffs(left_map.get(key), right_map.get(key), next_path, diffs)
                return
            if left != right:
                diffs[path or "/"] = {"from": copy.deepcopy(left), "to": copy.deepcopy(right)}
            return
        if left != right:
            diffs[path or "/"] = {"from": copy.deepcopy(left), "to": copy.deepcopy(right)}

    def _lists_are_id_addressable(self, items: Sequence[Any]) -> bool:
        return all(isinstance(item, dict) and "id" in item for item in items)

    def _next_identifier(self, prefix: str, ordinal: int) -> str:
        return f"{prefix}_{ordinal:04d}"

    def _error_code_for_exception(self, exc: Exception) -> str | None:
        code = getattr(exc, "code", None)
        return str(code) if code is not None else None

    def _require_current_state(self) -> StructuredState:
        current_state = self.storage.load_current_structured_state()
        if current_state is None:
            raise RuntimeError("no structured state revision exists yet")
        return current_state

    def _try_load_room_state(self) -> RoomRuntimeState | None:
        try:
            return self.storage.load_room_state()
        except FileNotFoundError:
            return None

    def _update_room_state_transcript_pointer(self, round_number: int) -> None:
        room_state = self._try_load_room_state()
        if room_state is None:
            return
        room_state.latest_transcript_round_number = round_number
        room_state.updated_at = utc_now()
        self.storage.save_room_state(room_state)

    def _update_room_state_checkpoint_pointers(
        self,
        room_state: RoomRuntimeState,
        checkpoint: Checkpoint,
        summary_snapshot: SummarySnapshot | None = None,
        structured_state: StructuredState | None = None,
    ) -> None:
        room_state.latest_checkpoint_id = checkpoint.checkpoint_id
        if summary_snapshot is not None:
            room_state.latest_summary_snapshot_id = summary_snapshot.summary_id
        if structured_state is not None:
            room_state.latest_structured_state_revision_id = structured_state.revision_id
        room_state.updated_at = utc_now()
        self.storage.save_room_state(room_state)
