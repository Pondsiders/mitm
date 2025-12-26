FROM mitmproxy/mitmproxy:12.1.2

# Install dependencies for addons
# Using 12.1.x for Python 3.13 (langfuse's API types use pydantic v1 which breaks on 3.14)
# TODO: Upgrade to latest when langfuse/langfuse#9618 is fixed
RUN pip install --no-cache-dir \
    psycopg[binary] \
    redis \
    langfuse \
    streamlit-autorefresh