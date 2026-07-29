from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import re
from typing import Any, Mapping

from roadnet_partition.io.paths import assert_owned_path


STAGE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class StageStatus(str, Enum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class RunContext:
    run_id: str
    run_dir: Path
    project_root: Path
    stage_name: str | None = None
    stage_dir: Path | None = None
    log_dir: Path | None = None

    def for_stage(self, stage_name: str) -> "RunContext":
        if not STAGE_NAME.fullmatch(stage_name):
            raise ValueError(f"invalid stage name: {stage_name!r}")
        stage_dir = assert_owned_path(self.run_dir / stage_name, self.run_dir)
        log_dir = assert_owned_path(self.run_dir / "logs", self.run_dir)
        return RunContext(
            run_id=self.run_id,
            run_dir=self.run_dir,
            project_root=self.project_root,
            stage_name=stage_name,
            stage_dir=stage_dir,
            log_dir=log_dir,
        )


@dataclass(frozen=True)
class StageResult:
    stage: str
    status: StageStatus
    outputs: Mapping[str, Path] = field(default_factory=dict)
    metrics: Mapping[str, Any] = field(default_factory=dict)
    contract: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not STAGE_NAME.fullmatch(self.stage):
            raise ValueError(f"invalid stage name: {self.stage!r}")
        if not isinstance(self.status, StageStatus):
            raise TypeError("status must be a StageStatus")


@dataclass(frozen=True)
class ResumeDecision:
    reusable: bool
    reasons: tuple[str, ...] = ()
