FROM python:3.12-slim

# GA4 MCP server (stdio-only, no Dockerfile upstream) + stdio→HTTP bridge.
# Serves SSE on 0.0.0.0:8080/sse
# Pin versions here once verified working, e.g. analytics-mcp==X.Y.Z mcp-proxy==A.B.C
RUN pip install --no-cache-dir analytics-mcp mcp-proxy

CMD ["mcp-proxy", "--host=0.0.0.0", "--port=8080", "--pass-environment", "--", "analytics-mcp"]