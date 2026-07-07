"""Gmail: search, read, DRAFT ONLY (never sends). OAuth token cached in token.json.
First call opens a browser for Google login - run once interactively before daemonizing.
"""
import base64
from email.mime.text import MIMEText

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from config import GMAIL_CREDS, GMAIL_TOKEN

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]

_service = None


def _svc():
    global _service
    if _service:
        return _service
    creds = None
    if GMAIL_TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not GMAIL_CREDS.exists():
                raise RuntimeError("credentials.json missing - see SETUP.md step 6")
            flow = InstalledAppFlow.from_client_secrets_file(str(GMAIL_CREDS), SCOPES)
            creds = flow.run_local_server(port=0)
        GMAIL_TOKEN.write_text(creds.to_json())
    _service = build("gmail", "v1", credentials=creds)
    return _service


def gmail_search(query, max_results=10):
    res = _svc().users().messages().list(
        userId="me", q=query, maxResults=int(max_results)).execute()
    msgs = res.get("messages", [])
    out = []
    for m in msgs:
        full = _svc().users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["From", "Subject", "Date"]).execute()
        h = {x["name"]: x["value"] for x in full["payload"]["headers"]}
        out.append(f"id={m['id']} | {h.get('Date','')} | {h.get('From','')} | "
                   f"{h.get('Subject','')} | {full.get('snippet','')[:120]}")
    return "\n".join(out) or "No results."


def _body(payload):
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode(errors="replace")
    for part in payload.get("parts", []) or []:
        b = _body(part)
        if b:
            return b
    return ""


def gmail_read(msg_id):
    full = _svc().users().messages().get(userId="me", id=msg_id, format="full").execute()
    h = {x["name"]: x["value"] for x in full["payload"]["headers"]}
    body = _body(full["payload"]) or full.get("snippet", "")
    return (f"From: {h.get('From')}\nDate: {h.get('Date')}\n"
            f"Subject: {h.get('Subject')}\n\n{body[:8000]}")


def gmail_draft(to, subject, body):
    """Creates a draft. NEVER sends. User reviews and hits send themselves."""
    msg = MIMEText(body)
    msg["to"], msg["subject"] = to, subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    d = _svc().users().drafts().create(
        userId="me", body={"message": {"raw": raw}}).execute()
    return f"Draft created (id {d['id']}). It is in your Drafts folder for review."
