"""apox MCP server: three tools (crawl, get_job, fetch_page) over apox's JSON API.

Serves streamable HTTP on 0.0.0.0:8080/mcp. Traefik + mcp-auth handle client
auth; this process holds apox's own bearer token, which Claude never sees.

Kept deliberately narrow:
- crawl takes a URL and force (bypass today's cached job). Page-field
  selection, delays, excludes etc. are fixed here so the model can't (and
  needn't) tune them.
- get_job waits on the server (up to JOB_WAIT_SECONDS) because the model has
  no way to sleep between calls. It returns the moment the crawl ends, so a
  long cap costs small crawls nothing and saves large ones model turns: every
  "still running" reply is a full model turn over the whole conversation.
- A finished crawl goes back as CSV text, not JSON: no repeated keys, so
  more pages fit in the model's context, and a result past ~150,000
  characters is saved by claude.ai as a file in Claude's code sandbox, where
  CSV is easy to analyse. html, content and fetchedAt are left out. content
  is still requested from apox so the job is reusable if we expose it later,
  and so apox adds contentLength (body size, for thin-content checks).
"""

import asyncio
import csv
import inspect
import io
import json
import os
import time
from typing import Annotated
from urllib.parse import quote

import httpx
from pydantic import Field
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
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
# apox adds titleLength / metaDescriptionLength / contentLength alongside
# title / metaDescription / content by itself; they aren't selectable.
CRAWL_PAGE_FIELDS = ["url", "title", "metaDescription", "h1", "content", "contentHash", "fetchedAt"]
# The pages CSV get_job returns, in column order. h1Count is ours; the rest
# come from apox. 12 hex chars of contentHash are plenty to spot duplicates
# within one site.
PAGE_COLUMNS = [
    "url", "title", "titleLength", "metaDescription", "metaDescriptionLength",
    "h1", "h1Count", "contentLength", "contentHash",
]
CONTENT_HASH_CHARS = 12
H1_SEPARATOR = " | "

# claude.ai and Desktop allow 240s per tool call
# (claude.com/docs/connectors/building). One get_job call never runs past
# JOB_CALL_BUDGET_SECONDS: apox requests inside it only get the time left.
JOB_CALL_BUDGET_SECONDS = 225
# How long one get_job call keeps polling a running crawl before replying
# "still running". Override with APOX_JOB_WAIT_SECONDS; capped so the last
# poll still has time inside the budget.
JOB_WAIT_SECONDS = min(int(os.environ.get("APOX_JOB_WAIT_SECONDS", "180")), 200)
JOB_POLL_SECONDS = 3
APOX_TIMEOUT_SECONDS = 60

# The model reads these to choose between our tools and its built-in web
# fetch / search / code execution, so they say when to prefer apox, not just
# what it does. Keep them in line with apox's actual behaviour.
INSTRUCTIONS = """\
apox is Trio's own website crawler and page fetcher. Use it for any task that needs \
the content or structure of a website: reading a page, auditing or reviewing a site, \
listing a site's pages, checking titles, meta descriptions and their lengths, H1s, \
thin or duplicate content, or redirects.

Prefer apox over built-in web fetch, web search or writing scraping code whenever you \
have a URL: it fetches pages server-side with SEO metadata included, and it crawls \
whole sites, which those tools can't.

- One page, or a few known pages: fetch_page.
- A whole site, or "all pages / every blog post / site structure": crawl, then get_job. \
Don't map a site by fetching pages one by one or guessing URLs.
- After a crawl, fetch_page the specific pages whose content you need to read.

Crawl flow: crawl returns a job_id. Call get_job with it. get_job waits up to a few \
minutes by itself; while status is "running", call get_job again immediately with the \
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
    timeout=httpx.Timeout(APOX_TIMEOUT_SECONDS, connect=10.0),
)


def _dump(value: object) -> str:
    """Compact JSON: whitespace costs model tokens on large crawls."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


