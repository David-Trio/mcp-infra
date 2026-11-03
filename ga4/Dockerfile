# GA4 MCP server (stdio-only, no Dockerfile upstream) + stdio→HTTP bridge.
# Serves SSE on 0.0.0.0:8080/sse
#
# EVERYTHING here is pinned (base image digest + full lockfiles). Nothing updates
# unless someone edits a version and regenerates the lock — see README.md.
#
# The two tools live in SEPARATE virtualenvs: they only talk over stdio, and
# their `mcp` SDK requirements conflict (analytics-mcp>=0.8 needs mcp>=2.3,
# mcp-proxy>=0.13 needs mcp<2). Installed together, pip silently backtracks to
# an mcp-proxy that crashes on import (the 2026-11-03 outage).
FROM python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337

COPY analytics-mcp.lock mcp-proxy.lock /tmp/

RUN python -m venv /opt/analytics-mcp \
 && /opt/analytics-mcp/bin/pip install --no-cache-dir --no-deps -r /tmp/analytics-mcp.lock \
 && /opt/analytics-mcp/bin/pip check \
 && python -m venv /opt/mcp-proxy \
 && /opt/mcp-proxy/bin/pip install --no-cache-dir --no-deps -r /tmp/mcp-proxy.lock \
 && /opt/mcp-proxy/bin/pip check \
 && rm /tmp/*.lock

CMD ["/opt/mcp-proxy/bin/mcp-proxy", "--host=0.0.0.0", "--port=8080", "--pass-environment", "--", "/opt/analytics-mcp/bin/analytics-mcp"]
