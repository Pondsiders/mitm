FROM mitmproxy/mitmproxy:latest

# Install dependencies for addons
RUN pip install --no-cache-dir psycopg[binary] redis
