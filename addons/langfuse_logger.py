"""
Langfuse Logger - mitmproxy addon to stream API traffic to Langfuse.

Captures Anthropic API requests and responses as Langfuse generations,
giving us proper LLM observability with token tracking, model info, etc.

Primary goal: "WTF just happened to Alpha" — see the full request and response
tied together as one transaction.

Secondary goal: Cost tracking.

Uses env vars: LANGFUSE_SECRET_KEY, LANGFUSE_PUBLIC_KEY, LANGFUSE_BASE_URL
"""

import json

from mitmproxy import http
from langfuse import get_client


# Initialize Langfuse client (SDK v3)
langfuse = get_client()


def extract_trace_name(request_data: dict) -> str:
    """
    Extract something identifiable for the trace name.

    Goal: scanning the trace list, you can find the weird one.
    Format: "model: preview of what's being asked..."
    """
    model = request_data.get("model", "unknown")
    # Shorten model name for readability
    model_short = model.replace("claude-", "").replace("-20", "-")

    messages = request_data.get("messages", [])
    system = request_data.get("system", "")

    # Try system prompt first (often identifies the agent type)
    if system:
        if isinstance(system, str):
            preview = system[:60].replace('\n', ' ')
        elif isinstance(system, list):
            # System can be list of content blocks
            for block in system:
                if isinstance(block, dict) and block.get("type") == "text":
                    preview = block.get("text", "")[:60].replace('\n', ' ')
                    break
            else:
                preview = "system-blocks"
        else:
            preview = str(system)[:60]
        return f"{model_short}: {preview}..."

    # Fall back to last user message (what's actually being asked)
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                preview = content[:60].replace('\n', ' ')
                return f"{model_short}: {preview}..."
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        preview = block.get("text", "")[:60].replace('\n', ' ')
                        return f"{model_short}: {preview}..."

    return f"{model_short}: (no preview)"


def parse_sse_stream(raw: str) -> tuple[str, dict | None]:
    """
    Parse Anthropic SSE stream and extract:
    - Reassembled text content from content_block_delta events
    - Usage stats from message_delta event

    Returns (text_content, usage_dict)
    """
    text_parts = []
    usage = None

    # Split into SSE events (double newline separated, but also handle data: lines)
    for line in raw.split('\n'):
        if not line.startswith('data: '):
            continue

        try:
            data = json.loads(line[6:])  # Skip 'data: ' prefix
            event_type = data.get('type')

            if event_type == 'content_block_delta':
                delta = data.get('delta', {})
                if delta.get('type') == 'text_delta':
                    text_parts.append(delta.get('text', ''))

            elif event_type == 'message_delta':
                # Final usage stats come in message_delta
                delta_usage = data.get('usage', {})
                if delta_usage:
                    usage = usage or {}
                    usage['output_tokens'] = delta_usage.get('output_tokens')

            elif event_type == 'message_start':
                # Initial usage (input tokens) comes in message_start
                msg = data.get('message', {})
                msg_usage = msg.get('usage', {})
                if msg_usage:
                    usage = usage or {}
                    usage['input_tokens'] = msg_usage.get('input_tokens')
                    usage['cache_creation_input_tokens'] = msg_usage.get('cache_creation_input_tokens')
                    usage['cache_read_input_tokens'] = msg_usage.get('cache_read_input_tokens')

        except json.JSONDecodeError:
            continue

    return ''.join(text_parts), usage


class LangfuseLogger:
    """Log Anthropic API traffic to Langfuse as generations."""

    def __init__(self):
        self.pending_flows = {}

    def request(self, flow: http.HTTPFlow):
        """Capture request, store for later matching with response."""
        if "/v1/messages" not in flow.request.path:
            return

        try:
            body = json.loads(flow.request.content)
            # Store the whole request — clean isn't the goal, true is the goal
            self.pending_flows[flow.id] = body
        except Exception as e:
            print(f"[langfuse] request_parse_error: {e}")

    def response(self, flow: http.HTTPFlow):
        """Match response with request and log to Langfuse."""
        if "/v1/messages" not in flow.request.path:
            return

        request_data = self.pending_flows.pop(flow.id, None)
        if not request_data:
            print(f"[langfuse] no pending request for flow {flow.id}")
            return

        try:
            raw = flow.response.content.decode("utf-8", errors="replace")

            if request_data.get("stream"):
                # Parse SSE stream to extract text and usage
                output, usage = parse_sse_stream(raw)
            else:
                # Non-streaming: parse JSON response directly
                response_body = json.loads(raw)
                content = response_body.get("content", [])
                # Extract text from content blocks
                output = ''.join(
                    block.get('text', '')
                    for block in content
                    if block.get('type') == 'text'
                )
                usage = response_body.get("usage", {})

            # Create a single generation — one API transaction, one trace
            # The generation IS the trace (no parent span needed)
            trace_name = extract_trace_name(request_data)

            with langfuse.start_as_current_observation(
                as_type="generation",
                name=trace_name,
                model=request_data.get("model"),
                input=request_data,  # Full request: system, messages, tools — the whole truth
                output=output,       # What Alpha said
                model_parameters={
                    "max_tokens": request_data.get("max_tokens"),
                    "temperature": request_data.get("temperature"),
                    "stream": request_data.get("stream"),
                },
                metadata={
                    "source": "mitmproxy",
                    "flow_id": str(flow.id),
                    "status_code": flow.response.status_code,
                },
            ) as generation:
                # Set usage with correct keys for cost tracking
                if usage:
                    generation.update(
                        usage_details={
                            "input": usage.get("input_tokens"),
                            "output": usage.get("output_tokens"),
                            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
                            "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
                        }
                    )

            langfuse.flush()
            print(f"[langfuse] {trace_name} ({len(output)} chars)")

        except Exception as e:
            print(f"[langfuse] response_parse_error: {e}")


addons = [LangfuseLogger()]
