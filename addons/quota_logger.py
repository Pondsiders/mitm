"""
Quota Logger - mitmproxy addon to track Anthropic API usage.

Extracts rate limit headers from Anthropic API responses and logs them to CSV.
"""

import csv
import os
from datetime import datetime, timezone
from pathlib import Path

from mitmproxy import http

# Output file - mounted from host
DATA_DIR = Path(os.environ.get("MITM_DATA_DIR", "/data"))
QUOTA_CSV = DATA_DIR / "quota.csv"

# Headers we care about
QUOTA_HEADERS = [
    "anthropic-ratelimit-unified-5h-utilization",
    "anthropic-ratelimit-unified-5h-reset",
    "anthropic-ratelimit-unified-5h-status",
    "anthropic-ratelimit-unified-7d-utilization",
    "anthropic-ratelimit-unified-7d-reset",
    "anthropic-ratelimit-unified-7d-status",
    "anthropic-ratelimit-unified-fallback",
    "anthropic-ratelimit-unified-fallback-percentage",
    "anthropic-ratelimit-unified-overage-status",
]


class QuotaLogger:
    def __init__(self):
        # Ensure CSV exists with header
        if not QUOTA_CSV.exists():
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(QUOTA_CSV, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["timestamp", "request_id"] + QUOTA_HEADERS)

    def response(self, flow: http.HTTPFlow):
        """Called for every response passing through the proxy."""
        # Only care about Anthropic API
        if "api.anthropic.com" not in flow.request.host:
            return

        headers = flow.response.headers

        # Check if this response has quota headers
        if not any(h in headers for h in QUOTA_HEADERS):
            return

        # Extract values
        timestamp = datetime.now(timezone.utc).isoformat()
        request_id = headers.get("request-id", "")
        values = [headers.get(h, "") for h in QUOTA_HEADERS]

        # Log to CSV
        with open(QUOTA_CSV, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([timestamp, request_id] + values)

        # Also print for visibility
        util_5h = headers.get("anthropic-ratelimit-unified-5h-utilization", "?")
        util_7d = headers.get("anthropic-ratelimit-unified-7d-utilization", "?")
        print(f"[quota] 5h: {float(util_5h)*100:.1f}%, 7d: {float(util_7d)*100:.1f}%")


addons = [QuotaLogger()]
