"""Configuration on disk: `%APPDATA%\\TokenMeter\\config.json`."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from urllib.parse import urlsplit

APP_NAME = "TokenMeter"
log = logging.getLogger(__name__)


def _now() -> float:
    return time.time()

DEFAULT_UPSTREAMS: dict[str, str] = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
    "google": "https://generativelanguage.googleapis.com",
    "openrouter": "https://openrouter.ai/api",
    "groq": "https://api.groq.com/openai",
    "ollama": "http://127.0.0.1:11434",
}


def data_dir() -> Path:
    """The app data folder. Honors TOKENMETER_HOME for tests."""
    override = os.environ.get("TOKENMETER_HOME")
    if override:
        return Path(override)
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return Path(base) / APP_NAME


@dataclass
class Config:
    # --- Budget and percentage ---
    session_budget_tokens: int = 500_000
    percent_basis: str = "io"  # "io" (input+output) | "all" (includes cache)
    session_window_hours: float = 5.0
    # What counts toward the percentage, the anchor and the week: "claude" (only
    # what the Claude plan consumes, which is what calibration is done against)
    # or "all".
    percent_scope: str = "claude"

    # Provenance of the budget: empty = uncalibrated default value.
    calibrated_at: float = 0.0
    calibrated_from_percent: float = 0.0
    calibrated_tokens: int = 0
    calibrated_session_start: float = 0.0
    calibrated_unseen_tokens: int = 0
    calibrated_scope: str = "claude"

    # --- Weekly window (the second limit claude.ai shows) ---
    weekly_enabled: bool = True
    weekly_window_days: float = 7.0
    weekly_budget_tokens: int = 3_000_000
    weekly_calibrated_at: float = 0.0
    weekly_calibrated_from_percent: float = 0.0

    @property
    def weekly_is_calibrated(self) -> bool:
        return self.weekly_calibrated_at > 0

    def apply_weekly_calibration(self, cal) -> None:
        self.weekly_budget_tokens = cal.budget_tokens
        self.weekly_calibrated_at = _now()
        self.weekly_calibrated_from_percent = cal.observed_percent

    @property
    def is_calibrated(self) -> bool:
        return self.calibrated_at > 0

    def apply_calibration(self, cal) -> None:
        self.session_budget_tokens = cal.budget_tokens
        self.percent_basis = cal.basis
        self.calibrated_at = _now()
        self.calibrated_from_percent = cal.observed_percent
        self.calibrated_tokens = cal.counted_tokens

    # --- Collectors ---
    proxy_enabled: bool = True
    proxy_host: str = "127.0.0.1"
    proxy_port: int = 8787
    upstreams: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_UPSTREAMS))
    inject_stream_usage: bool = True

    transcripts_enabled: bool = True
    transcripts_root: str = ""  # empty = ~/.claude/projects
    transcripts_poll_seconds: float = 2.0

    # Codex CLI / extension sessions. Empty = ~/.codex or $CODEX_HOME.
    codex_enabled: bool = True
    codex_root: str = ""
    codex_poll_seconds: float = 5.0

    # Manual per-provider billing type, for what cannot be detected:
    # {"groq": "api", "ollama": "local"}. Values: subscription, api, local.
    # It never overrides a certain detection.
    billing_overrides: dict[str, str] = field(default_factory=dict)

    # --- UI ---
    warn_percent: int = 60
    danger_percent: int = 85
    show_cost: bool = True

    # --- Alerts ---
    alerts_enabled: bool = True
    alert_thresholds: list[int] = field(default_factory=lambda: [60, 85, 100])

    # --- Retention ---
    retain_days: int = 90

    def invalidate_calibration(self, *, weekly: bool = True) -> None:
        """A new database or budget does not inherit the previous provenance."""
        self.calibrated_at = self.calibrated_from_percent = 0.0
        self.calibrated_tokens = self.calibrated_unseen_tokens = 0
        self.calibrated_session_start = 0.0
        if weekly:
            self.weekly_calibrated_at = self.weekly_calibrated_from_percent = 0.0

    def set_basis(self, basis: str) -> None:
        if basis not in ("io", "all"):
            raise ValueError("invalid percentage basis")
        if basis != self.percent_basis:
            self.invalidate_calibration()
            self.percent_basis = basis

    @classmethod
    def from_dict(cls, raw: object, *, strict: bool = False) -> "Config":
        """Validates without silent coercions (bool does not count as int)."""
        if not isinstance(raw, dict):
            raise ValueError("config.json must contain an object")
        defaults = cls()
        values = {}
        errors = []
        enums = {"percent_basis": {"io", "all"}, "percent_scope": {"claude", "all"},
                 "calibrated_scope": {"claude", "all"}}
        for f in fields(cls):
            if f.name not in raw:
                continue
            value, default = raw[f.name], getattr(defaults, f.name)
            valid = True
            if f.name in enums:
                valid = isinstance(value, str) and value in enums[f.name]
            elif isinstance(default, bool):
                valid = isinstance(value, bool)
            elif isinstance(default, (int, float)):
                # Compare before converting to double: a huge JSON integer can
                # make math.isfinite raise OverflowError.
                valid = (type(value) in (int, float) and 0 <= value <= 2**63-1
                         and math.isfinite(value)
                         and (not isinstance(default, int) or type(value) is int))
                if f.name in {"session_budget_tokens", "weekly_budget_tokens", "retain_days",
                              "session_window_hours", "weekly_window_days",
                              "transcripts_poll_seconds", "codex_poll_seconds"}:
                    valid = valid and value > 0
                if f.name == "proxy_port":
                    valid = valid and 1 <= value <= 65535
                # Technical limits of Windows clocks and waits. The calibration
                # fields are dates, not arbitrary durations.
                if f.name in {"calibrated_at", "weekly_calibrated_at", "calibrated_session_start"}:
                    valid = valid and value <= 253402214400
                if f.name == "session_window_hours":
                    valid = valid and value <= 24*36525
                if f.name == "weekly_window_days":
                    valid = valid and value <= 36525
                if f.name in {"transcripts_poll_seconds", "codex_poll_seconds"}:
                    valid = valid and value <= 86400
                if f.name.endswith("_percent") or f.name in {"warn_percent", "danger_percent"}:
                    valid = valid and value <= 100
            elif isinstance(default, str):
                valid = isinstance(value, str)
                if f.name == "proxy_host":
                    valid = valid and value in {"127.0.0.1", "localhost", "::1"}
            elif f.name == "upstreams":
                valid = isinstance(value, dict) and bool(value)
                if valid:
                    for name, url in value.items():
                        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                            valid = False
                            break
                        try:
                            parsed = urlsplit(url) if isinstance(url, str) else None
                            valid = bool(parsed and parsed.scheme in {"http", "https"}
                                         and parsed.hostname and not parsed.username
                                         and not parsed.password and not parsed.query and not parsed.fragment)
                            if parsed:
                                _ = parsed.port
                        except ValueError:
                            valid = False
                        if not valid:
                            break
            elif f.name == "billing_overrides":
                valid = isinstance(value, dict) and all(
                    isinstance(k, str) and v in ("api", "subscription", "local")
                    for k, v in value.items() if isinstance(v, str))
                valid = valid and all(isinstance(v, str) for v in value.values()) if isinstance(value, dict) else False
            elif f.name == "alert_thresholds":
                valid = (isinstance(value, list) and all(type(v) is int and 0 < v <= 100 for v in value))
            if valid:
                values[f.name] = value
            else:
                errors.append(f.name)
        cfg = cls(**values)
        if cfg.warn_percent >= cfg.danger_percent:
            errors.extend(["warn_percent", "danger_percent"])
            cfg.warn_percent, cfg.danger_percent = defaults.warn_percent, defaults.danger_percent
        if errors:
            if strict:
                raise ValueError("invalid configuration: " + ", ".join(errors))
            log.warning("Invalid settings replaced by defaults: %s", ", ".join(errors))
        return cfg

    @property
    def window_seconds(self) -> float:
        return self.session_window_hours * 3600.0

    def codex_path(self) -> Path:
        if self.codex_root:
            return Path(self.codex_root)
        from .collectors.codex import default_codex_root

        return default_codex_root()

    def transcripts_path(self) -> Path:
        if self.transcripts_root:
            return Path(self.transcripts_root)
        return Path(os.path.expanduser("~")) / ".claude" / "projects"

    # -- persistence ------------------------------------------------------

    @classmethod
    def path(cls) -> Path:
        return data_dir() / "config.json"

    @classmethod
    def load(cls) -> "Config":
        p = cls.path()
        if not p.exists():
            cfg = cls()
            cfg.save()
            return cfg
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            cls.from_dict(raw, strict=True)
        except (ValueError, UnicodeError, OSError) as exc:
            # Keep the exact original bytes before a recovery.
            if p.exists():
                backup = p.with_name(f"config-invalid-{time.time_ns()}.json")
                try:
                    shutil.copyfile(p, backup)
                except OSError:
                    raise OSError("Could not back up the invalid configuration") from exc
            log.warning("Configuration recovered; the original is kept in config-invalid-*.json")
            if 'raw' in locals() and isinstance(raw, dict):
                return cls.from_dict(raw)
            return cls()
        return cls.from_dict(raw)

    def save(self) -> None:
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(self), indent=2, ensure_ascii=False, allow_nan=False)
        self.from_dict(json.loads(payload), strict=True)
        fd, name = tempfile.mkstemp(prefix="config-", suffix=".tmp", dir=p.parent)
        tmp = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            tmp.replace(p)
        finally:
            tmp.unlink(missing_ok=True)
