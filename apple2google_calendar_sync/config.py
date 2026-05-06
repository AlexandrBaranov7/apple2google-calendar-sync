# Calendar sync configuration
# Edit these values before first run.

# Name of the Exchange/Outlook calendar as it appears in Apple Calendar
EXCHANGE_CALENDAR_NAME = "Календарь"

# Target Google Calendar ID.
# Use "primary" for your main calendar, or find the ID in
# Google Calendar → Settings → [calendar name] → "Calendar ID"
GOOGLE_CALENDAR_ID = "primary"

# Path to credentials.json downloaded from Google Cloud Console
# (APIs & Services → Credentials → OAuth 2.0 Client ID → Download JSON)
CREDENTIALS_PATH = "/Users/baranov.am/.calendar_sync/credentials.json"

# Path where the OAuth token will be cached after the first interactive login
TOKEN_PATH = "/Users/baranov.am/.calendar_sync/token.json"

# Path to the sync-state file (maps Apple UID → Google event ID)
STATE_PATH = "~/.calendar_sync_state.json"
