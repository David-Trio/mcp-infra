"""triotr.ee MCP server: read and edit the triotr.ee link page's profile.json.

Serves streamable HTTP on 0.0.0.0:8080/mcp. Traefik + mcp-auth handle client
auth; this process holds a GitHub token, which Claude never sees.

Only src/data/profile.json in the triotr.ee repo is ever touched. Each save is
one commit to the deploy branch (main); Cloudflare builds and deploys from
there. There is no local clone: the GitHub contents API reads and writes the
file.

Two tools:
- get_profile returns the file, its blob sha and the image files in
  src/assets that the page can reference.
- update_profile takes the whole new profile plus the sha it was based on.
  The shape is checked against the site's ProfileData type (src/types/profile.ts)
  and every image name against src/assets, so a save can't break the Astro
  build. GitHub rejects the write if the sha is stale, so two people editing
  at once can't overwrite each other.
"""

import base64
import inspect
import json
import os
import re
from typing import Annotated, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

GITHUB_TOKEN = os.environ.get("TRIOTREE_GITHUB_TOKEN", "")
REPO = os.environ.get("TRIOTREE_REPO", "David-Trio/triotr.ee")
BRANCH = os.environ.get("TRIOTREE_BRANCH", "main")
COMMIT_NAME = os.environ.get("TRIOTREE_COMMIT_NAME", "triotr.ee bot")
COMMIT_EMAIL = os.environ.get("TRIOTREE_COMMIT_EMAIL", "bot@triotr.ee")
if not GITHUB_TOKEN:
    raise SystemExit("TRIOTREE_GITHUB_TOKEN must be set")

PROFILE_PATH = "src/data/profile.json"
ASSETS_DIR = "src/assets"
# Must match the glob in the site's src/utils/images.ts.
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
SITE_URL = "https://triotr.ee/"
GITHUB_TIMEOUT_SECONDS = 30
SUMMARY_MAX_CHARS = 72

INSTRUCTIONS = """\
Edits the triotr.ee link page (Trio Media's link-in-bio): its profile, SEO tags, \
labels, theme colours, social icons and list of link buttons. Everything comes from \
one file, profile.json; saving commits it to GitHub and the live site rebuilds within \
a few minutes.

- Always call get_profile first and edit what it returns. Never write a profile from \
memory or from an earlier get_profile.
- update_profile replaces the whole file: send the complete profile with only the \
requested changes made, plus the sha from get_profile.
- Before calling update_profile, tell the user exactly what will change and get their \
OK, unless they already gave it.
- Images can't be uploaded. Image fields take a file name from the assets list \
get_profile returns (thumbnail can also be an https:// image URL).
- The top button is the latest blog post, added automatically at build time; it isn't \
in links.\
"""

mcp = MCPServer("triotr.ee", instructions=INSTRUCTIONS)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
# Overwrites the live page, but every save is a git commit, so it can be undone.
WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False,
)


def tool(title: str, annotations: ToolAnnotations):
    """Register a tool. The SDK sends docstrings with their source indentation; cleandoc strips it."""
    def register(fn):
        return mcp.tool(
            title=title,
            description=inspect.cleandoc(fn.__doc__ or ""),
            annotations=annotations,
            # Plain text only: structured output would send every result twice.
            structured_output=False,
        )(fn)
    return register


# --- profile.json shape: mirrors ProfileData in the site's src/types/profile.ts.
# Field order here is the key order written to the file.

HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
Text = Annotated[str, Field(min_length=1)]


class Strict(BaseModel):
    # Unknown keys are refused rather than silently dropped, so a typo'd field
    # name fails loudly instead of losing the edit.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _check_url(value: str, schemes: tuple[str, ...]) -> str:
    if not value.startswith(schemes) or len(value) <= len(value.split(":", 1)[0]) + 3:
        raise ValueError(f"must start with {' or '.join(schemes)}")
    if any(ch.isspace() for ch in value):
        raise ValueError("must not contain spaces")
    return value


class Seo(Strict):
    title: Text
    description: Text
    canonicalUrl: Text
    socialImage: Text = Field(description="Image file name from assets; also the favicon")

    @field_validator("canonicalUrl")
    @classmethod
    def _url(cls, v: str) -> str:
        return _check_url(v, ("https://",))


class Profile(Strict):
    name: Text
    handle: Text
    description: Text
    avatar: Text = Field(description="Image file name from assets")
    avatarAlt: Text


