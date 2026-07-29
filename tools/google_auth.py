"""Shared Google OAuth for all Google tools (Gmail + Calendar).

One token.json, one consent covering every scope below.

WHY TOKENS KEEP DYING: while the Cloud project's OAuth consent screen is in
"Testing" publishing status, Google expires refresh tokens after SEVEN DAYS.
SETUP.md's Google step has you add yourself as a Test user, which is exactly
that state - so the agenda silently stops updating about a week after every
re-auth. The permanent fix is to PUBLISH the consent screen (Google Cloud
Console -> APIs & Services -> OAuth consent screen -> PUBLISH APP). A personal
app using these scopes can publish without formal verification; you click
through one "unverified app" warning at consent time and refresh tokens then
last until you revoke them. See AuthExpired.HELP below.

Nothing here ever opens a browser unless asked (interactive=True). Cortana and
the bridge run headless under systemd, where a surprise consent window would
hang the process instead of failing cleanly.
"""
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from config import GMAIL_CREDS, GMAIL_TOKEN

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.readonly",
]

_creds = None


class AuthExpired(RuntimeError):
    """The stored Google token can no longer be refreshed. Carries the fix."""

    HELP = ("Google access expired - reconnect with:  "
            "./venv/bin/python main.py --google-auth\n"
            "To stop this recurring every ~7 days, publish the OAuth consent "
            "screen: console.cloud.google.com -> APIs & Services -> OAuth "
            "consent screen -> PUBLISH APP. Apps left in 'Testing' have their "
            "refresh tokens expired by Google after 7 days.")

    def __init__(self, detail=""):
        super().__init__(f"{self.HELP}\n({detail})" if detail else self.HELP)


def creds(interactive=False):
    """Valid Google credentials.

    interactive=False (default, and what every daemon uses): refresh silently,
    and raise AuthExpired if the token is dead - never open a browser.
    interactive=True: run the consent flow when needed (main.py --google-auth).
    """
    global _creds
    if _creds and _creds.valid:
        return _creds

    c = None
    if GMAIL_TOKEN.exists():
        try:
            c = Credentials.from_authorized_user_file(str(GMAIL_TOKEN), SCOPES)
        except Exception as e:
            if not interactive:
                raise AuthExpired(f"token.json unreadable: {e}")
            c = None

    if c and c.valid:
        _creds = c
        return _creds

    if c and c.expired and c.refresh_token:
        try:
            c.refresh(Request())
            GMAIL_TOKEN.write_text(c.to_json())
            _creds = c
            return _creds
        except RefreshError as e:
            # Expired or revoked. The stored token is now useless; keep it on
            # disk (harmless, and useful for diagnostics) but refuse to guess.
            if not interactive:
                raise AuthExpired(str(e))
            c = None

    if not interactive:
        raise AuthExpired("no usable token")

    if not GMAIL_CREDS.exists():
        raise RuntimeError("credentials.json missing - see SETUP.md step 6")
    flow = InstalledAppFlow.from_client_secrets_file(str(GMAIL_CREDS), SCOPES)
    c = flow.run_local_server(port=0)
    GMAIL_TOKEN.write_text(c.to_json())
    _creds = c
    return _creds
