from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from app.config import Config


@dataclass
class RuntimeConfig:
    enabled: bool
    sport: str
    day: str
    time_slot: str
    dry_run: bool
    poll_interval_seconds: int

    @classmethod
    def from_config(cls, cfg: Config) -> "RuntimeConfig":
        return cls(
            enabled=False,
            sport=cfg.sport,
            day=cfg.day,
            time_slot=cfg.time_slot,
            dry_run=cfg.dry_run,
            poll_interval_seconds=cfg.poll_interval_seconds,
        )

    @classmethod
    def load(cls, path: Path, defaults: Config) -> "RuntimeConfig":
        if not path.exists():
            runtime = cls.from_config(defaults)
            runtime.save(path)
            return runtime

        data = json.loads(path.read_text(encoding="utf-8"))
        fallback = cls.from_config(defaults)
        return cls(
            enabled=bool(data.get("enabled", fallback.enabled)),
            sport=str(data.get("sport", fallback.sport)),
            day=str(data.get("day", fallback.day)),
            time_slot=str(data.get("time_slot", fallback.time_slot)),
            dry_run=bool(data.get("dry_run", fallback.dry_run)),
            poll_interval_seconds=max(1, int(data.get("poll_interval_seconds", fallback.poll_interval_seconds))),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(path)

    def apply_to(self, cfg: Config) -> None:
        cfg.sport = self.sport
        cfg.day = self.day
        cfg.time_slot = self.time_slot
        cfg.dry_run = self.dry_run
        cfg.poll_interval_seconds = self.poll_interval_seconds

    def summary(self) -> str:
        state = "running" if self.enabled else "paused"
        dry_run = "on" if self.dry_run else "off"
        return (
            f"State: {state}\n"
            f"Target: {self.sport} {self.day} {self.time_slot}\n"
            f"Dry run: {dry_run}\n"
            f"Interval: {self.poll_interval_seconds}s"
        )
