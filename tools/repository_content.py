"""Repository contents: read files and folders, search code, governed commits.

Every tool is confined to the configured ``GH_PROJECT_ORG_NAME`` /
``GH_PROJECT_REPO_NAME`` repository (there is no owner/repo override), so the
documentation of that repository can be used as the source of truth by an MCP
client — for example a governance handbook served to a chat assistant.

Read tools (``contents.read``):

* ``list_repository_directory`` — one folder, or the whole tree filtered by a
  path prefix and/or a substring, with an explicit ``truncated`` flag.
* ``get_repository_file`` — UTF-8 text of one file, paged by characters.
* ``search_repository_code`` — GitHub code search restricted to the repository.

Write tools (``contents.write``), fenced by configuration:

* ``create_branch`` — only names starting with one of
  ``GH_PROJECT_WRITE_BRANCH_PREFIXES`` (default ``chatbot/``); never the
  default branch.
* ``commit_files`` — one commit on such a branch through the GraphQL
  ``createCommitOnBranch`` mutation, so the commit is attributed to the token
  owner (the end user with per-request credentials) and verified by GitHub.
  It accepts either full file contents or a bounded unified diff tied to an
  expected branch-head SHA.
  Paths under ``.github/`` are refused unless an exact workflow file is named
  by ``GH_PROJECT_ALLOWED_GITHUB_WORKFLOW_PATHS``. When
  ``GH_PROJECT_WRITE_PATH_PREFIXES`` is set, every path must also start with
  one of its prefixes. Changes reach the default branch only through a pull
  request.
"""
from __future__ import annotations

import base64
import fnmatch
import json
import re
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, Field, field_validator

from core.config import get_settings
from core.error_handling import build_error_response, handle_tool_error
from core.factory import get_service_factory
from core.hardening import bounded_text
from models.responses import ToolSuccess

MAX_TREE_ENTRIES = 1_000
MAX_FILE_CHARS = 60_000
MAX_COMMIT_FILES = 50
MAX_COMMIT_BYTES = 1_000_000
MAX_PATCH_BYTES = 1_000_000
_FORBIDDEN_PREFIXES = (".github/",)
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _repo() -> str:
    settings = get_settings()
    return f"{settings.org_name}/{settings.repo_name}"


def _clean_path(value: str, *, allow_empty: bool) -> str:
    """Normalize a repository-relative path and refuse traversal tricks."""
    path = (value or "").strip().strip("/")
    if not path:
        if allow_empty:
            return ""
        raise ValueError("path must not be empty")
    if "\\" in path or "\x00" in path:
        raise ValueError("path must use '/' separators and no control characters")
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise ValueError("path must not contain empty, '.' or '..' segments")
    return path


async def _api(path: str, *extra: str) -> Any:
    result = await get_service_factory().gh().run(["api", path, *extra])
    return json.loads(result.stdout) if result.stdout.strip() else {}


async def _default_branch() -> str:
    data = await _api(f"repos/{_repo()}")
    branch = data.get("default_branch") if isinstance(data, dict) else None
    if not branch:
        raise ValueError("could not resolve the repository default branch")
    return str(branch)


def _invalid(message: str, suggestion: str) -> dict:
    return build_error_response(
        error_type="validation", message=message, suggestion=suggestion
    )


