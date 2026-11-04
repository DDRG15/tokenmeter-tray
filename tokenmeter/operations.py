"""File operations shared by desktop shells, without importing a GUI toolkit."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile

from .exports import export_usage
from .files import output_destination


def save_export(app, path, days=30, *, provider="", source="", format="json"):
    """Write a consistent filtered export atomically, protecting live app files."""
    body, _, _ = export_usage(app, days, provider=provider, source=source, format=format)
    return write_export(body, path, app.store.path)


def write_export(body, path, protected_db):
    """Atomically write an export, refusing TokenMeter's own data files."""
    destination = output_destination(path, protected_db)
    fd, name = tempfile.mkstemp(prefix="export-", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
        os.replace(name, destination)
    finally:
        Path(name).unlink(missing_ok=True)
    return destination


def set_budget(app, amount):
    if type(amount) is not int or not 1000 <= amount <= 1_000_000_000:
        raise ValueError("Budget must be an integer between 1,000 and 1,000,000,000")
    app.config.invalidate_calibration(weekly=False)
    app.config.session_budget_tokens = amount
    app.config.save()


def calibrate_session(app, observed):
    """Apply the existing calibration/drift/multipoint algorithms to this session."""
    from dataclasses import replace
    from .calibration import calibrate, measure_drift, fit_session
    snap = app.snapshot()
    counted = snap.totals.basis(app.config.percent_basis)
    cal = calibrate(observed, counted, app.config.percent_basis)
    drift = measure_drift(observed, snap.used_tokens, app.config.session_budget_tokens) if app.config.is_calibrated else None
    app.store.add_calibration(ts=snap.now_ts, observed_percent=observed,
        counted_tokens=counted, basis=app.config.percent_basis,
        budget_tokens=cal.budget_tokens, session_start_ts=snap.start_ts,
        predicted_percent=drift.predicted_percent if drift else None, scope=app.config.percent_scope)
    rows = app.store.calibrations_for_session(snap.start_ts, app.config.percent_basis, app.config.percent_scope)
    fit = fit_session([(r["counted_tokens"], r["observed_percent"]) for r in rows])
    if fit is not None and not fit.misaligned:
        cal = replace(cal, budget_tokens=fit.budget_tokens)
    app.config.apply_calibration(cal)
    app.config.calibrated_session_start = snap.start_ts
    app.config.calibrated_scope = app.config.percent_scope
    app.config.calibrated_unseen_tokens = fit.unseen_tokens if fit is not None and not fit.misaligned else 0
    app.config.save()
    return "\n\n".join(item.summary() for item in (cal, drift, fit) if item is not None)


def calibrate_week(app, observed):
    from .calibration import calibrate
    cal = calibrate(observed, app.weekly.snapshot().used_tokens, app.config.percent_basis)
    app.config.apply_weekly_calibration(cal)
    app.config.save()
    return cal.summary()
