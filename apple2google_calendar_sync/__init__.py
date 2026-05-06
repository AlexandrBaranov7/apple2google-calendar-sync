"""Apple2Google Calendar Sync — one-way calendar synchronization.

Syncs events from Apple Calendar (Exchange/Outlook) to Google Calendar,
using content-based matching to handle recurring events correctly.
"""
from .calendar_reader import AppleCalendarReader, CalendarReader
from .calendar_writer import CalendarWriter, GoogleCalendarClient
from .exceptions import (
    CalendarAccessDeniedError,
    CalendarNotFoundError,
    CalendarSyncError,
)
from .models import EventData, SyncConfig, SyncStats
from .state import SyncState
from .syncer import CalendarSyncer

__version__ = "1.0.0"
__all__ = [
    "AppleCalendarReader",
    "CalendarReader",
    "CalendarWriter",
    "GoogleCalendarClient",
    "CalendarSyncError",
    "CalendarAccessDeniedError",
    "CalendarNotFoundError",
    "EventData",
    "SyncConfig",
    "SyncStats",
    "SyncState",
    "CalendarSyncer",
]