def _parse_unified_patch(patch: str) -> list[dict[str, Any]]:
    """Parse the supported, text-only subset of unified git diffs."""
    files: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    hunk: dict[str, Any] | None = None
    header = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    for line in patch.replace("\r\n", "\n").splitlines(keepends=True):
        if line.startswith(("GIT binary patch", "Binary files ", "rename from ", "rename to ")):
            raise ValueError("binary patches and renames are unsupported")
        if line.startswith("diff --git "):
            match = re.fullmatch(r"diff --git a/(\S+) b/(\S+)\n?", line)
            if not match:
                raise ValueError("quoted or malformed diff paths are unsupported")
            current = {"old": match.group(1), "new": match.group(2), "hunks": []}
            files.append(current)
            hunk = None
        elif line.startswith("@@ ") and current is not None:
            match = header.match(line)
            if not match:
                raise ValueError("malformed unified-diff hunk header")
            old_start, old_count, new_start, new_count = match.groups()
            hunk = {
                "old_start": int(old_start),
                "old_count": int(old_count or 1),
                "new_count": int(new_count or 1),
                "ops": [],
            }
            current["hunks"].append(hunk)
        elif hunk is not None:
            if line.startswith("\\ No newline at end of file"):
                raise ValueError("no-newline markers are unsupported")
            if line[:1] in (" ", "+", "-"):
                hunk["ops"].append((line[0], line[1:]))
            elif line.strip():
                raise ValueError("unexpected data inside unified-diff hunk")
        elif line.startswith("--- ") and current is not None:
            current["old"] = line[4:].split("\t", 1)[0].strip()
        elif line.startswith("+++ ") and current is not None:
            current["new"] = line[4:].split("\t", 1)[0].strip()

    if not files:
        raise ValueError("patch contains no file changes")
    for item in files:
        old, new = item["old"], item["new"]
        if old != "/dev/null" and not old.startswith("a/"):
            raise ValueError("patch source path must use the a/ prefix")
        if new != "/dev/null" and not new.startswith("b/"):
            raise ValueError("patch destination path must use the b/ prefix")
        old_path = old[2:] if old.startswith("a/") else None
        new_path = new[2:] if new.startswith("b/") else None
        if old_path and new_path and old_path != new_path:
            raise ValueError("renames are unsupported")
        if not item["hunks"] or (old_path is None and new_path is None):
            raise ValueError("each patch file must contain hunks and a path")
        item["old_path"] = old_path
        item["new_path"] = new_path
        for part in item["hunks"]:
            old_count = sum(op != "+" for op, _ in part["ops"])
            new_count = sum(op != "-" for op, _ in part["ops"])
            if old_count != part["old_count"] or new_count != part["new_count"]:
                raise ValueError("hunk line counts do not match the header")
    if len(files) > MAX_COMMIT_FILES:
        raise ValueError(f"patch changes more than {MAX_COMMIT_FILES} files")
    paths = [item["new_path"] or item["old_path"] for item in files]
    if len(set(paths)) != len(paths):
        raise ValueError("patch contains a duplicate file path")
    return files


def _apply_hunks(source: str, hunks: list[dict[str, Any]]) -> str:
    """Apply hunks only when every context/removal line matches exactly."""
    source_lines = source.replace("\r\n", "\n").splitlines(keepends=True)
    result: list[str] = []
    cursor = 0
    for hunk in hunks:
        start = max(hunk["old_start"] - 1, 0)
        if start < cursor or start > len(source_lines):
            raise ValueError("hunk ranges overlap or exceed the source file")
        result.extend(source_lines[cursor:start])
        cursor = start
        for operation, text in hunk["ops"]:
            if operation in (" ", "-"):
                if cursor >= len(source_lines) or source_lines[cursor] != text:
                    raise ValueError("patch context does not match the branch file")
                if operation == " ":
                    result.append(text)
                cursor += 1
            else:
                result.append(text)
    result.extend(source_lines[cursor:])
    return "".join(result)


# ── Read: directory listing ──────────────────────────────────────────────────


class ListDirectoryInput(BaseModel):
    path: str = Field(default="", max_length=1_000, description="Folder inside the repository; empty = repository root.")
    ref: str = Field(default="", max_length=200, description="Branch, tag or commit SHA; empty = default branch.")
    recursive: bool = Field(default=False, description="List every file below `path` (whole subtree) instead of one level.")
    name_contains: str = Field(default="", max_length=200, description="Case-insensitive substring or glob (e.g. '*adr*.md') the path must match.")
    limit: int = Field(default=300, ge=1, le=MAX_TREE_ENTRIES, description="Maximum entries returned.")


def _matches(path: str, pattern: str) -> bool:
    if not pattern:
        return True
    lowered = path.lower()
    needle = pattern.lower()
    if any(ch in needle for ch in "*?["):
        return fnmatch.fnmatch(lowered, needle) or fnmatch.fnmatch(lowered.rsplit("/", 1)[-1], needle)
    return needle in lowered


