"""Command-line interface and entry point."""
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .calendar_reader import AppleCalendarReader
from .calendar_writer import GoogleCalendarClient
from .exceptions import CalendarAccessDeniedError, CalendarNotFoundError
from .models import SyncConfig
from .state import SyncState
from .syncer import CalendarSyncer

log = logging.getLogger(__name__)


def configure_logging() -> None:
    """Configure root logger to write INFO+ to stdout."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


def main(config_module: Any = None) -> int:
    """CLI entry point.

    Reads configuration, builds all components, runs a sync cycle, and returns
    an appropriate exit code.

    Args:
        config_module: Configuration module (defaults to importing 'config').
                       Must have: EXCHANGE_CALENDAR_NAME, GOOGLE_CALENDAR_ID,
                       CREDENTIALS_PATH, TOKEN_PATH, STATE_PATH.

    Returns:
        0: Sync completed and all events verified.
        1: Configuration or authentication error.
        2: Sync completed but one or more events could not be verified.
        130: Interrupted by the user (Ctrl-C).
    """
    if config_module is None:
        import config as config_module  # type: ignore[import-not-found]

    configure_logging()

    cfg = SyncConfig(
        exchange_calendar_name=config_module.EXCHANGE_CALENDAR_NAME,
        google_calendar_id=config_module.GOOGLE_CALENDAR_ID,
        credentials_path=Path(config_module.CREDENTIALS_PATH).expanduser(),
        token_path=Path(config_module.TOKEN_PATH).expanduser(),
        state_path=Path(config_module.STATE_PATH).expanduser(),
    )

    now = datetime.now(tz=timezone.utc)
    sync_start = now - timedelta(days=cfg.horizon_past_days)
    sync_end = now + timedelta(days=cfg.horizon_future_days)

    log.info(
        "=== Calendar sync start  [%s → %s] ===",
        sync_start.date(),
        sync_end.date(),
    )
    log.info("Source : Apple Calendar / %r", cfg.exchange_calendar_name)
    log.info("Target : Google Calendar / %r", cfg.google_calendar_id)

    try:
        reader = AppleCalendarReader(cfg.exchange_calendar_name)
        writer = GoogleCalendarClient(
            cfg.google_calendar_id, cfg.credentials_path, cfg.token_path
        )
        state = SyncState(cfg.state_path)
        syncer = CalendarSyncer(
            reader,
            writer,
            state,
            verify_wait_secs=cfg.verify_wait_secs,
            time_tolerance_secs=cfg.time_tolerance_secs,
        )

        stats = syncer.run(sync_start, sync_end)

        log.info("=== Sync complete: %s ===", stats.summary())

        if stats.missing:
            log.warning(
                "%d event(s) could not be verified in Google:", len(stats.missing)
            )
            for ev in stats.missing:
                log.warning("  MISSING  %s  [%s – %s]", ev.title, ev.start, ev.end)
            return 2

        return 0

    except CalendarAccessDeniedError as exc:
        log.error("%s", exc)
        return 1
    except CalendarNotFoundError as exc:
        log.error("%s", exc)
        return 1
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.info("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
