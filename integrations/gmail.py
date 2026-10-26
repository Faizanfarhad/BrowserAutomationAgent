"""Gmail API integration for read-only search and sending email."""

import base64
import html
import json
import os
import re
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_SCOPES = [GMAIL_READONLY_SCOPE, GMAIL_SEND_SCOPE]
DEFAULT_CREDENTIALS_FILE = "credentials.json"
DEFAULT_TOKEN_FILE = "gmail_token.json"


def _settings() -> tuple[Path, Path]:
    load_dotenv()
    credentials_file = Path(
        os.getenv("GMAIL_CREDENTIALS_FILE", DEFAULT_CREDENTIALS_FILE)
    )
    token_file = Path(os.getenv("GMAIL_TOKEN_FILE", DEFAULT_TOKEN_FILE))
    return credentials_file, token_file


def _get_credentials():
    """Load cached Gmail credentials or run OAuth for all required scopes."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    credentials_file, token_file = _settings()
    credentials = None

    if token_file.exists():
        try:
            token_data = json.loads(token_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            token_data = {}

        # Check the scopes saved in the token file itself. Passing GMAIL_SCOPES
        # into Google's loader does not prove that the old token was authorized
        # for those scopes.
        granted_scopes = set(token_data.get("scopes") or [])
        if set(GMAIL_SCOPES).issubset(granted_scopes):
            credentials = Credentials.from_authorized_user_file(
                str(token_file), GMAIL_SCOPES
            )

    if credentials and credentials.valid:
        return credentials

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    else:
        if not credentials_file.exists():
            raise FileNotFoundError(
                f"Gmail OAuth credentials file not found: {credentials_file}. "
                "Download a Desktop OAuth client JSON file from Google Cloud "
                "and set GMAIL_CREDENTIALS_FILE if needed."
            )
        flow = InstalledAppFlow.from_client_secrets_file(
            str(credentials_file), GMAIL_SCOPES
        )
        credentials = flow.run_local_server(port=0)

    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(credentials.to_json(), encoding="utf-8")
    return credentials


def authorize_gmail() -> None:
    """Complete Gmail OAuth once and cache the token for later CLI runs."""
    _get_credentials()
    _, token_file = _settings()
    print(f"Gmail authorization succeeded. Token saved to {token_file}.")


def _build_service():
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=_get_credentials(), cache_discovery=False)


def _header_value(headers: list[dict[str, str]], wanted_name: str) -> str | None:
    wanted_name = wanted_name.lower()
    for header in headers:
        if header.get("name", "").lower() == wanted_name:
            return header.get("value")
    return None


def _decode_body(data: str | None) -> str:
    if not data:
        return ""
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding).decode(
        "utf-8", errors="replace"
    )


def _plain_text_from_payload(payload: dict[str, Any]) -> str:
    """Prefer plain text and fall back to a readable HTML body."""
    plain_parts: list[str] = []
    html_parts: list[str] = []

    def visit(part: dict[str, Any]) -> None:
        mime_type = part.get("mimeType", "")
        body_text = _decode_body((part.get("body") or {}).get("data"))
        if mime_type == "text/plain" and body_text:
            plain_parts.append(body_text)
        elif mime_type == "text/html" and body_text:
            html_parts.append(body_text)
        for child in part.get("parts") or []:
            visit(child)

    visit(payload)
    if plain_parts:
        return "\n".join(plain_parts).strip()

    if html_parts:
        text = re.sub(r"<[^>]+>", " ", "\n".join(html_parts))
        return html.unescape(re.sub(r"\s+", " ", text)).strip()

    return ""


def search_messages(query: str, max_results: int = 10) -> dict[str, Any]:
    """Search Gmail and return useful message metadata and readable bodies."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("Gmail search query cannot be empty")
    if not isinstance(max_results, int) or isinstance(max_results, bool):
        raise ValueError("Gmail max_results must be an integer")
    if not 1 <= max_results <= 25:
        raise ValueError("Gmail max_results must be between 1 and 25")

    service = _build_service()
    response = (
        service.users()
        .messages()
        .list(userId="me", q=query.strip(), maxResults=max_results)
        .execute()
    )

    messages = []
    for message in response.get("messages", []):
        full_message = (
            service.users()
            .messages()
            .get(userId="me", id=message["id"], format="full")
            .execute()
        )
        payload = full_message.get("payload") or {}
        headers = payload.get("headers") or []
        messages.append(
            {
                "id": full_message.get("id"),
                "thread_id": full_message.get("threadId"),
                "label_ids": full_message.get("labelIds", []),
                "snippet": full_message.get("snippet", ""),
                "subject": _header_value(headers, "Subject"),
                "from": _header_value(headers, "From"),
                "to": _header_value(headers, "To"),
                "date": _header_value(headers, "Date"),
                "body": _plain_text_from_payload(payload),
            }
        )

    return {
        "query": query.strip(),
        "result_size_estimate": response.get("resultSizeEstimate", 0),
        "messages": messages,
    }


def send_message(
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
) -> dict[str, Any]:
    """Send one plain-text email from the authenticated Gmail account."""
    if not isinstance(to, str) or not to.strip():
        raise ValueError("Gmail recipient cannot be empty")
    if not isinstance(subject, str) or not subject.strip():
        raise ValueError("Gmail subject cannot be empty")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("Gmail body cannot be empty")

    message = EmailMessage()
    message["To"] = to.strip()
    message["Subject"] = subject.strip()
    if cc and cc.strip():
        message["Cc"] = cc.strip()
    if bcc and bcc.strip():
        message["Bcc"] = bcc.strip()
    message.set_content(body)

    encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    sent_message = (
        _build_service()
        .users()
        .messages()
        .send(userId="me", body={"raw": encoded_message})
        .execute()
    )

    return {
        "message_id": sent_message.get("id"),
        "thread_id": sent_message.get("threadId"),
        "to": to.strip(),
        "subject": subject.strip(),
    }