async def list_repository_directory(params: ListDirectoryInput) -> dict:
    """List files and folders of the configured repository (one level, or the whole subtree with recursive=true).

    Use it to discover documentation before answering: e.g. path='docs',
    recursive=true, name_contains='*.md'. Returns path, type (file/dir),
    size and the html URL; `truncated` is true when more entries matched
    than `limit`.
    """
    try:
        path = _clean_path(params.path, allow_empty=True)
        await get_service_factory().ensure_auth()
        ref = params.ref.strip() or await _default_branch()
        repo = _repo()
        entries: list[dict[str, Any]] = []
        upstream_truncated = False
        if params.recursive:
            tree = await _api(f"repos/{repo}/git/trees/{quote(ref, safe='')}?recursive=1")
            upstream_truncated = bool(tree.get("truncated")) if isinstance(tree, dict) else False
            prefix = f"{path}/" if path else ""
            for node in tree.get("tree", []) if isinstance(tree, dict) else []:
                node_path = str(node.get("path", ""))
                if prefix and not node_path.startswith(prefix):
                    continue
                kind = "dir" if node.get("type") == "tree" else "file"
                entries.append({"path": node_path, "type": kind, "size": node.get("size")})
        else:
            suffix = f"/{quote(path, safe='/')}" if path else ""
            listing = await _api(f"repos/{repo}/contents{suffix}?ref={quote(ref, safe='')}")
            if isinstance(listing, dict):  # `path` is a file, not a folder
                listing = [listing]
            for node in listing if isinstance(listing, list) else []:
                entries.append({"path": node.get("path"), "type": node.get("type"), "size": node.get("size")})
        matched = [e for e in entries if _matches(str(e["path"]), params.name_contains.strip())]
        matched.sort(key=lambda e: (e["type"] != "dir", str(e["path"])))
        rows = matched[: params.limit]
        base = f"https://github.com/{repo}"
        for row in rows:
            view = "tree" if row["type"] == "dir" else "blob"
            row["url"] = f"{base}/{view}/{ref}/{quote(str(row['path']), safe='/')}"
        return ToolSuccess(
            data={
                "repository": repo,
                "ref": ref,
                "path": path,
                "recursive": params.recursive,
                "count": len(rows),
                "matched": len(matched),
                "truncated": len(matched) > len(rows) or upstream_truncated,
                "entries": rows,
            }
        ).model_dump()
    except ValueError as exc:
        return _invalid(str(exc), "Pass a repository-relative path such as 'docs/architecture'.")
    except Exception as exc:
        return handle_tool_error(exc, context="List repository directory failed")


# ── Read: one file ───────────────────────────────────────────────────────────


class GetFileInput(BaseModel):
    path: str = Field(min_length=1, max_length=1_000, description="File path inside the repository, e.g. 'docs/architecture/adr-process.md'.")
    ref: str = Field(default="", max_length=200, description="Branch, tag or commit SHA; empty = default branch.")
    offset: int = Field(default=0, ge=0, description="Character offset to start from (use `next_offset` to continue).")
    max_chars: int = Field(default=20_000, ge=500, le=MAX_FILE_CHARS, description="Maximum characters returned in this call.")


def _matches(path: str, pattern: str) -> bool:
    if not pattern:
        return True
    lowered = path.lower()
    needle = pattern.lower()
    if any(ch in needle for ch in "*?["):
        return fnmatch.fnmatch(lowered, needle) or fnmatch.fnmatch(lowered.rsplit("/", 1)[-1], needle)
    return needle in lowered