async def _apox(method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
    """Call apox and return its JSON, turning any failure into a ToolError the model can read."""
    try:
        response = await client.request(
            method, path, json=body,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)) if timeout else httpx.USE_CLIENT_DEFAULT,
        )
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
    force: Annotated[bool, Field(description=(
        "true only when the user asks for fresh, new or re-crawled data, or says the site "
        "changed since it was last crawled today. Otherwise leave it false."
    ))] = False,
) -> str:
    """Trio's website crawler: crawl, scrape or audit a whole website and list every page with its title, meta description, H1s and their character counts, plus redirects.

    Use this instead of built-in web fetch, web search or writing scraping
    code whenever a task covers a whole site or many of its pages: site or
    SEO audits, reviews of titles / meta descriptions / H1s (including
    too-long or too-short ones), listing all pages, URLs or blog posts,
    site structure, content inventories, thin- or duplicate-content checks,
    redirect checks. It discovers pages from the sitemap and internal links,
    so don't fetch pages one at a time or guess URLs to map a site.

    Returns a job_id at once; the crawl runs in the background. Next, call
    get_job with the job_id. A site already crawled today (or still being
    crawled) returns that same job, so its results are ready immediately.

    Set force to true only when the user explicitly wants fresh data ("re-crawl",
    "pull fresh data", "I've just updated the site"). It always starts a new
    full crawl, even while one is running, so never use it to retry or to
    check on a crawl: call get_job with the existing job_id instead.
    """
    data = {"url": url, "pages": CRAWL_PAGE_FIELDS}
    if force:
        data["force"] = True
    result = await _call("crawl", data)
    return _dump({"job_id": result["jobId"], "next": "Call get_job with this job_id."})


def _csv(rows: list[list]) -> str:
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerows(rows)
    return out.getvalue()


def _page_row(page: dict) -> list:
    h1 = page.get("h1")
    h1s = h1 if isinstance(h1, list) else [h1] if isinstance(h1, str) else []
    values = {
        **page,
        "h1": H1_SEPARATOR.join(h1s),
        "h1Count": len(h1s),
        "contentHash": (page.get("contentHash") or "")[:CONTENT_HASH_CHARS],
    }
    # None (field absent) becomes an empty cell.
    return [values.get(column) for column in PAGE_COLUMNS]


def _finished_csv(job_id: str, job: dict) -> str:
    """A finished crawl as a short header plus CSV sections, each under a "# <name>.csv" line."""
    pages = job.get("pages", [])
    redirects = job.get("redirects", [])
    # apox's urls list is the page URLs plus URLs skipped by an exclude
    # (crawl never sends one), so only list what isn't already a page.
    page_urls = {page.get("url") for page in pages}
    other_urls = [url for url in job.get("urls", []) if url not in page_urls]

    parts = [
        f"job_id: {job_id}\nstatus: finished\npages: {len(pages)}, redirects: {len(redirects)}\n",
        "# pages.csv\n" + _csv([PAGE_COLUMNS, *(_page_row(page) for page in pages)]),
        "# redirects.csv\n" + _csv([
            ["from", "to", "status"],
            *([r.get("from"), r.get("to"), r.get("status")] for r in redirects),
        ]),
    ]
    if other_urls:
        parts.append("# unfetched-urls.csv\n" + _csv([["url"], *([url] for url in other_urls)]))
    return "\n".join(parts)


def _still_running(job_id: str, job: dict) -> str:
    return _dump({
        "job_id": job_id,
        "status": "running",
        "pagesDone": job.get("pagesDone"),
        "pagesTotal": job.get("pagesTotal"),
        "next": "Still running. Call get_job again now with the same job_id.",
    })


