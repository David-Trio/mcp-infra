# mcp-infra

MCP servers (Google Ads, GA4, Search Console, apox, triotr.ee) behind Traefik + token auth, deployed with Dokploy.

| Service | Public host | Image built from |
|---|---|---|
| `google-ads-mcp` → `gads` | `gads.mcp.trioflow.io` | `gads/` (googleads/google-ads-mcp @ pinned commit) |
| `ga4-mcp` → `ga4` | `ga4.mcp.trioflow.io` | `ga4/` (analytics-mcp + mcp-proxy) |
| `gsc-mcp` → `gsc` | `gsc.mcp.trioflow.io` | `gsc/` (AminForou/mcp-gsc @ pinned commit) |
| `apox-mcp` → `apox` | `apox.mcp.trioflow.io` | `apox/` (our own `server.py`, wraps the apox API) |
| `triotree-mcp` → `triotree` | `triotree.mcp.trioflow.io` | `triotree/` (our own `server.py`, edits triotr.ee's `profile.json` via GitHub) |
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
   | `apox/requirements.lock` | `apox/requirements.in` | 3.12 |
   | `triotree/requirements.lock` | `triotree/requirements.in` | 3.12 |

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

## apox MCP

`apox/server.py` is our own code: three tools over the apox API (`crawl`, `get_job`, `fetch_page`). It needs `APOX_BASE_URL`, `APOX_API_TOKEN` (apox's `NITRO_API_TOKEN`) and `APOX_TRIO_AUTH` (sent as the `x-trio-auth` header on every apox call) in `.env`.

- `crawl` takes a URL and an optional `force`. Without `force`, apox returns today's job for the site if there is one. The description tells Claude to set `force` only when the user asks for fresh data. The page fields are fixed in `CRAWL_PAGE_FIELDS`: everything except `html`.
- `get_job` keeps polling up to 180s (`APOX_JOB_WAIT_SECONDS`), because the model can't sleep between calls. It returns as soon as the crawl ends, so only big crawls wait the full time. Each "still running" reply costs a model turn over the whole conversation, so longer is cheaper, but it must stay under the client's tool-call timeout (240s on claude.ai and Desktop, per claude.com/docs/connectors/building), so a whole call is held to 225s (`JOB_CALL_BUDGET_SECONDS`) and the setting is capped at 200s.
- A finished `get_job` returns CSV text, not JSON: a short header, then `# pages.csv` (columns in `PAGE_COLUMNS`), `# redirects.csv`, and `# unfetched-urls.csv` only when apox lists URLs that aren't pages. `content`, `html` and `fetchedAt` are left out, and `contentHash` is cut to 12 characters. CSV fits about 40% more pages than JSON under claude.ai's ~150,000-character tool-result limit. Past that limit, claude.ai saves the result as a file in Claude's code sandbox (when code execution is on), where Claude can analyse it with code. `fetch_page` drops `html`.

## triotr.ee MCP

`triotree/server.py` is our own code. It lets Claude read and edit the triotr.ee link page. It only ever touches `src/data/profile.json` in `David-Trio/triotr.ee`, through the GitHub contents API, so there's no clone on the server. Each save is one commit to `main`, made by "triotr.ee bot", and Cloudflare deploys from there.

It needs these in `.env`: `TRIOTREE_REPO`, `TRIOTREE_BRANCH` (the branch Cloudflare deploys), `TRIOTREE_COMMIT_NAME`, `TRIOTREE_COMMIT_EMAIL` (the bot commit identity), and `TRIOTREE_GITHUB_TOKEN`, a fine-grained token with access to **only** `David-Trio/triotr.ee` and the permission *Contents: Read and write*. The commit message names the editor from mcp-auth's `X-Auth-User` header.

- `get_profile` returns the file, its blob `sha`, and the image files in `src/assets`.
- `update_profile` takes the **whole** new profile, the `sha` it was based on, and a short summary for the commit message. Before it commits, it checks:
  - the shape against the site's `ProfileData` type (`src/types/profile.ts`). Unknown keys are refused, colours must be hex, URLs need a scheme, and social icons must be one of the six the site has;
  - every image field against `src/assets`. Uploads aren't supported; a thumbnail may also be an `https://` URL.

  An unchanged profile makes no commit. A stale `sha` means someone else saved first, so nothing is written and the model is told to fetch the file again.
- The pydantic models in `server.py` mirror `src/types/profile.ts`. **If you add a field to `profile.json` or `ProfileData`, add it here too**: unknown keys are refused, so edits would fail until you do. The same goes for a new social icon. The models write keys in the order they're declared, which matches the current file, so a save round-trips byte for byte.