async def list_repository_directory(params: ListDirectoryInput) -> dict:
    """List files and folders of the configured repository (one level, or the whole subtree with recursive=true).

    Use it to discover documentation before answering: e.g. path='docs',
    recursive=true, name_contains='*.md'. Returns path, type (file/dir),
    size and the html URL; `truncated` is true when more entries matched
    than `limit`.
    """
    try:
        path = _clean_path(params.path, allow_empty=True)
        await get_service_factory().ensure_auth()
        ref = params.ref.strip() or await _default_branch()
        repo = _repo()
        entries: list[dict[str, Any]] = []
        upstream_truncated = False
        if params.recursive:
            tree = await _api(f"repos/{repo}/git/trees/{quote(ref, safe='')}?recursive=1")
            upstream_truncated = bool(tree.get("truncated")) if isinstance(tree, dict) else False
            prefix = f"{path}/" if path else ""
            for node in tree.get("tree", []) if isinstance(tree, dict) else []:
                node_path = str(node.get("path", ""))
                if prefix and not node_path.startswith(prefix):
                    continue
                kind = "dir" if node.get("type") == "tree" else "file"
                entries.append({"path": node_path, "type": kind, "size": node.get("size")})
        else:
            suffix = f"/{quote(path, safe='/')}" if path else ""
            listing = await _api(f"repos/{repo}/contents{suffix}?ref={quote(ref, safe='')}")
            if isinstance(listing, dict):  # `path` is a file, not a folder
                listing = [listing]
            for node in listing if isinstance(listing, list) else []:
                entries.append({"path": node.get("path"), "type": node.get("type"), "size": node.get("size")})
        matched = [e for e in entries if _matches(str(e["path"]), params.name_contains.strip())]
        matched.sort(key=lambda e: (e["type"] != "dir", str(e["path"])))
        rows = matched[: params.limit]
        base = f"https://github.com/{repo}"
        for row in rows:
            view = "tree" if row["type"] == "dir" else "blob"
            row["url"] = f"{base}/{view}/{ref}/{quote(str(row['path']), safe='/')}"
        return ToolSuccess(
            data={
                "repository": repo,
                "ref": ref,
                "path": path,
                "recursive": params.recursive,
                "count": len(rows),
                "matched": len(matched),
                "truncated": len(matched) > len(rows) or upstream_truncated,
                "entries": rows,
            }
        ).model_dump()
    except ValueError as exc:
        return _invalid(str(exc), "Pass a repository-relative path such as 'docs/architecture'.")
    except Exception as exc:
        return handle_tool_error(exc, context="List repository directory failed")


async def get_repository_file(params: GetFileInput) -> dict:
    """Read the text of one file from the configured repository (Markdown, YAML, code...), paged by characters.

    Use it to ground answers in the repository's own documentation and cite
    the returned `url`. When `has_more` is true call again with
    `offset=next_offset` to read the rest.
    """
    try:
        path = _clean_path(params.path, allow_empty=False)
        await get_service_factory().ensure_auth()
        ref = params.ref.strip() or await _default_branch()
        repo = _repo()
        result = await get_service_factory().gh().run(
            [
                "api",
                "-H",
                "Accept: application/vnd.github.raw+json",
                f"repos/{repo}/contents/{quote(path, safe='/')}?ref={quote(ref, safe='')}",
            ]
        )
        text = result.stdout
        if "\x00" in text[:8_000]:
            return _invalid(f"'{path}' looks like a binary file", "Only text files can be read.")
        chunk = text[params.offset : params.offset + params.max_chars]
        end = params.offset + len(chunk)
        return ToolSuccess(
            data={
                "repository": repo,
                "ref": ref,
                "path": path,
                "url": f"https://github.com/{repo}/blob/{ref}/{quote(path, safe='/')}",
                "total_chars": len(text),
                "offset": params.offset,
                "next_offset": end if end < len(text) else None,
                "has_more": end < len(text),
                "content": chunk,
            }
        ).model_dump()
    except ValueError as exc:
        return _invalid(str(exc), "Pass a repository-relative file path such as 'README.md'.")
    except Exception as exc:
        return handle_tool_error(exc, context="Get repository file failed")


class SearchCodeInput(BaseModel):
    query: str = Field(min_length=1, max_length=256, description="Words to find in file contents, e.g. 'nomenclatura servidor'.")
    path_prefix: str = Field(default="", max_length=500, description="Optional folder to restrict the search, e.g. 'docs/devops'.")
    extension: str = Field(default="", max_length=20, description="Optional file extension without dot, e.g. 'md'.")
    limit: int = Field(default=20, ge=1, le=50, description="Maximum results.")

    @field_validator("query")
    @classmethod
    def _no_scope_qualifiers(cls, value: str) -> str:
        if re.search(r"(^|\s)-?(repo|org|user):", value, flags=re.IGNORECASE):
            raise ValueError("query must not contain repo:/org:/user: qualifiers; the repository is fixed")
        return value.strip()


