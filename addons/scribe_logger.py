"""
Scribe Logger - mitmproxy addon to record all conversations to Scribe database.

Captures request/response pairs from the Anthropic API and writes them to Postgres.
Handles streaming SSE responses by parsing event chunks.
"""

import json
import os
import re
import uuid
from datetime import datetime, timezone

import psycopg

from mitmproxy import http

# Database connection
DB_URL = os.environ.get("SCRIBE_DB_URL", "postgresql://scribe:scribe@172.17.0.1:5725/scribe")

# Track conversation context per request
_pending_requests = {}


class ScribeLogger:
    def __init__(self):
        self.conn = None
        self._ensure_connection()

    def _ensure_connection(self):
        """Ensure database connection is alive."""
        try:
            if self.conn is None or self.conn.closed:
                self.conn = psycopg.connect(DB_URL, autocommit=True)
                print("[scribe] Connected to database")
        except Exception as e:
            print(f"[scribe] Database connection failed: {e}")
            self.conn = None

    def _get_or_create_conversation(self, conv_uuid: str, name: str = None) -> int:
        """Get conversation ID, creating if needed."""
        self._ensure_connection()
        if not self.conn:
            return None

        try:
            with self.conn.cursor() as cur:
                # Try to find existing
                cur.execute(
                    "SELECT id FROM scribe.conversations WHERE uuid = %s",
                    (conv_uuid,)
                )
                row = cur.fetchone()
                if row:
                    return row[0]

                # Create new
                cur.execute("""
                    INSERT INTO scribe.conversations (uuid, name, source, created_at)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id
                """, (conv_uuid, name, 'claude-code', datetime.now(timezone.utc)))
                return cur.fetchone()[0]
        except Exception as e:
            print(f"[scribe] Conversation error: {e}")
            return None

    def _insert_message(self, conv_id: int, role: str, content: str, msg_uuid: str = None):
        """Insert a message into the database."""
        self._ensure_connection()
        if not self.conn or not conv_id:
            return

        if not msg_uuid:
            msg_uuid = str(uuid.uuid4())

        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO scribe.messages (conversation_id, uuid, role, content, created_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (uuid) DO NOTHING
                """, (conv_id, msg_uuid, role, content, datetime.now(timezone.utc)))
            print(f"[scribe] Recorded {role} message ({len(content)} chars)")
        except Exception as e:
            print(f"[scribe] Insert failed: {e}")

    def _strip_system_reminders(self, text: str) -> str:
        """Strip <system-reminder> blocks using boundary detection.

        Algorithm:
        1. Find span from first <system-reminder> to last </system-reminder>
        2. Keep everything before and after that span
        3. Clean any straggler blocks from the residue
        """
        # Find first opening tag
        first_open = text.find('<system-reminder>')
        if first_open == -1:
            return text  # No system reminders, return as-is

        # Find last closing tag
        last_close = text.rfind('</system-reminder>')
        if last_close == -1:
            return text  # Malformed, return as-is (safe failure)

        # The span to remove is from first_open to end of last closing tag
        end_of_last_close = last_close + len('</system-reminder>')

        # Keep everything before first_open and after end_of_last_close
        before = text[:first_open].strip()
        after = text[end_of_last_close:].strip()

        # Second pass: clean any straggler system-reminder blocks from after
        after = re.sub(r'<system-reminder>.*?</system-reminder>', '', after, flags=re.DOTALL)
        after = re.sub(r'\n{3,}', '\n\n', after).strip()

        # Combine what's left
        if before and after:
            return f"{before}\n\n{after}"
        return before or after or ""

    def _extract_text_from_content(self, content) -> str:
        """Extract text from content blocks and strip system reminders."""
        if isinstance(content, str):
            return self._strip_system_reminders(content)
        if isinstance(content, list):
            texts = []
            for block in content:
                # Only extract text blocks, skip tool_use and tool_result
                if isinstance(block, dict):
                    block_type = block.get("type", "")
                    if block_type == "text":
                        texts.append(block.get("text", ""))
                    # Skip: tool_use, tool_result, image, etc.
                elif isinstance(block, str):
                    texts.append(block)
            raw_text = "".join(texts)
            return self._strip_system_reminders(raw_text)
        return ""

    def _parse_sse_response(self, body: bytes) -> str:
        """Parse SSE streaming response and extract full assistant message."""
        text_parts = []

        # Decode body
        try:
            content = body.decode("utf-8")
        except:
            return ""

        # Parse SSE events
        for line in content.split("\n"):
            if line.startswith("data: "):
                data = line[6:]  # Remove "data: " prefix
                if data == "[DONE]":
                    continue
                try:
                    event = json.loads(data)
                    event_type = event.get("type", "")

                    # content_block_delta contains the text chunks
                    if event_type == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            text_parts.append(delta.get("text", ""))

                    # message_delta might have stop_reason
                    elif event_type == "message_stop":
                        pass  # End of message

                except json.JSONDecodeError:
                    continue

        return "".join(text_parts)

    def request(self, flow: http.HTTPFlow):
        """Capture the request (user message)."""
        if "/v1/messages" not in flow.request.path:
            return

        try:
            body = json.loads(flow.request.content)
            messages = body.get("messages", [])

            # Get conversation ID from header or generate one
            conv_uuid = flow.request.headers.get("x-conversation-id", str(uuid.uuid4()))

            # Store for response handler
            _pending_requests[flow.id] = {
                "conv_uuid": conv_uuid,
                "messages": messages,
                "is_streaming": body.get("stream", False)
            }

            # Get or create conversation
            conv_id = self._get_or_create_conversation(conv_uuid)
            if not conv_id:
                return

            # Record only the LAST user message that contains actual text (not just tool results)
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    msg_content = msg.get("content", "")

                    # Skip messages that are purely tool_result blocks
                    if isinstance(msg_content, list):
                        has_text = any(
                            isinstance(block, dict) and block.get("type") == "text"
                            for block in msg_content
                        )
                        if not has_text:
                            continue  # Skip this message, look for earlier one with text

                    content = self._extract_text_from_content(msg_content)
                    if content.strip():
                        self._insert_message(conv_id, "human", content.strip())
                        break  # Found a real user message

        except Exception as e:
            print(f"[scribe] Request parse error: {e}")

    def response(self, flow: http.HTTPFlow):
        """Capture the response (assistant message)."""
        if "/v1/messages" not in flow.request.path:
            return

        pending = _pending_requests.pop(flow.id, None)
        if not pending:
            return

        try:
            conv_id = self._get_or_create_conversation(pending["conv_uuid"])
            if not conv_id:
                return

            content = ""
            msg_uuid = None

            if pending.get("is_streaming"):
                # Parse SSE stream
                content = self._parse_sse_response(flow.response.content)
            else:
                # Parse regular JSON response
                body = json.loads(flow.response.content)
                content = self._extract_text_from_content(body.get("content", []))
                msg_uuid = body.get("id")

            if content.strip():
                self._insert_message(conv_id, "assistant", content.strip(), msg_uuid)

        except Exception as e:
            print(f"[scribe] Response parse error: {e}")


addons = [ScribeLogger()]
