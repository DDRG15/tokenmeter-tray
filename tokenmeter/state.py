"""GUI-free display contract shared by the Windows and Linux shells."""
from __future__ import annotations

import time
from datetime import datetime
from .collectors.proxy import last_request_ts, proxy_footer

STALE_CALIBRATION_DAYS = 14


class RuntimeState:
    def snapshot(self):
        return self.session.snapshot()

    def by_model(self):
        return self.session.by_model()

    def weekly_snapshot(self):
        return self.weekly.snapshot()

    def weekly_origin(self) -> tuple[str, bool]:
        """Provenance of the weekly limit for the panel: (text, is_warning)."""
        cfg = self.config
        snap = self.weekly.snapshot()
        if not snap.configured:
            return ("not configured · menu → Weekly window", True)
        if not cfg.weekly_is_calibrated:
            return ("cycle set, limit not calibrated", True)
        if snap.percent > 100:
            return ("above 100%: the weekly limit falls short", True)
        when = datetime.fromtimestamp(cfg.weekly_calibrated_at).strftime("%d/%m %H:%M")
        return (
            f"calibrated with {cfg.weekly_calibrated_from_percent:g}% of "
            f"\"All models\" · {when}",
            False,
        )

    @property
    def collectors_paused(self) -> bool:
        return any(c.paused for c in self._collectors())

    def sources_text(self) -> str:
        """Collector status, counted over the SESSION.

        It used to show the events inserted since the process started. When the
        app was opened mid-session almost everything was already in the
        database, so it said "5 calls" next to a panel showing 93: it looked
        like nothing had synced when it actually had.
        """
        per_source = self.session.calls_by_source()
        parts = []

        if self.transcripts is not None:
            if self.transcripts.paused:
                parts.append("transcripts paused")
            else:
                parts.append(f"transcripts {per_source.get('transcript', 0)}")

        if self.codex is not None and self.codex.stats.files_seen:
            # Only if there are Codex sessions: without Codex installed it adds nothing.
            if self.codex.paused:
                parts.append("codex paused")
            else:
                parts.append(f"codex {per_source.get('codex', 0)}")

        if self.proxy is not None:
            if self.proxy.bind_error:
                parts.append(f"proxy port {self.proxy.port} BUSY")
            elif self.proxy.paused:
                parts.append("proxy paused")
            else:
                parts.append(proxy_footer(
                    self.proxy.port,
                    per_source.get("proxy", 0),
                    last_request_ts(self.store),
                    time.time(),
                ))

        if not parts:
            return "no active collectors"
        return f"{'  ·  '.join(parts)}  ·  {self.sync_text()}"

    def sync_text(self) -> str:
        """Visible proof that the counter is alive and up to date."""
        for collector in self._collectors():
            if collector.stats.last_error:
                return f"{collector.name}: error; check the diagnostics"
            if not collector.running and not collector.paused:
                return f"{collector.name}: stopped"

        age = self.session.seconds_since_last_call()
        if age is None:
            return "no data yet"
        if age < 90:
            return f"last call {int(age)}s ago"
        if age < 5400:
            return f"last call {int(age // 60)} min ago"
        return f"last call {age / 3600:.1f} h ago"

    def _collectors(self):
        return [c for c in (self.transcripts, self.proxy, self.codex) if c is not None]

    def calibration_health(self) -> tuple[str, bool]:
        """Calibration status for the panel: (text, is_warning)."""
        cfg = self.config
        if not cfg.is_calibrated:
            return ("budget not calibrated · menu → Calibrate", True)

        snap = self.snapshot()
        if snap.percent > 100:
            return ("above 100%: the estimated limit falls short, recalibrate", True)

        days = (time.time() - cfg.calibrated_at) / 86400
        when = datetime.fromtimestamp(cfg.calibrated_at).strftime("%d/%m %H:%M")
        base = f"calibrated with {cfg.calibrated_from_percent:g}% from claude.ai · {when}"
        if days >= STALE_CALIBRATION_DAYS:
            return (f"{base} · {int(days)} days ago, consider recalibrating", True)
        return (base, False)
