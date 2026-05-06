"""Persistent state management for sync mappings."""
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class SyncState:
    """Persists the ``state_key → google_event_id`` mapping between sync runs.

    The state key has the form ``{apple_uid}|{start_value}``.  This encoding
    makes each recurring-event occurrence independently addressable even though
    all occurrences share the same ``calendarItemExternalIdentifier``.
    """

    def __init__(self, path: Path) -> None:
        """Load (or initialise) state from disk.

        Args:
            path: Path to the JSON state file.  The file and any missing parent
                  directories are created automatically on the first ``save()``.
        """
        self._path = path
        self._data: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        """Read the state file from disk.

        Returns:
            Parsed mapping, or an empty dict if the file is absent or corrupt.
        """
        if self._path.exists():
            try:
                return json.loads(self._path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Could not read state file (%s) — starting fresh.", exc)
        return {}

    def save(self, mapping: dict[str, str]) -> None:
        """Write the mapping to disk and update the in-memory copy.

        Args:
            mapping: Updated ``state_key → google_event_id`` dict.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(mapping, indent=2, ensure_ascii=False))
        self._data = mapping

    @property
    def data(self) -> dict[str, str]:
        """Return a shallow copy of the current in-memory state."""
        return dict(self._data)
