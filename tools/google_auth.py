"""Shared Google OAuth for all Google tools (Gmail + Calendar).

One token.json, one consent covering every scope below. Adding calendar here
means the existing Gmail token no longer matches the scope set, so the next
call re-runs the browser consent ONCE - after that both Gmail and Calendar
work from the same credentials. Run once interactively before daemonizing.
"""
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from config import GMAIL_CREDS, GMAIL_TOKEN

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.readonly",
]

_creds = None


def creds():
    """Return valid Google credentials, refreshing or (re)authorizing as needed."""
    global _creds
    if _creds and _creds.valid:
        return _creds
    c = None
    if GMAIL_TOKEN.exists():
        c = Credentials.from_authorized_user_file(str(GMAIL_TOKEN), SCOPES)
    if not c or not c.valid:
        if c and c.expired and c.refresh_token:
            c.refresh(Request())
        else:
            if not GMAIL_CREDS.exists():
                raise RuntimeError("credentials.json missing - see SETUP.md step 6")
            flow = InstalledAppFlow.from_client_secrets_file(str(GMAIL_CREDS), SCOPES)
            c = flow.run_local_server(port=0)
        GMAIL_TOKEN.write_text(c.to_json())
    _creds = c
    return _creds