async def search_repository_code(params: SearchCodeInput) -> dict:
    """Search file contents of the configured repository (GitHub code search on the default branch).

    Returns matching paths, URLs and short text fragments. Code search
    indexes the default branch only and matches whole words; if it returns
    nothing, fall back to list_repository_directory(recursive=true,
    name_contains=...) and get_repository_file.
    """
    try:
        await get_service_factory().ensure_auth()
        repo = _repo()
        terms = [params.query, f"repo:{repo}"]
        if params.path_prefix.strip():
            terms.append(f"path:{_clean_path(params.path_prefix, allow_empty=False)}")
        if params.extension.strip():
            terms.append(f"extension:{params.extension.strip().lstrip('.')}")
        result = await get_service_factory().gh().run(
            [
                "api",
                "-X",
                "GET",
                "-H",
                "Accept: application/vnd.github.text-match+json",
                "search/code",
                "-f",
                f"q={' '.join(terms)}",
                "-f",
                f"per_page={params.limit}",
            ]
        )
        data = json.loads(result.stdout) if result.stdout.strip() else {}
        items = data.get("items", []) if isinstance(data, dict) else []
        rows = [
            {
                "path": item.get("path"),
                "url": item.get("html_url"),
                "fragments": [
                    bounded_text(match.get("fragment", ""), 300)
                    for match in item.get("text_matches", []) or []
                    if isinstance(match, dict)
                ][:3],
            }
            for item in items
            if isinstance(item, dict)
        ]
        total = int(data.get("total_count", len(rows))) if isinstance(data, dict) else len(rows)
        return ToolSuccess(
            data={
                "repository": repo,
                "query": " ".join(terms),
                "total_count": total,
                "count": len(rows),
                "has_more": total > len(rows),
                "incomplete_results": bool(data.get("incomplete_results")) if isinstance(data, dict) else False,
                "results": rows,
            }
        ).model_dump()
    except ValueError as exc:
        return _invalid(str(exc), "Use plain words; the repository qualifier is added automatically.")
    except Exception as exc:
        return handle_tool_error(exc, context="Search repository code failed")


def _check_branch(name: str, default_branch: str) -> str | None:
    """Return a refusal reason for `name`, or None when it may be written."""
    if not _BRANCH_RE.match(name) or ".." in name or name.endswith(("/", ".lock")) or "//" in name:
        return f"'{name}' is not a valid branch name"
    if name == default_branch:
        return f"writing to the default branch '{default_branch}' is not allowed; use a pull request"
    prefixes = get_settings().branch_prefixes()
    if not prefixes or not any(name.startswith(prefix) for prefix in prefixes):
        return (
            f"branch '{name}' must start with one of {list(prefixes)} "
            "(GH_PROJECT_WRITE_BRANCH_PREFIXES)"
        )
    return None


def _check_write_path(path: str) -> str | None:
    settings = get_settings()
    if path == ".github" or any(path.startswith(prefix) for prefix in _FORBIDDEN_PREFIXES):
        if path not in settings.github_workflow_paths():
            return (
                f"'{path}': .github files are denied unless this exact workflow "
                "path is listed in GH_PROJECT_ALLOWED_GITHUB_WORKFLOW_PATHS"
            )
    prefixes = settings.path_prefixes()
    if prefixes and not any(path.startswith(prefix) for prefix in prefixes):
        return f"'{path}' is outside the allowed paths {list(prefixes)} (GH_PROJECT_WRITE_PATH_PREFIXES)"
    return None


class CreateBranchInput(BaseModel):
    name: str = Field(min_length=1, max_length=200, description="New branch name; must start with an allowed prefix (see server_info / GH_PROJECT_WRITE_BRANCH_PREFIXES).")
    from_ref: str = Field(default="", max_length=200, description="Branch or commit SHA to start from; empty = default branch.")


