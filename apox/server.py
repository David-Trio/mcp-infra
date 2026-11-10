"""apox MCP server: three tools (crawl, get_job, fetch_page) over apox's JSON API.

Serves streamable HTTP on 0.0.0.0:8080/mcp. Traefik + mcp-auth handle client
auth; this process holds apox's own bearer token, which Claude never sees.

Kept deliberately narrow:
- crawl takes only a URL. Page-field selection, delays, excludes etc. are
  fixed here so the model can't (and needn't) tune them.
- get_job waits on the server (up to JOB_WAIT_SECONDS) because the model has
  no way to sleep between calls; fewer, slower polls instead of a busy loop.
- html and content are stripped from results to keep them small enough for
  the model's context. content is still requested from apox so the job is
  reusable if we expose it later.
"""

import asyncio
import inspect
import json
import os
import time
from typing import Annotated
from urllib.parse import quote

import httpx
from pydantic import Field
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

APOX_BASE_URL = os.environ.get("APOX_BASE_URL", "").rstrip("/")
APOX_API_TOKEN = os.environ.get("APOX_API_TOKEN", "")
# Checked in front of apox (not by apox's own code); requests without it are rejected.
APOX_TRIO_AUTH = os.environ.get("APOX_TRIO_AUTH", "")
if not APOX_BASE_URL or not APOX_API_TOKEN or not APOX_TRIO_AUTH:
    raise SystemExit("APOX_BASE_URL, APOX_API_TOKEN and APOX_TRIO_AUTH must be set")

# Every crawl page field apox offers except html (see apox crawl-request.ts).
CRAWL_PAGE_FIELDS = ["url", "title", "metaDescription", "h1", "content", "contentHash", "fetchedAt"]
# Requested from apox but never returned to the model.
STRIPPED_PAGE_FIELDS = {"content", "html"}

JOB_WAIT_SECONDS = 30
JOB_POLL_SECONDS = 3

# The model reads these to choose between our tools and its built-in web
# fetch / search / code execution, so they say when to prefer apox, not just
# what it does. Keep them in line with apox's actual behaviour.
INSTRUCTIONS = """\
apox is Trio's own website crawler and page fetcher. Use it for any task that needs \
the content or structure of a website: reading a page, auditing or reviewing a site, \
listing a site's pages, checking titles, meta descriptions, H1s, duplicate content or \
redirects.

Prefer apox over built-in web fetch, web search or writing scraping code whenever you \
have a URL: it fetches pages server-side with SEO metadata included, and it crawls \
whole sites, which those tools can't.

- One page, or a few known pages: fetch_page.
- A whole site, or "all pages / every blog post / site structure": crawl, then get_job. \
Don't map a site by fetching pages one by one or guessing URLs.
- After a crawl, fetch_page the specific pages whose content you need to read.

Crawl flow: crawl returns a job_id. Call get_job with it. get_job waits up to 30 \
seconds by itself; while status is "running", call get_job again immediately with the \
same job_id. Don't wait first and don't start a new crawl. Stop when status is \
"finished", "failed" or "cancelled".\
"""

mcp = MCPServer("apox", instructions=INSTRUCTIONS)

# Read-only, external web: lets clients auto-approve without a prompt.
READ_ONLY_WEB = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)


def tool(title: str):
    """Register a tool. The SDK sends docstrings with their source indentation; cleandoc strips it."""
    def register(fn):
        return mcp.tool(
            title=title,
            description=inspect.cleandoc(fn.__doc__ or ""),
            annotations=READ_ONLY_WEB,
            # Plain text only: structured output would send every result twice.
            structured_output=False,
        )(fn)
    return register
client = httpx.AsyncClient(
    base_url=APOX_BASE_URL,
    # Sent on every call, not just crawl: whatever checks x-trio-auth sits in
    # front of all of apox's routes.
    headers={"authorization": f"Bearer {APOX_API_TOKEN}", "x-trio-auth": APOX_TRIO_AUTH},
    timeout=httpx.Timeout(60.0, connect=10.0),
)


