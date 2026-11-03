# Google Ads MCP — built from a pinned upstream commit with pinned deps.
# Replaces upstream's Dockerfile, which floats python:3.11-slim, uv:latest and
# resolves dependencies fresh on every build (no lockfile). See README.md to update.
FROM python:3.11.18-slim-bookworm@sha256:94a8fee35252d06ab75856f15f12c7bae67b10e8a6da93c7fded82b2610926cd

# https://github.com/googleads/google-ads-mcp — v0.0.6
ARG GADS_COMMIT=7452a9d49e3d350403f2610cd7125c60f6d05264

COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --no-deps -r /tmp/requirements.lock \
 && pip install --no-cache-dir --no-deps --no-build-isolation \
      "https://github.com/googleads/google-ads-mcp/archive/${GADS_COMMIT}.tar.gz" \
 && pip check \
 && rm /tmp/requirements.lock

# Carried over from upstream's Dockerfile @ GADS_COMMIT: Codex CLI 0.146 reports
# the RFC 9207 `iss` value as missing from the local OAuth callback. Fails the
# build loudly if fastmcp changes, so it gets re-checked on any bump.
RUN python -c "from importlib.metadata import version; from pathlib import Path; assert version('fastmcp').startswith('4.'); p = Path('/usr/local/lib/python3.11/site-packages/fastmcp/server/auth/oauth_proxy/proxy.py'); s = p.read_text(); old = 'metadata.authorization_response_iss_parameter_supported = True'; new = 'metadata.authorization_response_iss_parameter_supported = False'; assert s.count(old) == 1; p.write_text(s.replace(old, new))"

EXPOSE 8080
CMD ["google-ads-mcp"]
