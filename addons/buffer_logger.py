"""
Buffer Logger - mitmproxy addon to record raw API traffic to Redis.

Captures the complete request/response JSON for every Anthropic API call.
No filtering, no scrubbing—raw bytes for debugging and analysis.

Uses Redis Streams for time-ordered, queryable storage.
"""

import json
import os
from datetime import datetime, timezone

import redis
from mitmproxy import http

# Redis connection
REDIS_URL = os.environ.get("REDIS_URL", "redis://172.17.0.1:6379")
STREAM_KEY = "mitm:api_traffic"

# Connect to Redis
r = redis.from_url(REDIS_URL, decode_responses=True)


class BufferLogger:
    def request(self, flow: http.HTTPFlow):
        """Record the raw request to Redis."""
        if "/v1/messages" not in flow.request.path:
            return

        try:
            # Store raw request body as string (preserve exact bytes)
            raw_body = flow.request.content.decode("utf-8", errors="replace")

            # Parse for metadata extraction only
            try:
                body = json.loads(raw_body)
                model = body.get("model", "unknown")
                msg_count = len(body.get("messages", []))
            except json.JSONDecodeError:
                model = "unknown"
                msg_count = 0

            # Add to Redis Stream
            entry_id = r.xadd(STREAM_KEY, {
                "type": "request",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "flow_id": str(flow.id),
                "model": model,
                "message_count": str(msg_count),
                "size": str(len(flow.request.content)),
                "body": raw_body,
            })

            # Store entry ID on flow for response correlation
            flow.metadata["buffer_entry_id"] = entry_id

            print(f"[buffer] Request: {model} ({len(flow.request.content)} bytes) -> {entry_id}")

        except Exception as e:
            print(f"[buffer] Request error: {e}")

    def response(self, flow: http.HTTPFlow):
        """Record the raw response to Redis."""
        if "/v1/messages" not in flow.request.path:
            return

        try:
            # Get correlation info from request
            request_entry_id = flow.metadata.get("buffer_entry_id", "unknown")

            # Store raw response
            raw_body = flow.response.content.decode("utf-8", errors="replace") if flow.response.content else ""
            content_type = flow.response.headers.get("content-type", "unknown")

            # Add to Redis Stream
            entry_id = r.xadd(STREAM_KEY, {
                "type": "response",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "flow_id": str(flow.id),
                "request_entry_id": request_entry_id,
                "status_code": str(flow.response.status_code),
                "content_type": content_type,
                "size": str(len(flow.response.content) if flow.response.content else 0),
                "body": raw_body,
            })

            print(f"[buffer] Response: {flow.response.status_code} ({len(raw_body)} bytes) -> {entry_id}")

        except Exception as e:
            print(f"[buffer] Response error: {e}")


addons = [BufferLogger()]