async def create_branch(params: CreateBranchInput) -> dict:
    """Create a branch in the configured repository from the default branch (or `from_ref`).

    Only names with an allowed prefix are accepted and the default branch is
    never written. Use it before commit_files, then open a pull request.
    """
    try:
        await get_service_factory().ensure_auth()
        repo = _repo()
        name = params.name.strip()
        default_branch = await _default_branch()
        reason = _check_branch(name, default_branch)
        if reason:
            return _invalid(reason, "Choose a branch name with an allowed prefix.")
        source = params.from_ref.strip() or default_branch
        if re.fullmatch(r"[0-9a-f]{40}", source):
            sha = source
        else:
            ref = await _api(f"repos/{repo}/git/ref/heads/{quote(source, safe='/')}")
            sha = ((ref or {}).get("object") or {}).get("sha")
            if not sha:
                return _invalid(f"source ref '{source}' was not found", "Pass an existing branch or a full commit SHA.")
        created = await get_service_factory().gh().run(
            ["api", "--method", "POST", f"repos/{repo}/git/refs", "-f", f"ref=refs/heads/{name}", "-f", f"sha={sha}"]
        )
        payload = json.loads(created.stdout) if created.stdout.strip() else {}
        return ToolSuccess(
            data={
                "repository": repo,
                "branch": name,
                "from": source,
                "sha": ((payload or {}).get("object") or {}).get("sha", sha),
                "url": f"https://github.com/{repo}/tree/{quote(name, safe='/')}",
                "message": f"Branch '{name}' created from '{source}'.",
            }
        ).model_dump()
    except Exception as exc:
        return handle_tool_error(exc, context="Create branch failed")


class FileChange(BaseModel):
    path: str = Field(min_length=1, max_length=1_000, description="Repository-relative file path to create or overwrite.")
    content: str = Field(max_length=MAX_COMMIT_BYTES, description="Full UTF-8 text content of the file.")


class CommitFilesInput(BaseModel):
    branch: str = Field(min_length=1, max_length=200, description="Existing branch with an allowed prefix (create it with create_branch).")
    message: str = Field(min_length=1, max_length=2_000, description="Commit message headline (Conventional Commits recommended), optional body after a blank line.")
    files: list[FileChange] = Field(default_factory=list, max_length=MAX_COMMIT_FILES, description="Files to add or overwrite (full content).")
    deletions: list[str] = Field(default_factory=list, max_length=MAX_COMMIT_FILES, description="Paths to delete.")
    expected_head_sha: str = Field(default="", max_length=40, description="Optional branch head SHA the commit must build on (optimistic concurrency).")
    patch: str = Field(default="", max_length=MAX_PATCH_BYTES, description="Optional bounded unified diff. Requires expected_head_sha; cannot be combined with files or deletions.")


_CREATE_COMMIT = """
mutation($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) {
    commit { oid url }
  }
}
"""


