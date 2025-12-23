FROM mitmproxy/mitmproxy:latest

# Install psycopg for Scribe database logging
RUN pip install --no-cache-dir psycopg[binary]
