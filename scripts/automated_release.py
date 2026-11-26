#!/usr/bin/env python3
"""Prepare an automated semantic-version release after a merge to main.

The script is dependency-free so it can run on a bare GitHub Actions Ubuntu
runner before either project's Docker image exists. It gathers merged PR
metadata with the preinstalled ``gh`` CLI, chooses the semver bump, updates
the version source file and CHANGELOG.md, and writes GitHub Actions outputs.
Git commit/tag/push/dispatch operations stay in the workflow so credentials
and failure behavior are explicit.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
CONVENTIONAL_RE = re.compile(r"^(?P<type>[A-Za-z]+)(?:\([^()\n]+\))?(?P<breaking>!)?:\s*(?P<summary>.+)$")

CATEGORIES = {
    "feat": ("🚀 Features", "minor", "new user-facing capability"),
    "fix": ("🐛 Bug Fixes", "patch", "corrected behavior"),
    "perf": ("⚡ Performance", "patch", "performance improvement"),
    "security": ("🔒 Security", "patch", "security hardening"),
    "docs": ("📚 Documentation", "patch", "documentation update"),
    "refactor": ("🧹 Maintenance", "patch", "internal maintenance"),
    "test": ("✅ Testing", "patch", "test coverage or infrastructure"),
    "ci": ("🛠️ CI/CD", "patch", "continuous integration automation"),
    "build": ("📦 Build", "patch", "build or packaging update"),
    "chore": ("🧹 Maintenance", "patch", "repository maintenance"),
    "revert": ("↩️ Reverts", "patch", "reverted change"),
    "deps": ("⬆️ Dependencies", "patch", "dependency update"),
    "hotfix": ("🚑 Hotfixes", "patch", "urgent production fix"),
}
LABEL_CATEGORY = {
    "bug": "fix",
    "enhancement": "feat",
    "feature": "feat",
    "documentation": "docs",
    "docs": "docs",
    "security": "security",
    "dependencies": "deps",
    "ci": "ci",
    "test": "test",
    "refactor": "refactor",
}


@dataclass
class PullRequestInfo:
    """Metadata used in release notes; empty values mean a direct push."""

    number: int = 0
    title: str = ""
    author: str = ""
    url: str = ""
    labels: list[str] = field(default_factory=list)
    body: str = ""


def parse_version(value: str) -> tuple[int, int, int] | None:
    """Parse a strict semver string or tag."""
    match = SEMVER_RE.fullmatch(value.strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def format_version(version: tuple[int, int, int]) -> str:
    """Format a semver tuple without the leading v."""
    return f"{version[0]}.{version[1]}.{version[2]}"


def conventional_parts(title: str) -> tuple[str, bool, str]:
    """Return (type, breaking, summary) for a Conventional Commit title."""
    match = CONVENTIONAL_RE.match(title.strip())
    if not match:
        return "chore", False, title.strip() or "Repository update"
    return (
        match.group("type").lower(),
        bool(match.group("breaking")),
        match.group("summary").strip(),
    )


def choose_bump(title: str, labels: list[str], body: str = "") -> str:
    """Choose major/minor/patch/none, with release labels taking precedence."""
    normalized = {label.strip().lower() for label in labels}
    if "release:none" in normalized:
        return "none"
    if "release:major" in normalized:
        return "major"
    if "release:minor" in normalized:
        return "minor"
    if "release:patch" in normalized:
        return "patch"
    change_type, breaking, _ = conventional_parts(title)
    if breaking or re.search(r"(?im)^BREAKING[ -]CHANGE:\s*.+", body or ""):
        return "major"
    if change_type == "feat":
        return "minor"
    return "patch"


def category_for(title: str, labels: list[str]) -> tuple[str, str, str]:
    """Return the Markdown section, type key and explanation for a change."""
    normalized = {label.strip().lower() for label in labels}
    change_type, _, _ = conventional_parts(title)
    # Labels are prioritized rather than sorted alphabetically: a security fix
    # that also required CI work must be presented as security-relevant to users.
    label_priority = (
        "security", "bug", "enhancement", "feature", "dependencies",
        "documentation", "docs", "ci", "test", "refactor",
    )
    for label in label_priority:
        if label in normalized:
            change_type = LABEL_CATEGORY[label]
            break
    section, _, explanation = CATEGORIES.get(change_type, CATEGORIES["chore"])
    return section, change_type, explanation


def bump_version(version: tuple[int, int, int], bump: str) -> tuple[int, int, int]:
    """Apply a semver bump."""
    major, minor, patch = version
    if bump == "major":
        return major + 1, 0, 0
    if bump == "minor":
        return major, minor + 1, 0
    if bump == "patch":
        return major, minor, patch + 1
    raise ValueError(f"unsupported bump {bump!r}")


def read_current_version(kind: str) -> str:
    """Read the repository's single-source-of-truth version."""
    if kind == "python":
        text = (ROOT / "core/version.py").read_text(encoding="utf-8")
        match = re.search(r'^VERSION\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"', text, re.MULTILINE)
    elif kind == "go":
        text = (ROOT / "internal/mcpserver/server.go").read_text(encoding="utf-8")
        match = re.search(r'^const Version = "([0-9]+\.[0-9]+\.[0-9]+)"', text, re.MULTILINE)
    else:
        raise ValueError(f"unsupported kind {kind!r}")
    if not match:
        raise ValueError(f"could not find the {kind} version declaration")
    return match.group(1)


