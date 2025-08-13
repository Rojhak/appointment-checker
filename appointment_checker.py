#!/usr/bin/env python3
"""
appointment_checker.py
~~~~~~~~~~~~~~~~~~~~~~

This module provides a simple monitoring script that queries the Copenhagen City
Hall wedding appointment page for available time slots.  The script polls
FrontDesk's time selection URL at a regular interval (default: every 10
minutes) and examines the returned HTML to determine whether any date up to a
specified cut‑off has open appointment times.  When available dates are
detected, the script prints a notification message to stdout so that a user
watching the logs can act promptly and complete the booking.

This implementation avoids any direct booking or reservation logic – it only
reads the publicly available appointment page and reports on the state of
appointments.  The program is intended to be run on a server, in a cron job,
or as a GitHub Actions workflow to keep track of newly released time slots.

Usage example:

```
python appointment_checker.py --url <time_selection_url> \
                             --until 2025-08-28 \
                             --interval 600
```

Dependencies:
    - requests
    - beautifulsoup4

These can be installed via pip:

```
pip install requests beautifulsoup4
```

Note: The FrontDesk reservation site may block non‑browser clients.  A
User-Agent header is set to mimic a browser and avoid simple blocks.  If
additional cookies or headers are required in the future, extend the
``fetch_page`` function accordingly.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
import time
from typing import List, Optional, Tuple

import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
from bs4 import BeautifulSoup


def fetch_page(url: str) -> str:
    """Retrieve the HTML content of the given URL.

    A custom User-Agent is supplied so that the request resembles a typical
    browser.  If the request fails, an exception will be propagated to the
    caller.

    Args:
        url: The absolute URL to fetch.

    Returns:
        The decoded HTML content as a string.

    Raises:
        requests.HTTPError: If the HTTP request returns a non‑success status.
        requests.RequestException: For network related errors.
    """
    headers = {
        # Pretend to be a modern Firefox browser on Linux to avoid simple bots
        # blocking based on User-Agent.
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) "
            "Gecko/20100101 Firefox/117.0"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    return response.text


def parse_available_dates(
    html: str, cutoff: _dt.date
) -> List[Tuple[_dt.date, Optional[str]]]:
    """Parse the appointment page for available dates before the cutoff.

    The FrontDesk appointment page lists each date along with a message.  If
    there are no remaining time slots, the message contains the phrase
    "No more available time slots".  Otherwise, the message typically lists
    the available time ranges or may include a booking link.  This parser
    iterates over all date headings in the HTML, interprets the date, and
    collects those dates that have at least one available time.

    Args:
        html: Raw HTML of the time selection page.
        cutoff: Inclusive cutoff date; only dates on or before this date are
            returned.

    Returns:
        A list of (date, status_text) tuples for each date with available
        appointments.  If `status_text` is None, it means that a date heading
        was found but no accompanying status was located (this should be rare).
    """
    soup = BeautifulSoup(html, "html.parser")
    available: List[Tuple[_dt.date, Optional[str]]] = []

    # The page uses heading tags (h3) for each date.  Extract them and inspect
    # the subsequent sibling to determine availability.
    for heading in soup.find_all(["h2", "h3", "h4"]):
        text = heading.get_text(strip=True)
        # Only process headings that contain a year.
        if "202" not in text:
            continue
        try:
            # Example format: "Wednesday August 27, 2025"
            date_obj = _dt.datetime.strptime(text, "%A %B %d, %Y").date()
        except ValueError:
            continue
        # Skip dates beyond the cutoff.
        if date_obj > cutoff:
            continue
        # Find the next element containing the status.  Typically this is a div
        # or span with text.  We'll look ahead until we find a string and
        # examine its contents.
        status_elem = heading.find_next(string=True)
        status_text = status_elem.strip() if status_elem else None
        # If no status text is present, skip.
        if not status_text:
            continue
        # Identify dates that have available times.
        if "No more available time slots" not in status_text:
            available.append((date_obj, status_text))
    return available


def send_email_notification(available_dates: List[Tuple[_dt.date, Optional[str]]], url: str, cutoff_date: _dt.date):
    """Send an email notification with available appointment details."""
    sender = os.environ.get("APPT_MAIL_SENDER")
    recipient = os.environ.get("APPT_MAIL_RECIPIENT")
    smtp_server = os.environ.get("APPT_MAIL_SMTP_SERVER")
    smtp_port = int(os.environ.get("APPT_MAIL_SMTP_PORT", "587"))
    smtp_user = os.environ.get("APPT_MAIL_SMTP_USER") or sender
    smtp_password = os.environ.get("APPT_MAIL_SMTP_PASSWORD")
    missing = [k for k, v in [
        ("APPT_MAIL_SENDER", sender),
        ("APPT_MAIL_RECIPIENT", recipient),
        ("APPT_MAIL_SMTP_SERVER", smtp_server),
        ("APPT_MAIL_SMTP_PASSWORD", smtp_password)
    ] if not v]
    if missing:
        print(f"[WARN] Email not sent: missing environment variables: {', '.join(missing)}", file=sys.stderr)
        return
    sender = str(sender)
    recipient = str(recipient)
    smtp_server = str(smtp_server)
    smtp_user = str(smtp_user)
    smtp_password = str(smtp_password)
    subject = "Appointment Slot Found!"
    body = f"Available appointments up to {cutoff_date} were found at {url}:\n\n"
    for date_obj, status in available_dates:
        date_str = date_obj.strftime("%A %B %d, %Y")
        message = status or "Available"
        body += f"- {date_str}: {message}\n"
    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    try:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.sendmail(sender, [recipient], msg.as_string())
        print(f"[INFO] Notification email sent to {recipient}.")
    except Exception as e:
        print(f"[ERROR] Failed to send email: {e}", file=sys.stderr)


def monitor_appointments(
    url: str, cutoff_str: str, interval: int
) -> None:
    """Continuously poll the appointment page for availability.

    This function runs an infinite loop, fetching the page at the specified
    interval.  It parses the page for available dates and prints any
    discoveries.  Should an exception occur (network failure, parse error,
    etc.), the error is logged and the loop continues.

    Args:
        url: The FrontDesk time selection URL to monitor.
        cutoff_str: A date string (YYYY-MM-DD) indicating the latest date to
            consider when searching for appointments.
        interval: Polling interval in seconds.
    """
    cutoff_date = _dt.datetime.strptime(cutoff_str, "%Y-%m-%d").date()
    print(f"Monitoring appointments until {cutoff_date} ...")
    while True:
        try:
            html = fetch_page(url)
            available_dates = parse_available_dates(html, cutoff_date)
            timestamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if available_dates:
                print(f"[{timestamp}] Found available appointments:")
                for date_obj, status in available_dates:
                    date_str = date_obj.strftime("%A %B %d, %Y")
                    message = status or "Available"
                    print(f"  * {date_str}: {message}")
                print("Act quickly to book your preferred slot via the web interface.")
                send_email_notification(available_dates, url, cutoff_date)
            else:
                print(f"[{timestamp}] No appointments available up to {cutoff_date}.")
        except Exception as exc:
            err_time = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{err_time}] Error while checking appointments: {exc}", file=sys.stderr)
        # Sleep before next check
        time.sleep(interval)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Monitor Copenhagen City Hall wedding appointment availability."
    )
    parser.add_argument(
        "--url",
        required=True,
        help=(
            "The full FrontDesk time selection URL to monitor.  You can obtain this "
            "URL by navigating to the 'Select date and time for the wedding "
            "ceremony' page in your browser and copying the address."
        ),
    )
    parser.add_argument(
        "--until",
        dest="cutoff",
        default=(
            _dt.date.today() + _dt.timedelta(days=30)
        ).strftime("%Y-%m-%d"),
        help=(
            "Latest date (inclusive) to consider when searching for appointments. "
            "Format: YYYY-MM-DD. Defaults to 30 days from today."
        ),
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=600,
        help=(
            "Polling interval in seconds between checks (default: 600 seconds / 10 minutes)."
        ),
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    monitor_appointments(args.url, args.cutoff, args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())