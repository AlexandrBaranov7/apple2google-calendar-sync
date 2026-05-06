# Apple2Google Calendar Sync

One-way calendar synchronization: **Apple Calendar (Exchange/Outlook) → Google Calendar**.

Syncs events from Apple Calendar to Google Calendar using content-based matching (title + time range + URL), correctly handling recurring events and allowing flexible re-linking without state-file lock-in.

## Architecture

**Modular OOP design with abstract base classes for extensibility:**

- `AppleCalendarReader` — reads events from Apple Calendar via EventKit (pyobjc)
- `GoogleCalendarClient` — reads and writes events via Google Calendar REST API
- `CalendarSyncer` — orchestrates the three-pass sync (upsert, delete, verify)
- `SyncState` — persists the `state_key → google_event_id` mapping (JSON)
- Abstract `CalendarReader` / `CalendarWriter` interfaces for future integrations (Microsoft 365, iCal, etc.)

## Matching Strategy

Events are matched by **(title, time range, URL)** rather than iCalUID:

- Exchange recurring events share a single UID across all instances; using `uid|start` as the state key makes every occurrence independently addressable.
- Content-based fallback prevents duplicates after state-file resets or partial syncs.
- Meeting URLs (Teams, Zoom, Meet) are extracted from EventKit and stored in Google event descriptions.

## Sync Cycle

Each run consists of three sequential passes:

1. **Upsert pass** — every source event in the window is created or updated in Google
2. **Delete pass** — events previously synced but now gone from source are removed from Google
3. **Verify pass** — fresh Google fetch confirms all source events are present; gaps are logged

## Requirements

- **macOS** (10.13+)
- **Python** 3.10+
- **System permissions**: Calendar access (requested at first run)
- **Google Cloud** OAuth 2.0 credentials

## Installation

### 1. Clone or download

```bash
git clone https://github.com/yourusername/apple2google-calendar-sync.git
cd apple2google-calendar-sync
```

### 2. Create a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Set up Google OAuth

1. **Google Cloud Console** → [create a project](https://console.cloud.google.com/projectcreate)
2. **Enable** the **Google Calendar API**
3. **Create OAuth 2.0 credentials** (type: *Desktop application*)
4. Download the JSON credentials file
5. Save it to `~/.calendar_sync/credentials.json`

### 5. Configure

Edit `apple2google_calendar_sync/config.py`:

```python
EXCHANGE_CALENDAR_NAME = "Your Calendar Name"  # as shown in Apple Calendar
GOOGLE_CALENDAR_ID = "primary"  # or your specific calendar ID
CREDENTIALS_PATH = "~/.calendar_sync/credentials.json"
TOKEN_PATH = "~/.calendar_sync/token.json"
STATE_PATH = "~/.calendar_sync_state.json"
```

### 6. First run (interactive OAuth)

```bash
python3 -m apple2google_calendar_sync
```

A browser window opens for Google OAuth authorisation. The token is cached for future headless runs.

## Usage

### Manual sync

```bash
python3 -m apple2google_calendar_sync
```

Exit codes:
- **0**: Success, all events verified
- **1**: Configuration or auth error
- **2**: Sync completed but some events unverified
- **130**: Interrupted (Ctrl-C)

### Automated sync (macOS launchd)

Create `~/Library/LaunchAgents/com.user.apple2google-sync.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.apple2google-sync</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/venv/bin/python3</string>
        <string>-m</string>
        <string>apple2google_calendar_sync</string>
    </array>
    <key>StartInterval</key>
    <integer>900</integer>  <!-- 15 minutes -->
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/apple2google-sync.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/apple2google-sync-err.log</string>
</dict>
</plist>
```

Load it:

```bash
launchctl load ~/Library/LaunchAgents/com.user.apple2google-sync.plist
```

Check status:

```bash
launchctl list | grep apple2google
tail -f /tmp/apple2google-sync.log
```

## Development

### Project structure

```
apple2google_calendar_sync/
├── __init__.py              # public API exports
├── config.py                # user-editable configuration
├── exceptions.py            # custom exceptions
├── types.py                 # TypedDict for Google API
├── models.py                # dataclasses (EventData, SyncStats, SyncConfig)
├── calendar_reader.py       # abstract + Apple implementation
├── calendar_writer.py       # abstract + Google implementation
├── state.py                 # sync state persistence
├── syncer.py                # sync orchestration (3-pass)
└── cli.py                   # CLI entry point
```

### Extending to other calendars

Implement `CalendarReader` / `CalendarWriter` interfaces:

```python
from apple2google_calendar_sync import CalendarReader, CalendarWriter

class MicrosoftCalendarReader(CalendarReader):
    def fetch_events(self, start, end):
        # ... your implementation
        return [EventData(...), ...]

class ICalWriter(CalendarWriter):
    def fetch_events(self, start, end):
        ...
    def create_event(self, ev):
        ...
    # etc.
```

Then in your sync code:

```python
syncer = CalendarSyncer(
    reader=MicrosoftCalendarReader(...),
    writer=ICalWriter(...),
    state=state,
)
stats = syncer.run(start, end)
```

## Typing & Documentation

- **100% type-annotated** with `from __future__ import annotations`
- **Google-style docstrings** throughout (module, class, method level)
- Strict `mypy` mode ready

## License

MIT

## Contributing

Contributions welcome! Please:

1. Fork the repo
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Commit with clear messages (`git commit -am 'Add my feature'`)
4. Push and open a pull request

## Troubleshooting

### Calendar access denied

Go to **System Settings → Privacy & Security → Calendars** and grant access.

### Calendar not found

Run with logging to see available calendars:

```bash
python3 -m apple2google_calendar_sync 2>&1 | grep -i "available"
```

### Events not syncing

1. Check that the state file is writable: `ls -la ~/.calendar_sync_state.json`
2. Check logs for API errors
3. Verify Google Calendar API is enabled in Google Cloud Console
4. Ensure OAuth token is fresh (delete `~/.calendar_sync/token.json` and run interactively)

### Duplicate events

This can happen if state was reset. The content matcher should prevent duplicates, but you may need to manually clean up Google Calendar.

---

**Questions or issues?** Open an [issue on GitHub](https://github.com/yourusername/apple2google-calendar-sync/issues).
