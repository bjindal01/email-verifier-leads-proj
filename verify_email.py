#!/usr/bin/env python3
"""Verify an email with QuickEmailVerification, log it, and alert Slack when it looks risky.

Usage:
    python3 verify_email.py bill.lumbergh@gamil.com
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
LOG_CSV = ROOT / "email_verification_log.csv"
ENV_FILE = ROOT / ".env"

QEV_URL = "https://api.quickemailverification.com/v1/verify"
SF_API_VERSION = "v62.0"
DEFAULT_SLACK_CHANNEL = "C02RANF29DX"  # #general

CSV_FIELDS = [
    "checked_at",
    "email",
    "result",
    "reason",
    "risky",
    "disposable",
    "role",
    "accept_all",
    "free",
    "safe_to_send",
    "did_you_mean",
    "flags",
]

RISKY_RESULTS = {"invalid", "unknown"}


def load_env(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def is_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


def nonempty(value: Any) -> bool:
    return bool(str(value or "").strip())


def http_json(
    url: str,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    body = None
    req_headers = {"Accept": "application/json", "User-Agent": "email-verifier/1.0"}
    if headers:
        req_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {redact_url(url)}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Request failed for {redact_url(url)}: {exc.reason}") from exc


def http_form(url: str, data: Dict[str, str], timeout: int = 30) -> Dict[str, Any]:
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=encoded,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "email-verifier/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {redact_url(url)}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Request failed for {redact_url(url)}: {exc.reason}") from exc


def redact_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    if "apikey" in query:
        query["apikey"] = ["REDACTED"]
    if "access_token" in query:
        query["access_token"] = ["REDACTED"]
    return urllib.parse.urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urllib.parse.urlencode(query, doseq=True),
            parts.fragment,
        )
    )


def verify_email(email: str, api_key: str, api_url: str) -> Dict[str, Any]:
    query = urllib.parse.urlencode({"email": email, "apikey": api_key})
    data = http_json(f"{api_url}?{query}", timeout=20)
    if not is_true(data.get("success", True)):
        message = data.get("message") or "QuickEmailVerification request failed"
        raise RuntimeError(message)
    return data


def risk_flags(result: Dict[str, Any]) -> List[str]:
    flags: List[str] = []
    status = str(result.get("result") or "").lower()
    if status in RISKY_RESULTS:
        flags.append(status)
    if is_true(result.get("disposable")):
        flags.append("disposable")
    if is_true(result.get("accept_all")):
        flags.append("accept_all")
    if is_true(result.get("role")):
        flags.append("role")
    if nonempty(result.get("did_you_mean")):
        flags.append("possible_typo")
    if "safe_to_send" in result and not is_true(result.get("safe_to_send")):
        flags.append("not_safe_to_send")
    return flags


def row_from_result(email: str, result: Dict[str, Any], flags: List[str]) -> Dict[str, Any]:
    return {
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "email": email,
        "result": result.get("result") or "",
        "reason": result.get("reason") or "",
        "risky": "yes" if flags else "no",
        "disposable": result.get("disposable", ""),
        "role": result.get("role", ""),
        "accept_all": result.get("accept_all", ""),
        "free": result.get("free", ""),
        "safe_to_send": result.get("safe_to_send", ""),
        "did_you_mean": result.get("did_you_mean") or "",
        "flags": ",".join(flags),
    }


def append_csv(row: Dict[str, Any]) -> None:
    new_file = not LOG_CSV.exists()
    with LOG_CSV.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def append_google_sheet(row: Dict[str, Any]) -> str:
    webapp_url = os.environ.get("GOOGLE_SHEETS_WEBAPP_URL", "").strip()
    if webapp_url:
        http_json(webapp_url, method="POST", payload=row)
        return "web app"

    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "").strip()
    token = os.environ.get("GOOGLE_ACCESS_TOKEN", "").strip()
    if not sheet_id or not token:
        raise RuntimeError(
            "Set GOOGLE_SHEETS_WEBAPP_URL, or GOOGLE_SHEET_ID plus GOOGLE_ACCESS_TOKEN"
        )

    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/"
        f"{urllib.parse.quote('Sheet1!A1', safe='')}:append"
        "?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS"
    )
    http_json(
        url,
        method="POST",
        payload={"values": [[row[field] for field in CSV_FIELDS]]},
        headers={"Authorization": f"Bearer {token}"},
    )
    return f"spreadsheet {sheet_id}"


def salesforce_session() -> Dict[str, str]:
    instance = os.environ.get("SF_INSTANCE_URL", "").strip().rstrip("/")
    token = os.environ.get("SF_ACCESS_TOKEN", "").strip()
    if instance and token:
        return {"instance_url": instance, "access_token": token}

    login_url = (
        os.environ.get("SF_LOGIN_URL", "").strip().rstrip("/")
        or "https://login.salesforce.com"
    )
    client_id = os.environ.get("SF_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SF_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise RuntimeError(
            "Set SF_ACCESS_TOKEN and SF_INSTANCE_URL, or SF_CLIENT_ID and SF_CLIENT_SECRET"
        )

    username = os.environ.get("SF_USERNAME", "").strip()
    password = os.environ.get("SF_PASSWORD", "").strip()
    security_token = os.environ.get("SF_SECURITY_TOKEN", "").strip()
    token_url = f"{login_url}/services/oauth2/token"

    if username and password:
        data = {
            "grant_type": "password",
            "client_id": client_id,
            "client_secret": client_secret,
            "username": username,
            "password": password + security_token,
        }
    else:
        data = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }

    auth = http_form(token_url, data)
    access_token = auth.get("access_token")
    instance_url = (auth.get("instance_url") or instance or "").rstrip("/")
    if not access_token or not instance_url:
        raise RuntimeError(auth.get("error_description") or "Salesforce OAuth failed")
    return {"instance_url": instance_url, "access_token": access_token}


def salesforce_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def lead_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    local_part = (row["email"].split("@")[0] or "unknown")[:80]
    flags = row.get("flags") or ""
    risky = row.get("risky") == "yes"
    description = "\n".join(
        [
            "Email verification (QuickEmailVerification)",
            f"Checked at: {row.get('checked_at')}",
            f"Result: {row.get('result')}",
            f"Reason: {row.get('reason')}",
            f"Risky: {row.get('risky')}",
            f"Safe to send: {row.get('safe_to_send')}",
            f"Disposable: {row.get('disposable')}",
            f"Role: {row.get('role')}",
            f"Accept all: {row.get('accept_all')}",
            f"Did you mean: {row.get('did_you_mean') or ''}",
            f"Flags: {flags}",
        ]
    )
    if row.get("gmail_subject"):
        description += f"\nGmail subject: {row['gmail_subject']}"
    if row.get("gmail_message_id"):
        description += f"\nGmail message id: {row['gmail_message_id']}"
    return {
        "LastName": local_part,
        "Company": "Email Verification",
        "Email": row["email"],
        "LeadSource": "Web",
        "Title": f"{row.get('result')} / {row.get('reason')}".strip(" /"),
        "Rating": "Cold" if risky else "Warm",
        "Description": description,
    }


def upsert_salesforce_lead(row: Dict[str, Any]) -> str:
    session = salesforce_session()
    instance = session["instance_url"]
    token = session["access_token"]
    headers = salesforce_headers(token)
    email = row["email"].replace("\\", "\\\\").replace("'", "\\'")
    query = (
        f"SELECT Id FROM Lead WHERE Email = '{email}' ORDER BY LastModifiedDate DESC LIMIT 1"
    )
    found = http_json(
        f"{instance}/services/data/{SF_API_VERSION}/query?"
        + urllib.parse.urlencode({"q": query}),
        headers=headers,
    )
    records = found.get("records") or []
    payload = lead_payload(row)
    if records:
        lead_id = records[0]["Id"]
        http_json(
            f"{instance}/services/data/{SF_API_VERSION}/sobjects/Lead/{lead_id}",
            method="PATCH",
            payload=payload,
            headers=headers,
        )
        return f"updated {lead_id}"

    created = http_json(
        f"{instance}/services/data/{SF_API_VERSION}/sobjects/Lead",
        method="POST",
        payload=payload,
        headers=headers,
    )
    lead_id = created.get("id") or ""
    if not created.get("success", True) or not lead_id:
        raise RuntimeError(str(created.get("errors") or created))
    return f"created {lead_id}"


def slack_message(email: str, result: Dict[str, Any], flags: List[str]) -> str:
    suggestion = result.get("did_you_mean")
    lines = [
        f":warning: Risky email: `{email}`",
        f"*Result:* {result.get('result')} ({result.get('reason')})",
        f"*Flags:* {', '.join(flags)}",
        f"*Safe to send:* {result.get('safe_to_send')}",
    ]
    if nonempty(suggestion):
        lines.append(f"*Did you mean:* `{suggestion}`")
    return "\n".join(lines)


def post_slack(text: str) -> str:
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if webhook:
        body = json.dumps({"text": text}).encode("utf-8")
        request = urllib.request.Request(
            webhook,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "email-verifier/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code} from Slack webhook: {detail}") from exc
        return "webhook"

    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channel = os.environ.get("SLACK_CHANNEL_ID", DEFAULT_SLACK_CHANNEL).strip()
    if not token:
        raise RuntimeError("Set SLACK_WEBHOOK_URL or SLACK_BOT_TOKEN")

    data = http_json(
        "https://slack.com/api/chat.postMessage",
        method="POST",
        payload={"channel": channel, "text": text, "mrkdwn": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    if not data.get("ok", True):
        raise RuntimeError(data.get("error") or "Slack chat.postMessage failed")
    return f"channel {channel}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify an email with QuickEmailVerification and log / alert on risk."
    )
    parser.add_argument("email", help="Email address to verify")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the QuickEmailVerification JSON response",
    )
    parser.add_argument(
        "--sandbox",
        action="store_true",
        help="Use the sandbox endpoint (mock results, no credits)",
    )
    return parser.parse_args()


def process_address(
    email: str,
    sandbox: bool = False,
    print_json: bool = False,
    extra: Optional[Dict[str, Any]] = None,
) -> int:
    api_key = os.environ.get("QEV_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing QEV_API_KEY. Copy .env.example to .env and add your key.")

    api_url = os.environ.get("QEV_API_URL", QEV_URL).strip() or QEV_URL
    if sandbox:
        api_url = api_url.rstrip("/")
        if not api_url.endswith("/sandbox"):
            api_url = f"{api_url}/sandbox"

    result = verify_email(email, api_key, api_url)
    flags = risk_flags(result)
    row = row_from_result(email, result, flags)
    if extra:
        row.update(extra)

    append_csv(row)
    print(f"Logged to {LOG_CSV.name}")

    try:
        destination = append_google_sheet(row)
        print(f"Logged to Google Sheet ({destination})")
    except RuntimeError as exc:
        print(f"Google Sheet skipped: {exc}")

    try:
        destination = upsert_salesforce_lead(row)
        print(f"Logged to Salesforce Lead ({destination})")
    except RuntimeError as exc:
        print(f"Salesforce skipped: {exc}")

    print(
        f"{email}: {result.get('result')} / {result.get('reason')}"
        + (f" [{row['flags']}]" if flags else " [clean]")
    )
    if nonempty(result.get("did_you_mean")):
        print(f"Did you mean: {result['did_you_mean']}")

    if print_json:
        print(json.dumps(result, indent=2))

    if flags:
        try:
            destination = post_slack(slack_message(email, result, flags))
            print(f"Posted Slack alert ({destination})")
        except RuntimeError as exc:
            print(f"Slack alert skipped: {exc}")
        return 2

    return 0


def main() -> int:
    args = parse_args()
    load_env(ENV_FILE)
    try:
        return process_address(args.email, sandbox=args.sandbox, print_json=args.json)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
