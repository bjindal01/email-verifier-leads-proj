#!/usr/bin/env python3
"""Poll Gmail for new senders, verify them, then log a Salesforce Lead and Slack alert.

Requires a Gmail app password (Google Account → Security → 2-Step Verification → App passwords).

Usage:
    python3 process_gmail.py
    python3 process_gmail.py --watch
    python3 process_gmail.py --sandbox --once
"""

from __future__ import annotations

import argparse
import imaplib
import os
import sys
import time
from email import message_from_bytes
from email.header import decode_header, make_header
from email.utils import parseaddr
from typing import List, Optional, Tuple

from verify_email import ENV_FILE, load_env, process_address

SKIP_LOCAL_PARTS = {
    "mailer-daemon",
    "mailerdaemon",
    "noreply",
    "no-reply",
    "donotreply",
    "do-not-reply",
    "notifications",
    "notify",
    "bounce",
    "postmaster",
}


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def decode_header_value(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def sender_email(raw_from: str) -> str:
    _, address = parseaddr(raw_from or "")
    return address.strip().lower()


def should_skip(address: str, inbox_address: str) -> bool:
    if not address or "@" not in address:
        return True
    if address == inbox_address.lower():
        return True
    local = address.split("@", 1)[0]
    return local in SKIP_LOCAL_PARTS or local.startswith("noreply")


def ensure_label(imap: imaplib.IMAP4_SSL, label: str) -> None:
    status, _ = imap.create(label)
    if status not in {"OK", "NO"}:
        raise RuntimeError(f"Could not create Gmail label {label!r}: {status}")


def search_unprocessed(imap: imaplib.IMAP4_SSL, label: str) -> List[bytes]:
    status, data = imap.search(None, "X-GM-RAW", f"-label:{label}")
    if status != "OK":
        status, data = imap.search(None, "UNSEEN")
    if status != "OK":
        raise RuntimeError("Gmail search failed")
    ids = data[0].split() if data and data[0] else []
    return ids


def mark_processed(imap: imaplib.IMAP4_SSL, message_id: bytes, label: str) -> None:
    imap.store(message_id, "+FLAGS", "\\Seen")
    try:
        imap.store(message_id, "+X-GM-LABELS", f"({label})")
    except imaplib.IMAP4.error:
        pass


def fetch_envelope(imap: imaplib.IMAP4_SSL, message_id: bytes) -> Tuple[str, str, str]:
    status, data = imap.fetch(message_id, "(RFC822.HEADER)")
    if status != "OK" or not data or not data[0]:
        raise RuntimeError("Failed to fetch message headers")
    raw = data[0][1]
    message = message_from_bytes(raw)
    address = sender_email(decode_header_value(message.get("From")))
    subject = decode_header_value(message.get("Subject"))
    gmail_id = decode_header_value(message.get("Message-ID"))
    return address, subject, gmail_id


def process_mailbox(sandbox: bool, limit: int) -> int:
    address = env("GMAIL_ADDRESS")
    app_password = env("GMAIL_APP_PASSWORD").replace(" ", "")
    label = env("GMAIL_PROCESSED_LABEL", "EmailVerified")
    if not address or not app_password:
        raise RuntimeError("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env")

    imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    try:
        imap.login(address, app_password)
        imap.select("INBOX")
        ensure_label(imap, label)
        message_ids = search_unprocessed(imap, label)
        if limit:
            message_ids = message_ids[-limit:]
        if not message_ids:
            print("No new Gmail messages to verify.")
            return 0

        worst = 0
        seen_senders = set()
        for message_id in message_ids:
            sender, subject, gmail_id = fetch_envelope(imap, message_id)
            if should_skip(sender, address):
                mark_processed(imap, message_id, label)
                print(f"Skipped {sender or '(no sender)'}")
                continue
            if sender in seen_senders:
                mark_processed(imap, message_id, label)
                continue
            seen_senders.add(sender)
            print(f"Verifying sender {sender} ({subject or 'no subject'})")
            code = process_address(
                sender,
                sandbox=sandbox,
                extra={"gmail_subject": subject, "gmail_message_id": gmail_id},
            )
            worst = max(worst, code)
            mark_processed(imap, message_id, label)
        return worst
    finally:
        try:
            imap.close()
        except Exception:
            pass
        try:
            imap.logout()
        except Exception:
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify Gmail senders and log Salesforce Leads / Slack alerts."
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Keep polling for new mail",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process the inbox once and exit (default)",
    )
    parser.add_argument(
        "--sandbox",
        action="store_true",
        help="Use QuickEmailVerification sandbox",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Max messages to process per pass (default 25)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_env(ENV_FILE)
    interval = int(env("GMAIL_POLL_SECONDS", "60") or "60")
    watch = args.watch and not args.once
    try:
        while True:
            code = process_mailbox(sandbox=args.sandbox, limit=args.limit)
            if not watch:
                return code
            time.sleep(max(15, interval))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Stopped Gmail watcher.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
