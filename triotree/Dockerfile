# triotr.ee MCP server (our own code, server.py) — edits profile.json in the triotr.ee repo via the GitHub API.
# Serves streamable HTTP on 0.0.0.0:8080/mcp
#
# Pinned: base image digest + full lockfile. See README.md to update.
FROM python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337

WORKDIR /app

COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --no-deps -r /tmp/requirements.lock \
 && pip check \
 && rm /tmp/requirements.lock

COPY server.py .

USER nobody
CMD ["python", "server.py"]
