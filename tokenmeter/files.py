"""File destinations chosen by the user, kept apart from the live state."""

from pathlib import Path


def output_destination(path: str | Path, database: Path) -> Path:
    """Refuses to overwrite internal files, even through equivalent paths."""
    destination = Path(path)
    database = Path(database).resolve()
    protected = {database, *(Path(str(database) + suffix) for suffix in ("-wal", "-shm", "-journal"))}
    protected.update(database.parent / name for name in (
        "config.json", "instance.lock", "quit.request", "tokenmeter.log",
        "tokenmeter.log.1", "tokenmeter.log.2", "tokenmeter.log.3"))
    if destination.resolve() in {p.resolve() for p in protected}:
        raise ValueError("Choose another name: that file holds TokenMeter internal data.")
    return destination
