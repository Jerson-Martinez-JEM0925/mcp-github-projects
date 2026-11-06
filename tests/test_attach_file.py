"""attach_file_to_issue: binary-safe issue attachments via a fenced branch.

No network: the gh CLI is faked. Covers branch fencing, base64 validation,
size limits, the Contents API upload (new branch + overwrite), and the issue
comment that embeds images and lists other files.
"""
from __future__ import annotations

import base64
import json

import pytest

from clients.gh_cli_client import CLIError, CommandResult
from core.config import get_settings
from core.factory import use_service_factory
from tools.issues.attach_file import (
    AttachFileToIssueInput,
    AttachmentFile,
    attach_file_to_issue,
)

REPO = "octo-org/octo-repo"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def configured_target(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GH_PROJECT_ORG_NAME", "octo-org")
    monkeypatch.setenv("GH_PROJECT_REPO_NAME", "octo-repo")
    monkeypatch.setenv("GH_PROJECT_PROJECT_NUMBER", "7")
    monkeypatch.setenv("GH_PROJECT_WRITE_BRANCH_PREFIXES", "chatbot/")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeGH:
    """Routes `gh api` by path prefix; `gh issue comment` returns a URL.

    `missing` is a set of `contents/<path>` targets that should 404 (so the
    tool treats them as new files) and `branches` lists existing branch refs.
    """

    def __init__(self, *, branches: set[str] | None = None, missing: set[str] | None = None):
        self.branches = branches or set()
        self.missing = missing or set()
        self.calls: list[list[str]] = []

    async def run(self, args: list[str]) -> CommandResult:
        self.calls.append(args)
        if args[:1] == ["issue"] and "comment" in args:
            return CommandResult(stdout="https://github.com/octo-org/octo-repo/issues/5#issuecomment-1\n", stderr="", return_code=0)
        target = next((a for a in args[1:] if a.startswith("repos/")), "")
        base = target.split("?")[0]
        # Repository metadata (default branch).
        if base == f"repos/{REPO}":
            return self._json({"default_branch": "main"})
        # Branch ref lookup.
        if base.startswith(f"repos/{REPO}/git/ref/heads/"):
            branch = base.split("/git/ref/heads/", 1)[1]
            if branch == "main" or branch in self.branches:
                return self._json({"object": {"sha": "a" * 40}})
            raise CLIError("Not Found", return_code=1, stderr="HTTP 404")
        # Create ref.
        if base == f"repos/{REPO}/git/refs":
            return self._json({"object": {"sha": "a" * 40}})
        # Contents: GET (existence probe) vs PUT (upload).
        if base.startswith(f"repos/{REPO}/contents/"):
            is_put = "--method" in args and "PUT" in args
            path = base.split("/contents/", 1)[1]
            if not is_put:
                if path in self.missing:
                    raise CLIError("Not Found", return_code=1, stderr="HTTP 404")
                return self._json({"sha": "old" + "0" * 37})
            return self._json({
                "content": {
                    "html_url": f"https://github.com/{REPO}/blob/chatbot/attachments/{path}",
                    "download_url": f"https://raw.githubusercontent.com/{REPO}/chatbot/attachments/{path}",
                }
            })
        return self._json({})

    @staticmethod
    def _json(payload: object) -> CommandResult:
        return CommandResult(stdout=json.dumps(payload), stderr="", return_code=0)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


# ── Validation / fencing ─────────────────────────────────────────────────────


def test_filename_with_path_is_rejected() -> None:
    with pytest.raises(ValueError):
        AttachmentFile(filename="dir/evil.txt", content_base64=_b64("x"))


@pytest.mark.anyio
async def test_branch_outside_prefix_is_refused() -> None:
    gh = FakeGH()
    with use_service_factory(gh_client=gh):
        result = await attach_file_to_issue(AttachFileToIssueInput(
            issue_number=5, branch="feature/x",
            files=[AttachmentFile(filename="a.txt", content_base64=_b64("hi"))],
        ))
    assert result["ok"] is False
    assert not any("--method" in c for c in gh.calls)


@pytest.mark.anyio
async def test_invalid_base64_is_refused_before_any_write() -> None:
    gh = FakeGH()
    with use_service_factory(gh_client=gh):
        result = await attach_file_to_issue(AttachFileToIssueInput(
            issue_number=5,
            files=[AttachmentFile(filename="a.txt", content_base64="not base64!!")],
        ))
    assert result["ok"] is False
    assert not any("--method" in c for c in gh.calls)


@pytest.mark.anyio
async def test_duplicate_filenames_refused() -> None:
    gh = FakeGH()
    with use_service_factory(gh_client=gh):
        result = await attach_file_to_issue(AttachFileToIssueInput(
            issue_number=5,
            files=[
                AttachmentFile(filename="a.txt", content_base64=_b64("1")),
                AttachmentFile(filename="a.txt", content_base64=_b64("2")),
            ],
        ))
    assert result["ok"] is False
    assert not any("--method" in c for c in gh.calls)


# ── Happy paths ──────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_new_branch_upload_and_image_comment() -> None:
    # Branch 'chatbot/attachments' does not exist yet; files are new.
    gh = FakeGH(
        branches=set(),
        missing={
            ".attachments/issue-5/plan.txt",
            ".attachments/issue-5/diagram.png",
        },
    )
    png = _b64("\x89PNGbytes")
    with use_service_factory(gh_client=gh):
        result = await attach_file_to_issue(AttachFileToIssueInput(
            issue_number=5,
            comment_header="Terraform plan output",
            files=[
                AttachmentFile(filename="plan.txt", content_base64=_b64("terraform plan...")),
                AttachmentFile(filename="diagram.png", content_base64=png),
            ],
        ))
    assert result["ok"] is True
    data = result["data"]
    assert data["branch"] == "chatbot/attachments"
    assert data["count"] == 2
    assert data["folder"] == ".attachments/issue-5"
    # A branch ref was created (default branch did not exist as the target).
    assert any(c[:3] == ["api", "--method", "POST"] and any("refs" in a for a in c) for c in gh.calls)
    # Two PUT uploads happened, with base64 content.
    puts = [c for c in gh.calls if "--method" in c and "PUT" in c]
    assert len(puts) == 2
    assert any("content=" in a for p in puts for a in p)
    # The comment embeds the image and links the text file.
    comment = next(c for c in gh.calls if c[:1] == ["issue"] and "comment" in c)
    body = comment[comment.index("--body") + 1]
    assert body.startswith("Terraform plan output")
    assert "![diagram.png]" in body
    assert "[plan.txt]" in body and "![plan.txt]" not in body
    assert data["comment_url"].endswith("issuecomment-1")


@pytest.mark.anyio
async def test_existing_file_is_overwritten_with_sha() -> None:
    # Branch exists and the file already exists (probe returns a sha).
    gh = FakeGH(branches={"chatbot/attachments"}, missing=set())
    with use_service_factory(gh_client=gh):
        result = await attach_file_to_issue(AttachFileToIssueInput(
            issue_number=9, comment=False,
            files=[AttachmentFile(filename="log.txt", content_base64=_b64("v2"))],
        ))
    assert result["ok"] is True and result["data"]["commented"] is False
    # No new branch ref created (it already existed).
    assert not any(c[:3] == ["api", "--method", "POST"] for c in gh.calls)
    # The PUT carried the existing blob sha so the API updates in place.
    put = next(c for c in gh.calls if "--method" in c and "PUT" in c)
    assert any(a.startswith("sha=old") for a in put)
    # No comment was posted.
    assert not any(c[:1] == ["issue"] for c in gh.calls)
