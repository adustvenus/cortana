"""Gmail: search, read, DRAFT ONLY (never sends). OAuth token cached in token.json.
First call opens a browser for Google login - run once interactively before daemonizing.
"""
import base64
from email.mime.text import MIMEText

from googleapiclient.discovery import build

from tools import google_auth

_service = None


def _svc():
    global _service
    if _service is None:
        _service = build("gmail", "v1", credentials=google_auth.creds())
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