async def commit_files(params: CommitFilesInput) -> dict:
    """Commit file additions/updates/deletions to an allowed branch as the authenticated user (createCommitOnBranch).

    Accepts full file contents or a bounded unified diff tied to an expected
    branch-head SHA. Refuses the default branch, unlisted .github paths, and
    paths outside GH_PROJECT_WRITE_PATH_PREFIXES. Patch commits apply only
    when every hunk matches the branch content. Always follow with
    create_pull_request; nothing reaches the default branch directly.
    """
    try:
        await get_service_factory().ensure_auth()
        repo = _repo()
        branch = params.branch.strip()
        files = list(params.files)
        deletions = list(params.deletions)
        if params.patch:
            if files or deletions:
                return _invalid("patch cannot be combined with files or deletions", "Pass either patch or full file contents.")
            expected = params.expected_head_sha.strip()
            if not re.fullmatch(r"[0-9a-f]{40}", expected):
                return _invalid("patch requires a 40-character expected_head_sha", "Read the branch head and include it to prevent stale patches.")
            if len(params.patch.encode("utf-8")) > MAX_PATCH_BYTES:
                return _invalid("patch exceeds the byte limit", "Split the change into smaller reviewed commits.")
            try:
                patch_files = _parse_unified_patch(params.patch)
            except ValueError as exc:
                return _invalid(str(exc), "Pass a text unified diff without renames or binary changes.")
        else:
            patch_files = []
        if not files and not deletions and not patch_files:
            return _invalid("nothing to commit", "Pass at least one file or deletion.")
        default_branch = await _default_branch()
        reason = _check_branch(branch, default_branch)
        if reason:
            return _invalid(reason, "Create an allowed branch with create_branch first.")
        if patch_files:
            repo_head = await _api(f"repos/{repo}/git/ref/heads/{quote(branch, safe='/')}")
            actual_head = ((repo_head or {}).get("object") or {}).get("sha")
            if actual_head != params.expected_head_sha:
                return _invalid("branch head differs from expected_head_sha", "Refresh the branch diff and retry against its current SHA.")
            for item in patch_files:
                old_path = item["old_path"]
                new_path = item["new_path"]
                for path in (old_path, new_path):
                    if path:
                        clean = _clean_path(path, allow_empty=False)
                        refusal = _check_write_path(clean)
                        if refusal:
                            return _invalid(refusal, "Only change files inside the allowed folders.")
                original = ""
                if old_path:
                    result = await get_service_factory().gh().run(
                        [
                            "api",
                            "-H",
                            "Accept: application/vnd.github.raw+json",
                            f"repos/{repo}/contents/{quote(old_path, safe='/')}?ref={quote(branch, safe='')}",
                        ]
                    )
                    original = result.stdout
                updated = _apply_hunks(original, item["hunks"])
                if new_path is None:
                    deletions.append(old_path)
                else:
                    files.append(FileChange(path=new_path, content=updated))
        additions = []
        total = 0
        seen: set[str] = set()
        try:
            for change in files:
                path = _clean_path(change.path, allow_empty=False)
                encoded = change.content.encode("utf-8")
                total += len(encoded)
                additions.append({"path": path, "contents": base64.b64encode(encoded).decode("ascii")})
                seen.add(path)
            deletions = [_clean_path(p, allow_empty=False) for p in deletions]
        except ValueError as exc:
            return _invalid(str(exc), "Use repository-relative paths such as 'docs/adr/ADR-0006.md'.")
        if seen & set(deletions):
            return _invalid("a path cannot be both written and deleted", "Remove the duplicate path.")
        for path in [*seen, *deletions]:
            refusal = _check_write_path(path)
            if refusal:
                return _invalid(refusal, "Only change files inside the allowed folders.")
        if total > MAX_COMMIT_BYTES:
            return _invalid(f"commit too large ({total} bytes > {MAX_COMMIT_BYTES})", "Split the change into smaller commits.")
        head = params.expected_head_sha.strip()
        if not head:
            ref = await _api(f"repos/{repo}/git/ref/heads/{quote(branch, safe='/')}")
            head = ((ref or {}).get("object") or {}).get("sha", "")
            if not head:
                return _invalid(f"branch '{branch}' was not found", "Create it with create_branch first.")
        headline, _, body = params.message.strip().partition("\n")
        message: dict[str, str] = {"headline": headline.strip()[:250]}
        if body.strip():
            message["body"] = body.strip()
        variables = {
            "input": {
                "branch": {"repositoryNameWithOwner": repo, "branchName": branch},
                "message": message,
                "expectedHeadOid": head,
                "fileChanges": {"additions": additions, "deletions": [{"path": p} for p in deletions]},
            }
        }
        executor = await get_service_factory().graphql()
        response = await executor.execute_with_retry(_CREATE_COMMIT, variables, is_mutation=True)
        data = response.get("data", response) if isinstance(response, dict) else {}
        commit = ((data or {}).get("createCommitOnBranch") or {}).get("commit") or {}
        if not commit.get("oid"):
            return build_error_response(
                error_type="internal",
                message="GitHub did not return the created commit",
                suggestion="Check the branch with list_repository_directory(ref=branch) before retrying; the commit may not exist.",
            )
        return ToolSuccess(
            data={
                "repository": repo,
                "branch": branch,
                "commit_sha": commit.get("oid"),
                "commit_url": commit.get("url"),
                "changed": sorted(seen),
                "deleted": deletions,
                "next_step": f"Open a pull request with create_pull_request(head='{branch}', base='{default_branch}').",
            }
        ).model_dump()
    except Exception as exc:
        return handle_tool_error(exc, context="Commit files failed")


REPOSITORY_CONTENT_TOOLS = [
    list_repository_directory,
    get_repository_file,
    search_repository_code,
    create_branch,
    commit_files,
]

__all__ = ["REPOSITORY_CONTENT_TOOLS", *(fn.__name__ for fn in REPOSITORY_CONTENT_TOOLS)]