"""Calendar writer abstraction and Google Calendar implementation."""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .models import EventData
from .types import GoogleEvent

log = logging.getLogger(__name__)


class CalendarWriter(ABC):
    """Abstract interface for writing to a calendar."""

    @abstractmethod
    def fetch_events(self, start: datetime, end: datetime) -> list[GoogleEvent]:
        """Fetch all events in the given time window.

        Args:
            start: Window start (inclusive), must be timezone-aware.
            end:   Window end (exclusive), must be timezone-aware.

        Returns:
            List of raw event resource dicts.
        """

    @abstractmethod
    def create_event(self, ev: EventData) -> str:
        """Create a new event and return its ID.

        Args:
            ev: Event data to create.

        Returns:
            The calendar system's event ID.

        Raises:
            HttpError: On an API error.
        """

    @abstractmethod
    def update_event(self, event_id: str, ev: EventData) -> None:
        """Update an existing event.

        Args:
            event_id: ID of the event in the calendar system.
            ev:       New event data to write.

        Raises:
            HttpError: On an API error.
        """

    @abstractmethod
    def delete_event(self, event_id: str) -> None:
        """Delete an event.

        Args:
            event_id: ID of the event to delete.

        Raises:
            HttpError: On a non-404/410 API error.
        """


class GoogleCalendarClient(CalendarWriter):
    """Thin wrapper around the Google Calendar REST API.

    Handles OAuth token management, pagination, and transient-error retries.
    """

    _SCOPES: ClassVar[list[str]] = ["https://www.googleapis.com/auth/calendar"]
    _MAX_RESULTS_PER_PAGE: ClassVar[int] = 2500
    _RETRYABLE_STATUSES: ClassVar[frozenset[int]] = frozenset({429, 500, 503})
    _RETRY_DELAY_SECS: ClassVar[float] = 5.0

    def __init__(
        self,
        calendar_id: str,
        credentials_path: Path,
        token_path: Path,
    ) -> None:
        """Initialise the client, refreshing or acquiring OAuth credentials.

        Args:
            calendar_id:      Google Calendar ID or ``"primary"``.
            credentials_path: Path to ``credentials.json`` from Google Cloud Console.
            token_path:       Where to cache the access/refresh token.

        Raises:
            FileNotFoundError: If ``credentials_path`` does not exist.
        """
        self._calendar_id = calendar_id
        self._service: Any = self._build_service(credentials_path, token_path)

    def _build_service(self, credentials_path: Path, token_path: Path) -> Any:
        """Authenticate and return a built ``googleapiclient`` service resource."""
        if not credentials_path.exists():
            raise FileNotFoundError(
                f"Google credentials file not found: {credentials_path}\n"
                "Download it from Google Cloud Console → APIs & Services → Credentials."
            )

        creds: Credentials | None = None
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), self._SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                log.info("Refreshing Google OAuth token…")
                creds.refresh(Request())
            else:
                log.info("Starting interactive OAuth flow (browser will open)…")
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(credentials_path), self._SCOPES
                )
                creds = flow.run_local_server(port=0)
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(creds.to_json())
            log.info("Token cached at %s", token_path)

        return build("calendar", "v3", credentials=creds)

    def fetch_events(self, start: datetime, end: datetime) -> list[GoogleEvent]:
        """Fetch all non-deleted events in the given window, handling pagination."""
        result: list[GoogleEvent] = []
        page_token: str | None = None

        while True:
            resp: dict[str, Any] = (
                self._service.events()
                .list(
                    calendarId=self._calendar_id,
                    timeMin=start.isoformat(),
                    timeMax=end.isoformat(),
                    singleEvents=True,
                    maxResults=self._MAX_RESULTS_PER_PAGE,
                    pageToken=page_token,
                    showDeleted=False,
                )
                .execute()
            )
            result.extend(resp.get("items", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        return result

    def create_event(self, ev: EventData) -> str:
        """Create a new event and return its Google event ID.

        Retries once on transient server errors (429, 500, 503).

        Raises:
            HttpError: On a non-retryable API error.
        """
        body = self._build_body(ev)
        last_exc: HttpError | None = None

        for attempt in range(2):
            try:
                created: GoogleEvent = (
                    self._service.events()
                    .insert(calendarId=self._calendar_id, body=body)
                    .execute()
                )
                return created["id"]
            except HttpError as exc:
                last_exc = exc
                if exc.resp.status in self._RETRYABLE_STATUSES and attempt == 0:
                    log.warning(
                        "HTTP %s on insert — retrying in %.0f s…",
                        exc.resp.status,
                        self._RETRY_DELAY_SECS,
                    )
                    time.sleep(self._RETRY_DELAY_SECS)
                else:
                    raise

        raise last_exc  # type: ignore[misc]

    def update_event(self, google_id: str, ev: EventData) -> None:
        """Update an existing Google Calendar event."""
        body = self._build_body(ev)
        self._service.events().update(
            calendarId=self._calendar_id,
            eventId=google_id,
            body=body,
        ).execute()

    def delete_event(self, google_id: str) -> None:
        """Delete a Google Calendar event."""
        self._service.events().delete(
            calendarId=self._calendar_id,
            eventId=google_id,
        ).execute()

    @staticmethod
    def _build_body(ev: EventData) -> dict[str, Any]:
        """Convert an ``EventData`` into a Google Calendar API request body."""
        body: dict[str, Any] = {"summary": ev.title, "status": ev.status}

        if ev.location:
            body["location"] = ev.location
        if ev.url:
            body["description"] = ev.url

        if ev.all_day:
            body["start"] = {"date": ev.start}
            body["end"] = {"date": ev.end}
        else:
            body["start"] = {"dateTime": ev.start, "timeZone": ev.tz_id}
            body["end"] = {"dateTime": ev.end, "timeZone": ev.tz_id}

        return body