class Labels(Strict):
    skipToContent: Text
    socialNavigation: Text
    profileLinks: Text
    share: Text
    copied: Text
    shareError: Text


class Theme(Strict):
    backgroundColor: Text = Field(description="Hex colour, e.g. #5e0a9e")
    backgroundImage: Text = Field(description="Image file name from assets")
    textColor: Text = Field(description="Hex colour")
    buttonColor: Text = Field(description="Hex colour")
    buttonTextColor: Text = Field(description="Hex colour")

    @field_validator("backgroundColor", "textColor", "buttonColor", "buttonTextColor")
    @classmethod
    def _hex(cls, v: str) -> str:
        if not HEX_COLOR.match(v):
            raise ValueError("must be a hex colour like #5e0a9e")
        return v


class Blog(Strict):
    label: Text = Field(description="Shown on the auto-added latest-blog button")
    endpoint: Text = Field(description="WordPress posts API URL; leave unchanged unless asked")

    @field_validator("endpoint")
    @classmethod
    def _url(cls, v: str) -> str:
        return _check_url(v, ("https://",))


class SocialLink(Strict):
    label: Text = Field(description="Accessible name, e.g. 'Trio Media on Instagram'")
    icon: Literal["email", "instagram", "linkedin", "facebook", "x", "tiktok"]
    url: Text

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return _check_url(v, ("https://", "http://", "mailto:"))


class Link(Strict):
    label: Text = Field(description="Button text; emoji are fine")
    url: Text
    thumbnail: str | None = Field(
        default=None, description="Optional: image file name from assets, or an https:// image URL",
    )
    thumbnailAlt: str | None = Field(default=None, description="Alt text for thumbnail")

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return _check_url(v, ("https://", "http://", "mailto:", "tel:"))


class ProfileData(Strict):
    seo: Seo
    profile: Profile
    labels: Labels
    theme: Theme
    blog: Blog
    socialLinks: list[SocialLink]
    links: list[Link] = Field(description="Link buttons, top to bottom (after the latest-blog button)")


# --- GitHub

client = httpx.AsyncClient(
    base_url="https://api.github.com",
    headers={
        "authorization": f"Bearer {GITHUB_TOKEN}",
        "accept": "application/vnd.github+json",
        "x-github-api-version": "2022-11-28",
        "user-agent": "triotree-mcp",
    },
    timeout=httpx.Timeout(GITHUB_TIMEOUT_SECONDS, connect=10.0),
)


def _contents_path(path: str) -> str:
    return f"/repos/{REPO}/contents/{quote(path)}"


async def _github(method: str, path: str, **kwargs) -> httpx.Response:
    try:
        response = await client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise ToolError(f"GitHub unreachable: {type(exc).__name__}") from exc
    if response.status_code >= 400 and response.status_code not in (409, 422):
        try:
            message = response.json().get("message") or response.text
        except ValueError:
            message = response.text
        raise ToolError(f"GitHub error {response.status_code}: {message[:300]}")
    return response


async def _read_profile() -> tuple[dict, str]:
    response = await _github("GET", _contents_path(PROFILE_PATH), params={"ref": BRANCH})
    body = response.json()
    raw = base64.b64decode(body["content"]).decode("utf-8")
    return json.loads(raw), body["sha"]


async def _list_assets() -> list[str]:
    response = await _github("GET", _contents_path(ASSETS_DIR), params={"ref": BRANCH})
    return sorted(
        item["name"] for item in response.json()
        if item.get("type") == "file" and item["name"].lower().endswith(IMAGE_EXTENSIONS)
    )


def _serialise(profile: ProfileData) -> str:
    """Same layout as the hand-edited file: 2-space indent, literal emoji, trailing newline."""
    return json.dumps(profile.model_dump(exclude_none=True), ensure_ascii=False, indent=2) + "\n"


def _missing_images(profile: ProfileData, assets: list[str]) -> list[str]:
    refs = {
        "seo.socialImage": profile.seo.socialImage,
        "profile.avatar": profile.profile.avatar,
        "theme.backgroundImage": profile.theme.backgroundImage,
    }
    for i, link in enumerate(profile.links):
        if link.thumbnail and not link.thumbnail.startswith("https://"):
            refs[f"links[{i}].thumbnail"] = link.thumbnail
    available = set(assets)
    return [f"{field}: {name}" for field, name in refs.items() if name not in available]