def write_current_version(kind: str, version: str) -> None:
    """Replace exactly one version declaration in the appropriate source file."""
    if kind == "python":
        path = ROOT / "core/version.py"
        pattern = r'^(VERSION\s*=\s*")([0-9]+\.[0-9]+\.[0-9]+)("\s*)$'
    else:
        path = ROOT / "internal/mcpserver/server.go"
        pattern = r'^(const Version = ")([0-9]+\.[0-9]+\.[0-9]+)(")$'
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, rf"\g<1>{version}\g<3>", text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f"expected exactly one version declaration in {path}")
    path.write_text(updated, encoding="utf-8", newline="\n")


def tagged_versions() -> list[tuple[int, int, int]]:
    """Return all strict semver versions represented by local tags."""
    result = subprocess.run(
        ["git", "tag", "--list", "v*.*.*"],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    versions = []
    for line in result.stdout.splitlines():
        parsed = parse_version(line)
        if parsed:
            versions.append(parsed)
    return versions


def commit_message(sha: str) -> str:
    """Read the complete message for the pushed commit."""
    result = subprocess.run(
        ["git", "log", "-1", "--pretty=%B", sha],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return result.stdout.strip()


def current_head() -> str:
    """Return the currently checked-out commit SHA."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return result.stdout.strip()


def gh_api(path: str) -> object:
    """Call the GitHub API through the preinstalled gh CLI."""
    result = subprocess.run(
        ["gh", "api", path],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return json.loads(result.stdout)


def pull_request_number_from_message(message: str) -> int:
    """Extract the PR number from merge-commit and squash-commit subjects."""
    patterns = [
        r"^Merge pull request #(\d+)\b",
        r"\(#(\d+)\)\s*$",
        r"\bpull request #(\d+)\b",
    ]
    first_lines = "\n".join(message.splitlines()[:3])
    for pattern in patterns:
        match = re.search(pattern, first_lines, re.MULTILINE)
        if match:
            return int(match.group(1))
    return 0


def pull_request_from_payload(payload: dict[str, object]) -> PullRequestInfo:
    """Normalize the subset of PR API data needed by release notes."""
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    raw_labels = payload.get("labels") if isinstance(payload.get("labels"), list) else []
    labels = []
    for item in raw_labels:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            labels.append(item["name"])
    return PullRequestInfo(
        number=int(payload.get("number") or 0),
        title=str(payload.get("title") or ""),
        author=str(user.get("login") or "unknown"),
        url=str(payload.get("html_url") or ""),
        labels=labels,
        body=str(payload.get("body") or ""),
    )


def resolve_pull_request(repo: str, sha: str, message: str) -> PullRequestInfo:
    """Resolve the merged PR for merge and squash commit shapes.

    A commit that clearly references a PR fails closed when metadata cannot be
    read: silently falling back could ignore release:none or a breaking-change
    label. Only true direct pushes use the direct-push fallback.
    """
    number = pull_request_number_from_message(message)
    if number:
        try:
            payload = gh_api(f"repos/{repo}/pulls/{number}")
            if isinstance(payload, dict):
                return pull_request_from_payload(payload)
        except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"could not resolve merged pull request #{number}: {exc}") from exc
        raise ValueError(f"GitHub API returned no pull request object for #{number}")
    try:
        associated = gh_api(f"repos/{repo}/commits/{sha}/pulls")
        if isinstance(associated, list) and associated and isinstance(associated[0], dict):
            return pull_request_from_payload(associated[0])
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        print(f"warning: could not resolve associated pull request metadata: {exc}", file=sys.stderr)
    subject = message.splitlines()[0] if message else "Repository update"
    return PullRequestInfo(title=subject, author="unknown")


def unreleased_body(lines: list[str]) -> tuple[int, int, str]:
    """Return indexes and body of the CHANGELOG Unreleased section."""
    start = next((i for i, line in enumerate(lines) if line.startswith("## [Unreleased]")), -1)
    if start < 0:
        raise ValueError("CHANGELOG.md has no '## [Unreleased]' section")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## [")), len(lines))
    return start, end, "\n".join(lines[start + 1 : end]).strip()


def markdown_title(title: str) -> str:
    """Use the human part of a Conventional Commit title in notes."""
    _, _, summary = conventional_parts(title)
    return summary[:1].upper() + summary[1:] if summary else "Repository update"


def release_entry(
    *,
    version: str,
    base_version: str,
    compare_base: str,
    bump: str,
    change_type: str,
    explanation: str,
    section: str,
    pr: PullRequestInfo,
    sha: str,
    repo: str,
    image: str,
    accumulated: str,
    batching: bool = False,
    today: str | None = None,
) -> str:
    """Build a professional, user-facing release section."""
    date = today or datetime.now(timezone.utc).date().isoformat()
    short_sha = sha[:12]
    compare = f"https://github.com/{repo}/compare/v{compare_base}...v{version}"
    if pr.number:
        attribution = f"by @{pr.author} in [#{pr.number}]({pr.url})"
        source = f"pull request [#{pr.number}]({pr.url})"
    else:
        attribution = f"by @{pr.author}" if pr.author != "unknown" else "from a direct push"
        source = "direct push to `main`"
    major_minor = version.rsplit(".", 1)[0]
    compatibility = {
        "major": "⚠️ potentially breaking; review the compare link before upgrading",
        "minor": "✅ backwards-compatible feature release",
        "patch": "✅ backwards-compatible fix or maintenance release",
    }[bump]
    lines = [
        f"## [{version}] - {date}",
        "",
        f"### {section}",
        "",
        f"- **{markdown_title(pr.title)}** {attribution}.",
        "",
    ]
    if accumulated:
        lines.extend([accumulated.rstrip(), ""])
    release_data = [
        "### 📦 Release data",
        "",
        f"- **Version:** `v{version}` (`{bump}` bump from `v{base_version}` — {explanation}).",
        f"- **Compatibility:** {compatibility}.",
        f"- **Source:** {source}; merge commit `{short_sha}`.",
    ]
    if accumulated:
        release_data.append(
            "- **Scope:** includes curated changes that were accumulated under `[Unreleased]` before automation."
        )
    if batching:
        release_data.append(
            "- **Batching:** `main` advanced before automation ran, so the compare may include later merged pull requests."
        )
    release_data.append(f"- **Compare:** [`v{compare_base}...v{version}`]({compare}).")
    lines.extend(release_data)
    if compare_base != base_version:
        lines.append(
            f"  The previous declared version `v{base_version}` has no git tag; "
            f"the comparison starts at the last published tag `v{compare_base}`."
        )
    lines += [
        f"- **Container image:** `{image}:{version}` (also `{image}:{major_minor}` and `{image}:latest`).",
        f"- **Upgrade:** `docker pull {image}:{version}`.",
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


def update_changelog(entry: str) -> None:
    """Move Unreleased content into the new release and insert the entry."""
    path = ROOT / "CHANGELOG.md"
    lines = path.read_text(encoding="utf-8").splitlines()
    start, end, _ = unreleased_body(lines)
    # Normalize the heading: feature PRs may use "Unreleased — theme", but
    # after automation moves that content the next unreleased section starts empty.
    prefix = "\n".join([*lines[:start], "## [Unreleased]"]).rstrip() + "\n\n"
    suffix = "\n".join(lines[end:]).lstrip("\n")
    updated = prefix + entry.rstrip() + "\n\n" + suffix
    path.write_text(updated.rstrip() + "\n", encoding="utf-8", newline="\n")


def write_outputs(path: str | None, values: dict[str, str]) -> None:
    """Write GitHub Actions outputs when requested."""
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def prepare(args: argparse.Namespace) -> int:
    """Prepare files and workflow outputs for the release commit."""
    message = commit_message(args.sha)
    pr = resolve_pull_request(args.repo, args.sha, message)
    if not pr.number:
        write_outputs(args.github_output, {"skip": "true", "reason": "no merged pull request"})
        print("release skipped: direct pushes to main do not publish automatically")
        return 0
    bump = choose_bump(pr.title or message.splitlines()[0], pr.labels, pr.body)
    if bump == "none":
        write_outputs(args.github_output, {"skip": "true", "reason": "release:none label"})
        print("release skipped: pull request has the release:none label")
        return 0

    current = parse_version(read_current_version(args.kind))
    if current is None:
        raise ValueError("current version declaration is not strict semver")
    versions = tagged_versions()
    base = max(versions + [current])
    if current not in versions and current > max(versions or [(0, 0, 0)]):
        print(
            f"warning: no tag exists for current version v{format_version(current)}; "
            "using it as the base instead of an older legacy tag",
            file=sys.stderr,
        )
    new_version_tuple = bump_version(base, bump)
    version = format_version(new_version_tuple)
    base_version = format_version(base)
    latest_tag = max(versions) if versions else base
    compare_base = base_version if base in versions else format_version(latest_tag)
    section, change_type, explanation = category_for(pr.title, pr.labels)
    changelog_lines = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
    _, _, accumulated = unreleased_body(changelog_lines)
    entry = release_entry(
        version=version,
        base_version=base_version,
        compare_base=compare_base,
        bump=bump,
        change_type=change_type,
        explanation=explanation,
        section=section,
        pr=pr,
        sha=args.sha,
        repo=args.repo,
        image=args.image,
        accumulated=accumulated,
        batching=current_head() != args.sha,
    )
    write_current_version(args.kind, version)
    update_changelog(entry)
    write_outputs(
        args.github_output,
        {
            "skip": "false",
            "version": version,
            "tag": f"v{version}",
            "base_version": base_version,
            "bump": bump,
            "change_type": change_type,
            "pr_number": str(pr.number),
        },
    )
    print(f"prepared release v{version} ({bump} bump from v{base_version})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare", help="prepare version and changelog files")
    prepare_parser.add_argument("--repo", required=True, help="owner/repository")
    prepare_parser.add_argument("--sha", required=True, help="merge commit SHA")
    prepare_parser.add_argument("--kind", choices=("python", "go"), required=True)
    prepare_parser.add_argument("--image", required=True, help="GHCR image without tag")
    prepare_parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT", ""))
    prepare_parser.set_defaults(func=prepare)
    return parser


def main(argv: list[str]) -> int:
    """CLI entry point."""
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
