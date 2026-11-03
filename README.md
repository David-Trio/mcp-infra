# mcp-infra

MCP servers (Google Ads, GA4, Search Console) behind Traefik + token auth, deployed with Dokploy.

| Service | Public host | Image built from |
|---|---|---|
| `google-ads-mcp` → `gads` | `gads.mcp.trioflow.io` | `gads/` (googleads/google-ads-mcp @ pinned commit) |
| `ga4-mcp` → `ga4` | `ga4.mcp.trioflow.io` | `ga4/` (analytics-mcp + mcp-proxy) |
| `gsc-mcp` → `gsc` | `gsc.mcp.trioflow.io` | `gsc/` (AminForou/mcp-gsc @ pinned commit) |
| `mcp-auth-app` → `mcp-auth` | Traefik forward-auth | `auth/` |

Each public service is an nginx logging proxy (`logging-proxy/`) in front of the real server.

## Everything is pinned — updates are manual

Nothing changes on a rebuild unless a version is edited here. The 2026-11-03 GA4 outage was caused by an unpinned `pip install` resolving a newly released `mcp` SDK that broke `mcp-proxy`.

What is pinned, and where:

- **Base images**: exact tag **and** `@sha256:` digest in each `Dockerfile` `FROM` line.
- **Upstream source**: commit SHA in `ARG GADS_COMMIT` / `ARG GSC_COMMIT`.
- **Python packages, including transitive deps**: `*.lock` files, installed with `pip install --no-deps` followed by `pip check`. If a lock is incomplete, the **build fails** instead of silently pulling something new.
- **Traefik** is managed by Dokploy and is not part of this stack.

## How to update

Requires [`uv`](https://docs.astral.sh/uv/).

### A Python package

1. Edit the matching `.in` file. Direct pins such as `analytics-mcp==0.8.0` live there.
2. Regenerate the lock from the repo root. Use the Python version from that service's `FROM` line:

   ```sh
   uv pip compile --python-platform x86_64-unknown-linux-gnu --no-header \
     --python-version 3.12 ga4/analytics-mcp.in -o ga4/analytics-mcp.lock
   ```

   | Lock | `.in` | Python |
   |---|---|---|
   | `auth/requirements.lock` | `auth/requirements.in` | 3.11 |
   | `gads/requirements.lock` | `gads/requirements.in` | 3.11 |
   | `ga4/analytics-mcp.lock` | `ga4/analytics-mcp.in` | 3.12 |
   | `ga4/mcp-proxy.lock` | `ga4/mcp-proxy.in` | 3.12 |
   | `gsc/requirements.lock` | `gsc/requirements.in` | 3.13 |

   Add `--upgrade` to move every transitive dep to its latest version. Add `--upgrade-package <name>` to move only one.
3. Review the lock diff, rebuild locally (`docker compose build <service>`), then deploy.

### An upstream server (gads / gsc)

1. Change `ARG GADS_COMMIT` / `ARG GSC_COMMIT` to the new commit SHA.
2. Copy that commit's `pyproject.toml` dependencies into `gads/requirements.in` / `gsc/requirements.in`, then regenerate the lock as above.
3. **gads only:** check upstream's `Dockerfile` at the new commit for changes to the `fastmcp` patch step, and mirror them in `gads/Dockerfile`.

### A base image

Find the new tag's digest, e.g. `docker buildx imagetools inspect python:3.12.17-slim-bookworm`, then update both the tag and the `@sha256:` in `FROM`. If the Python minor version changes, also regenerate that service's lock with the new `--python-version`. For gads, the `fastmcp` patch path contains `python3.11`.

## Gotcha: GA4 needs two virtualenvs

`analytics-mcp` (needs `mcp>=2.3`) and `mcp-proxy` (needs `mcp<2`) can't share an environment. They only communicate over stdio, so `ga4/Dockerfile` installs each into its own venv. Keep them separate.