def _editor(ctx: Context) -> str | None:
    """Who's editing, from the X-Auth-User header mcp-auth adds. None if absent (e.g. local runs)."""
    try:
        request = ctx.request_context.request
        return request.headers.get("x-auth-user") if request is not None else None
    except (ValueError, AttributeError):
        return None


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


# --- tools

@tool("Get triotr.ee profile", READ_ONLY)
async def get_profile() -> str:
    """Get the current content of the triotr.ee link page (profile.json), ready to edit.

    Returns:
    - profile: the whole page. seo (page title, meta description, share
      image), profile (name, handle, bio, avatar), labels (accessibility and
      share-button text), theme (hex colours, background image), blog (label
      for the auto-added latest-blog button), socialLinks (icon row) and links
      (the buttons, top to bottom).
    - sha: pass this unchanged to update_profile.
    - assets: image file names that image fields can use.
    - site: the live page.

    Call this before every edit, and again after a failed save.
    """
    profile, sha = await _read_profile()
    assets = await _list_assets()
    return _dump({"site": SITE_URL, "sha": sha, "assets": assets, "profile": profile})


@tool("Update triotr.ee profile", WRITE)
async def update_profile(
    profile: Annotated[ProfileData, Field(description=(
        "The complete new profile: everything from get_profile, with only the requested "
        "changes made. Anything left out is removed from the page."
    ))],
    sha: Annotated[str, Field(description="The sha from the get_profile this edit is based on")],
    summary: Annotated[str, Field(description=(
        "What changed, in a few words, for the commit message, e.g. "
        "'Add Christmas offer link' or 'Reorder links'"
    ))],
    ctx: Context,
) -> str:
    """Save a new version of the triotr.ee link page. It goes live within a few minutes.

    Replaces profile.json as a whole: send the complete profile from
    get_profile with the user's changes applied, never a partial one. Confirm
    the changes with the user before calling, unless they already approved.

    Checks before saving: every field present and non-empty, colours as hex,
    URLs starting with https:// (or mailto:/tel: where it makes sense), social
    icon one of email, instagram, linkedin, facebook, x, tiktok, and image
    fields naming a file in assets. Nothing is saved if a check fails; fix
    what the error lists and call again.

    If someone else saved in the meantime, the save is refused: call
    get_profile, reapply the change to the new version, and save again.

    Returns the commit URL. Saving an unchanged profile makes no commit.
    """
    assets = await _list_assets()
    missing = _missing_images(profile, assets)
    if missing:
        raise ToolError(
            "These images aren't in src/assets, and uploads aren't supported: "
            + "; ".join(missing) + f". Available: {', '.join(assets)}"
        )

    current, current_sha = await _read_profile()
    if current_sha != sha:
        raise ToolError(
            "profile.json changed since your get_profile (someone else saved). Nothing was "
            "saved. Call get_profile, reapply the change to the new version, and save again."
        )

    content = _serialise(profile)
    try:
        unchanged = ProfileData.model_validate(current) == profile
    except ValidationError:
        unchanged = False
    if unchanged:
        return _dump({"saved": False, "reason": "No changes: the profile is already like this."})

    summary = " ".join(summary.split())[:SUMMARY_MAX_CHARS] or "Update profile"
    message = f"triotr.ee: {summary}"
    editor = _editor(ctx)
    if editor:
        message += f"\n\nEdited by {editor} via the triotr.ee MCP."

    bot = {"name": COMMIT_NAME, "email": COMMIT_EMAIL}
    response = await _github("PUT", _contents_path(PROFILE_PATH), json={
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "sha": sha,
        "branch": BRANCH,
        "committer": bot,
        "author": bot,
    })
    if response.status_code in (409, 422):
        raise ToolError(
            "GitHub refused the save, most likely because someone else saved first. Nothing "
            "was saved. Call get_profile, reapply the change, and save again."
        )
    result = response.json()
    return _dump({
        "saved": True,
        "commit": result["commit"]["html_url"],
        "sha": result["content"]["sha"],
        "site": SITE_URL,
        "note": "The live page updates within a few minutes, once the site rebuilds.",
    })


if __name__ == "__main__":
    mcp.run(
        "streamable-http",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        # Traefik controls the Host header and mcp-auth gates every request.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