@tool("Get crawl results")
async def get_job(
    job_id: Annotated[str, Field(description="The job_id returned by crawl")],
    ctx: Context,
) -> str:
    """Get the results of a crawl started with crawl.

    This call itself waits up to a few minutes for the crawl to finish. While
    status is "running" (with pagesDone / pagesTotal progress), call get_job
    again immediately with the same job_id: don't wait or sleep first, and
    don't start a new crawl. Large sites can take several minutes. Stop when
    status is "finished", "failed" (see error) or "cancelled".

    A finished result is plain text, not JSON: a short header (job_id,
    status, page and redirect counts), then CSV sections, each starting
    with a "# <name>.csv" line:
    - pages.csv: one row per page that answered 2xx, sorted by url, with
      columns url, title, titleLength, metaDescription,
      metaDescriptionLength, h1, h1Count, contentLength, contentHash. An
      empty cell means the page has none (no title means no titleLength
      either). The *Length columns are character counts (an emoji counts as
      one), ready for SEO length checks: use them rather than counting
      yourself. contentLength is the size of the page's body text in
      characters, so a low value flags thin content. h1 holds every H1,
      joined with " | " when h1Count is over 1. Pages sharing a contentHash
      have identical body content. Rows whose url ends in #header / #footer
      are the site-wide header and footer, not real pages.
    - redirects.csv: URLs that redirect elsewhere, with columns from, to,
      status.
    - unfetched-urls.csv: only present when the site has URLs that weren't
      fetched as pages.
    A large result may be saved as a file in your code sandbox instead of
    shown inline: split it on the "# <name>.csv" lines and analyse the CSV
    with code.
    Page body text isn't included: use fetch_page on the pages you need to read.
    Results are kept until midnight UK time.
    """
    path = f"/crawl/{quote(job_id, safe='')}"
    start = time.monotonic()
    deadline = start + JOB_WAIT_SECONDS
    job: dict | None = None
    while True:
        remaining = start + JOB_CALL_BUDGET_SECONDS - time.monotonic()
        try:
            job = await _apox("GET", path, timeout=min(APOX_TIMEOUT_SECONDS, remaining))
        except ToolError as exc:
            # Out of budget mid-wait (e.g. a slow fetch of a big finished
            # result): report the last progress so the model calls again
            # with a fresh budget, rather than an error.
            if job is None or not isinstance(exc.__cause__, httpx.TimeoutException):
                raise
            return _still_running(job_id, job)
        if job.get("status") != "running":
            break
        # Progress notifications keep the response stream active during the
        # long wait (and show progress in clients that display it). No-op
        # when the client didn't ask for progress.
        if isinstance(job.get("pagesDone"), int):
            total = job.get("pagesTotal")
            await ctx.report_progress(
                job["pagesDone"],
                total if isinstance(total, int) and total > 0 else None,
                "Crawling",
            )
        if time.monotonic() + JOB_POLL_SECONDS > deadline:
            return _still_running(job_id, job)
        await asyncio.sleep(JOB_POLL_SECONDS)

    if job.get("status") != "finished":
        return _dump({"job_id": job_id, **{k: v for k, v in job.items() if k != "jobId"}})

    return _finished_csv(job_id, job)


@tool("Fetch web page")
async def fetch_page(
    url: Annotated[str, Field(description="Full URL of the page, e.g. https://example.com/about")],
) -> str:
    """Trio's page fetcher: open, read or scrape any web page or URL and get its text as Markdown, plus title, meta description and SEO tags.

    Use this instead of built-in web fetch for any URL the user gives or
    mentions, and whenever you need to read, quote, summarise, rewrite,
    check or audit a specific page, including pages found by crawl. For a
    whole site, use crawl instead. Returns url (final, after redirects), title, siteName, seo
    (title, titleLength, description = meta description,
    metaDescriptionLength, lang, og and twitter tags), markdown and
    markdownLength. The *Length fields are character counts (an emoji counts
    as one; 0 when the page has no title or meta description): use them for
    SEO length checks rather than counting yourself. Markdown keeps
    headings, lists and link text but drops link URLs and images, so use
    crawl, not this, to discover a site's pages. Markdown over 100,000
    characters is cut, with truncated set to true and markdownLength
    counting what was returned.
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
