"""Attach one or more files to a GitHub issue.

GitHub has no public REST/GraphQL endpoint for the drag-and-drop attachments of
the issue UI (those are served from a private ``github.com/user-attachments``
uploader). The portable, API-only way to give an issue a durable file is to
commit the file into the repository and reference its URL from an issue
comment. This tool does exactly that, binary-safe:

* each file's bytes are supplied as base64 (so PNGs, PDFs and logs work, not
  only UTF-8 text) and committed through the Contents API
  (``PUT /repos/{owner}/{repo}/contents/{path}``), which creates the file's
  parent folders on demand and returns its canonical URLs;
* the files land on a single, configuration-fenced branch (never the default
  branch, never ``.github/``) under a per-issue folder, so attachments never
  touch reviewed source and are easy to find and prune;
* a single issue comment is posted linking every uploaded file (images are
  embedded, other files are listed), unless ``comment`` is false.

The tool is confined to the configured ``GH_PROJECT_ORG_NAME`` /
``GH_PROJECT_REPO_NAME`` repository and honours the same
``GH_PROJECT_WRITE_BRANCH_PREFIXES`` fence as ``commit_files``; it holds no
caller filesystem path, so nothing outside the base64 payload is read.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, Field, field_validator

from clients.gh_cli_client import CLIError
from core.config import get_settings
from core.error_handling import build_error_response, handle_tool_error
from core.factory import get_service_factory
from models.responses import ToolSuccess

logger = logging.getLogger(__name__)

MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_BYTES = 10_000_000  # 10 MB per file (GitHub Contents API ceiling).
MAX_TOTAL_BYTES = 25_000_000       # 25 MB across all files in one call.
_DEFAULT_PREFIX = ".attachments"
_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp")


class AttachmentFile(BaseModel):
    """One file to attach: a safe filename and its base64-encoded bytes."""

    filename: str = Field(
        min_length=1,
        max_length=120,
        description="File name only (no folders), e.g. 'terraform-plan.txt' or 'diagram.png'. Letters, digits, '.', '_' and '-' only.",
    )
    content_base64: str = Field(
        min_length=1,
        max_length=14_000_000,
        description="The file's bytes encoded as base64 (binary-safe). Decoded size must be <= 10 MB.",
    )

    @field_validator("filename")
    @classmethod
    def _safe_filename(cls, value: str) -> str:
        name = value.strip()
        if not _FILENAME_RE.match(name) or name in (".", ".."):
            raise ValueError(
                "filename must be a bare name (no '/' or '\\') of letters, digits, '.', '_' or '-'"
            )
        return name


class AttachFileToIssueInput(BaseModel):
    """Input schema for the attach_file_to_issue tool."""

    issue_number: int = Field(ge=1, description="GitHub issue (or pull request) number to attach the files to.")
    files: list[AttachmentFile] = Field(
        min_length=1,
        max_length=MAX_ATTACHMENTS,
        description="Files to attach (1-10). Each carries a filename and its base64 content.",
    )
    branch: str = Field(
        default="",
        max_length=200,
        description="Branch to commit the files to; must start with an allowed prefix (GH_PROJECT_WRITE_BRANCH_PREFIXES). Empty uses '<first prefix>attachments'.",
    )
    comment: bool = Field(
        default=True,
        description="Post an issue comment linking the uploaded files (images embedded). Set false to upload only.",
    )
    comment_header: str = Field(
        default="",
        max_length=500,
        description="Optional first line for the comment, e.g. 'Terraform plan output'. Empty uses a neutral default.",
    )


def _repo() -> str:
    settings = get_settings()
    return f"{settings.org_name}/{settings.repo_name}"


def _resolve_branch(requested: str) -> tuple[str, str | None]:
    """Return (branch, refusal). An empty request defaults to '<prefix>attachments'."""
    prefixes = tuple(get_settings().branch_prefixes())
    name = requested.strip()
    if not name:
        if not prefixes:
            return "", (
                "no branch given and GH_PROJECT_WRITE_BRANCH_PREFIXES is empty; "
                "set a write-branch prefix or pass an explicit branch"
            )
        return f"{prefixes[0]}attachments", None
    if ".." in name or name.endswith(("/", ".lock")) or "//" in name:
        return name, f"'{name}' is not a valid branch name"
    if not any(name.startswith(prefix) for prefix in prefixes):
        return name, (
            f"branch '{name}' must start with one of {list(prefixes)} "
            "(GH_PROJECT_WRITE_BRANCH_PREFIXES)"
        )
    return name, None


async def _branch_exists(gh, repo: str, branch: str) -> bool:
    try:
        await gh.run(["api", f"repos/{repo}/git/ref/heads/{quote(branch, safe='/')}"])
        return True
    except CLIError:
        return False


async def _default_branch(gh, repo: str) -> str:
    result = await gh.run(["api", f"repos/{repo}"])
    data = json.loads(result.stdout) if result.stdout.strip() else {}
    branch = data.get("default_branch") if isinstance(data, dict) else None
    if not branch:
        raise ValueError("could not resolve the repository default branch")
    return str(branch)


def _is_image(filename: str) -> bool:
    return filename.lower().endswith(_IMAGE_SUFFIXES)


async def attach_file_to_issue(params: AttachFileToIssueInput) -> dict:
    """Attach one or more files to a GitHub issue by committing them and linking them in a comment.

    GitHub exposes no public API for the issue UI's drag-and-drop attachments,
    so each file (base64, binary-safe) is committed through the Contents API to
    an allowed, non-default branch under `.attachments/issue-<n>/` and then
    referenced from a single issue comment (images embedded, other files
    listed). Returns the uploaded files with their URLs and the comment URL.
    """
    try:
        await get_service_factory().ensure_auth()
        repo = _repo()
        gh = get_service_factory().gh()

        branch, refusal = _resolve_branch(params.branch)
        if refusal:
            return build_error_response(
                error_type="validation",
                message=refusal,
                suggestion="Pass a branch whose name starts with an allowed write prefix.",
            )

        # Validate payloads up front so nothing is committed on a bad file.
        decoded: list[tuple[str, bytes]] = []
        seen: set[str] = set()
        total = 0
        for item in params.files:
            if item.filename in seen:
                return build_error_response(
                    error_type="validation",
                    message=f"duplicate filename '{item.filename}' in this request",
                    suggestion="Give each attached file a distinct name.",
                )
            seen.add(item.filename)
            try:
                raw = base64.b64decode(item.content_base64, validate=True)
            except (binascii.Error, ValueError):
                return build_error_response(
                    error_type="validation",
                    message=f"content_base64 for '{item.filename}' is not valid base64",
                    suggestion="Send the file's bytes encoded as standard base64.",
                )
            if not raw:
                return build_error_response(
                    error_type="validation",
                    message=f"'{item.filename}' decodes to an empty file",
                    suggestion="Provide non-empty file content.",
                )
            if len(raw) > MAX_ATTACHMENT_BYTES:
                return build_error_response(
                    error_type="validation",
                    message=f"'{item.filename}' is {len(raw)} bytes (> {MAX_ATTACHMENT_BYTES} per file)",
                    suggestion="Attach a smaller file or split it.",
                )
            total += len(raw)
            decoded.append((item.filename, raw))
        if total > MAX_TOTAL_BYTES:
            return build_error_response(
                error_type="validation",
                message=f"attachments total {total} bytes (> {MAX_TOTAL_BYTES} per call)",
                suggestion="Attach fewer or smaller files per call.",
            )

        default_branch = await _default_branch(gh, repo)
        if branch == default_branch:
            return build_error_response(
                error_type="validation",
                message=f"attachments must not be committed to the default branch '{default_branch}'",
                suggestion="Use a branch whose name starts with an allowed write prefix.",
            )

        # Create the attachments branch off the default branch if it is new.
        if not await _branch_exists(gh, repo, branch):
            ref = await gh.run(["api", f"repos/{repo}/git/ref/heads/{quote(default_branch, safe='/')}"])
            ref_data = json.loads(ref.stdout) if ref.stdout.strip() else {}
            sha = ((ref_data or {}).get("object") or {}).get("sha", "")
            if not sha:
                return build_error_response(
                    error_type="internal",
                    message="could not resolve the default branch head to create the attachments branch",
                    suggestion="Verify the repository has at least one commit on its default branch.",
                )
            await gh.run([
                "api", "--method", "POST", f"repos/{repo}/git/refs",
                "-f", f"ref=refs/heads/{branch}",
                "-f", f"sha={sha}",
            ])

        folder = f"{_DEFAULT_PREFIX}/issue-{params.issue_number}"
        uploaded: list[dict[str, Any]] = []
        for filename, raw in decoded:
            path = f"{folder}/{filename}"
            api_path = f"repos/{repo}/contents/{quote(path, safe='/')}"
            # Overwrite-safe: if the path already exists on the branch, pass its
            # blob sha so the Contents API updates instead of rejecting it.
            existing_sha = ""
            try:
                meta = await gh.run(["api", f"{api_path}?ref={quote(branch, safe='/')}"])
                meta_data = json.loads(meta.stdout) if meta.stdout.strip() else {}
                if isinstance(meta_data, dict):
                    existing_sha = str(meta_data.get("sha") or "")
            except CLIError:
                existing_sha = ""

            put_args = [
                "api", "--method", "PUT", api_path,
                "-f", f"message=chore(attachments): add {filename} for issue #{params.issue_number}",
                "-f", f"branch={branch}",
                "-f", f"content={base64.b64encode(raw).decode('ascii')}",
            ]
            if existing_sha:
                put_args.extend(["-f", f"sha={existing_sha}"])
            put = await gh.run(put_args)
            put_data = json.loads(put.stdout) if put.stdout.strip() else {}
            content = put_data.get("content", {}) if isinstance(put_data, dict) else {}
            html_url = content.get("html_url") or f"https://github.com/{repo}/blob/{branch}/{quote(path, safe='/')}"
            raw_url = content.get("download_url") or f"https://raw.githubusercontent.com/{repo}/{branch}/{quote(path, safe='/')}"
            uploaded.append({
                "filename": filename,
                "path": path,
                "size": len(raw),
                "is_image": _is_image(filename),
                "html_url": html_url,
                "raw_url": raw_url,
            })

        comment_url = ""
        if params.comment:
            header = params.comment_header.strip() or (
                f"Attached {len(uploaded)} file{'s' if len(uploaded) != 1 else ''}:"
            )
            lines = [header, ""]
            for file in uploaded:
                if file["is_image"]:
                    lines.append(f"![{file['filename']}]({file['raw_url']})")
                else:
                    lines.append(f"- [{file['filename']}]({file['html_url']})")
            body = "\n".join(lines)
            comment_result = await gh.run([
                "issue", "comment", str(params.issue_number),
                "--repo", repo,
                "--body", body,
            ])
            comment_url = comment_result.stdout.strip() if comment_result.stdout else ""

        return ToolSuccess(
            data={
                "repository": repo,
                "issue_number": params.issue_number,
                "branch": branch,
                "folder": folder,
                "count": len(uploaded),
                "files": uploaded,
                "comment_url": comment_url,
                "commented": bool(params.comment),
                "message": (
                    f"Attached {len(uploaded)} file(s) to issue #{params.issue_number} on branch '{branch}'."
                ),
            },
        ).model_dump()

    except CLIError as exc:
        logger.error("CLI error in attach_file_to_issue: %s", exc)
        return build_error_response(
            error_type="internal",
            message=f"Failed to attach files: {exc.stderr.strip()}",
            suggestion="Verify the issue exists and the token has 'contents' and 'issues' write permissions.",
        )
    except Exception as exc:
        logger.error("Error in attach_file_to_issue: %s", exc)
        return handle_tool_error(exc, context="Attach file to issue failed")