def _dump(value: object) -> str:
    """Compact JSON: whitespace costs model tokens on large crawls."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


async def _apox(method: str, path: str, body: dict | None = None) -> dict:
    """Call apox and return its JSON, turning any failure into a ToolError the model can read."""
    try:
        response = await client.request(method, path, json=body)
    except httpx.HTTPError as exc:
        raise ToolError(f"apox unreachable: {type(exc).__name__}") from exc
    if response.status_code >= 400:
        try:
            message = response.json().get("message") or response.text
        except ValueError:
            message = response.text
        raise ToolError(f"apox error {response.status_code}: {message[:500]}")
    return response.json()


async def _call(function: str, data: dict) -> dict:
    return await _apox("POST", "/api/v1", {"function": function, "data": data})


@tool("Crawl website")
async def crawl(
    url: Annotated[str, Field(description="The site's homepage, e.g. https://example.com")],
) -> str:
    """Crawl an entire website: every page's title, meta description and H1s, plus all URLs and redirects.

    Use this whenever a task covers a whole site or many of its pages: site
    audits, SEO reviews of titles / meta descriptions / H1s, finding all pages
    or blog posts, content inventories, duplicate-content checks, redirect
    checks. Prefer it over fetching pages one at a time or guessing URLs: it
    discovers pages from the sitemap and internal links.

    Returns a job_id at once; the crawl runs in the background. Next, call
    get_job with the job_id. A site already crawled today returns that same
    job, so its results are ready immediately.
    """
    result = await _call("crawl", {"url": url, "pages": CRAWL_PAGE_FIELDS})
    return _dump({"job_id": result["jobId"], "next": "Call get_job with this job_id."})


@tool("Get crawl results")
async def get_job(
    job_id: Annotated[str, Field(description="The job_id returned by crawl")],
) -> str:
    """Get the results of a crawl started with crawl.

    This call itself waits up to 30 seconds for the crawl to finish. While
    status is "running" (with pagesDone / pagesTotal progress), call get_job
    again immediately with the same job_id: don't wait or sleep first, and
    don't start a new crawl. Large sites can take several minutes. Stop when
    status is "finished", "failed" (see error) or "cancelled".

    Finished result:
    - pages: one entry per page that answered 2xx, with url, title,
      metaDescription, h1 (a string, or a list when the page has several),
      contentHash and fetchedAt. A missing field means the page has none.
      Pages sharing a contentHash have identical body content. Entries whose
      url ends in #header / #footer are the site-wide header and footer,
      not real pages.
    - urls: every page URL of the site, sorted.
    - redirects: URLs that redirect elsewhere, as {from, to, status}.
    Page body text isn't included: use fetch_page on the pages you need to read.
    Results are kept until midnight UK time.
    """
    path = f"/crawl/{quote(job_id, safe='')}"
    deadline = time.monotonic() + JOB_WAIT_SECONDS
    while True:
        job = await _apox("GET", path)
        if job.get("status") != "running":
            break
        if time.monotonic() + JOB_POLL_SECONDS > deadline:
            return _dump({
                "job_id": job_id,
                "status": "running",
                "pagesDone": job.get("pagesDone"),
                "pagesTotal": job.get("pagesTotal"),
                "next": "Still running. Call get_job again now with the same job_id.",
            })
        await asyncio.sleep(JOB_POLL_SECONDS)

    if job.get("status") != "finished":
        return _dump({"job_id": job_id, **{k: v for k, v in job.items() if k != "jobId"}})

    pages = [
        {k: v for k, v in page.items() if k not in STRIPPED_PAGE_FIELDS}
        for page in job.get("pages", [])
    ]
    return _dump({
        "job_id": job_id,
        "status": "finished",
        "pageCount": len(pages),
        "pages": pages,
        "urls": job.get("urls", []),
        "redirects": job.get("redirects", []),
    })


@tool("Fetch web page")
async def fetch_page(
    url: Annotated[str, Field(description="Full URL of the page, e.g. https://example.com/about")],
) -> str:
    """Read a web page: its text as Markdown, plus title, site name and SEO metadata.

    Use this instead of built-in web fetch whenever you need to read, quote,
    summarise, rewrite or audit a specific page, including pages found by
    crawl. Returns url (final, after redirects), title, siteName, seo
    (title, description = meta description, lang, og and twitter tags) and
    markdown. Markdown keeps headings, lists and link text but drops link
    URLs and images, so use crawl, not this, to discover a site's pages.
    Markdown over 100,000 characters is cut, with truncated set to true.
    """
    result = await _call("webfetch", {"url": url})
    for key in ("html", "htmlTruncated", "format", "mode"):
        result.pop(key, None)
    return _dump(result)


if __name__ == "__main__":
    mcp.run(
        "streamable-http",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        # Traefik controls the Host header and mcp-auth gates every request.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
