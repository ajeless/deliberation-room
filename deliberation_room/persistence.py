"""Filesystem persistence skeleton for Deliberation Room."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .domain import (
    JSONDict,
    Checkpoint,
    Room,
    RoomConfig,
    RoomRuntimeState,
    RoundTranscriptRecord,
    StructuredState,
    SummarySnapshot,
)


def _write_json(path: Path, payload: JSONDict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _read_json(path: Path) -> JSONDict:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object in {path}")
    return data


def _append_jsonl(path: Path, payload: JSONDict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True))
        handle.write("\n")


def _read_jsonl(path: Path) -> list[JSONDict]:
    if not path.exists():
        return []
    rows: list[JSONDict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            if not isinstance(data, dict):
                raise ValueError(f"expected JSON object row in {path}")
            rows.append(data)
    return rows


@dataclass(frozen=True, slots=True)
class RoomPaths:
    root: Path

    @property
    def room_config(self) -> Path:
        return self.root / "room_config.json"

    @property
    def room_state(self) -> Path:
        return self.root / "room_state.json"

    @property
    def transcript(self) -> Path:
        return self.root / "transcript.jsonl"

    @property
    def checkpoint_log(self) -> Path:
        return self.root / "checkpoint_log.jsonl"

    @property
    def metrics(self) -> Path:
        return self.root / "metrics.jsonl"

    @property
    def summaries_dir(self) -> Path:
        return self.root / "summaries"

    @property
    def current_summary(self) -> Path:
        return self.summaries_dir / "current.json"

    @property
    def structured_state_dir(self) -> Path:
        return self.root / "structured_state"

    @property
    def current_structured_state(self) -> Path:
        return self.structured_state_dir / "current.json"

    def summary_snapshot(self, summary_id: str) -> Path:
        return self.summaries_dir / f"{summary_id}.json"

    def structured_state_revision(self, revision_id: str) -> Path:
        return self.structured_state_dir / f"{revision_id}.json"


class RoomStorage:
    """Per-room filesystem storage using JSON and JSONL artifacts."""

    def __init__(self, root: str | Path):
        self.paths = RoomPaths(Path(root))

    def initialize_room(self, room: Room) -> None:
        self.paths.root.mkdir(parents=True, exist_ok=True)
        self.save_room_config(room.config)
        self.save_room_state(room.state)

    def save_room_config(self, config: RoomConfig) -> None:
        _write_json(self.paths.room_config, config.to_dict())

    def load_room_config(self) -> RoomConfig:
        return RoomConfig.from_dict(_read_json(self.paths.room_config))

    def save_room_state(self, state: RoomRuntimeState) -> None:
        _write_json(self.paths.room_state, state.to_dict())

    def load_room_state(self) -> RoomRuntimeState:
        return RoomRuntimeState.from_dict(_read_json(self.paths.room_state))

    def append_transcript(self, record: RoundTranscriptRecord) -> None:
        _append_jsonl(self.paths.transcript, record.to_dict())

    def read_transcript(self) -> list[RoundTranscriptRecord]:
        return [RoundTranscriptRecord.from_dict(item) for item in _read_jsonl(self.paths.transcript)]

    def append_checkpoint(self, checkpoint: Checkpoint) -> None:
        _append_jsonl(self.paths.checkpoint_log, checkpoint.to_dict())

    def read_checkpoints(self) -> list[Checkpoint]:
        return [Checkpoint.from_dict(item) for item in _read_jsonl(self.paths.checkpoint_log)]

    def write_summary_snapshot(self, snapshot: SummarySnapshot) -> None:
        payload = snapshot.to_dict()
        _write_json(self.paths.summary_snapshot(snapshot.summary_id), payload)
        _write_json(self.paths.current_summary, payload)

    def load_summary_snapshot(self, summary_id: str) -> SummarySnapshot:
        return SummarySnapshot.from_dict(_read_json(self.paths.summary_snapshot(summary_id)))

    def load_current_summary(self) -> SummarySnapshot | None:
        if not self.paths.current_summary.exists():
            return None
        return SummarySnapshot.from_dict(_read_json(self.paths.current_summary))

    def write_structured_state_revision(self, state: StructuredState) -> None:
        payload = state.to_dict()
        _write_json(self.paths.structured_state_revision(state.revision_id), payload)
        _write_json(self.paths.current_structured_state, payload)

    def load_structured_state_revision(self, revision_id: str) -> StructuredState:
        return StructuredState.from_dict(_read_json(self.paths.structured_state_revision(revision_id)))

    def load_current_structured_state(self) -> StructuredState | None:
        if not self.paths.current_structured_state.exists():
            return None
        return StructuredState.from_dict(_read_json(self.paths.current_structured_state))

    def append_metric(self, metric: JSONDict) -> None:
        _append_jsonl(self.paths.metrics, metric)

    def read_metrics(self) -> list[JSONDict]:
        return _read_jsonl(self.paths.metrics)

    def read_raw_json(self, path: str | Path) -> JSONDict:
        return _read_json(Path(path))

    def read_raw_jsonl(self, path: str | Path) -> list[JSONDict]:
        return _read_jsonl(Path(path))
