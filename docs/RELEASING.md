# Releasing

This repository uses [Semantic Versioning](https://semver.org/) and publishes
releases automatically after a pull request merges to `main`. Maintainers do
not normally create tags, edit version constants, or assemble release notes by
hand.

The published artifacts are:

- Git tag `vX.Y.Z` on the automated release commit.
- GitHub release `vX.Y.Z` with the matching `CHANGELOG.md` section as notes.
- Multi-architecture GHCR image (`linux/amd64` and `linux/arm64`) tagged as:
  - `ghcr.io/jersonmartinez/mcp-github-projects:X.Y.Z` — immutable exact version;
  - `ghcr.io/jersonmartinez/mcp-github-projects:X.Y` — latest patch in the minor line;
  - `ghcr.io/jersonmartinez/mcp-github-projects:latest` — latest stable release.

## Automated flow

`.github/workflows/release-automation.yaml` runs on every push to `main`. Direct pushes that cannot be associated with a merged pull request are skipped; only reviewed PR merges publish automatically:

1. It resolves the merged pull request from either a merge commit
   (`Merge pull request #N`) or a squash commit (`... (#N)`), then reads the
   PR title, author, labels, and body through the GitHub API.
2. It chooses the semver bump described below. A PR labelled `release:none`
   stops here and publishes nothing.
3. It computes the base version as the maximum of existing `v*` tags and the
   version currently declared in code. Using the code version as a lower bound
   prevents legacy missing tags from moving the project backwards.
4. It updates `CHANGELOG.md`: the previous `[Unreleased]` content is moved
   into the new `## [X.Y.Z]` section, followed by generated release metadata
   with emojis, the PR attribution, compare link, merge commit, semver bump,
   and exact container-image tags.
5. It updates `core/version.py` (`VERSION`) to the same `X.Y.Z`.
6. It commits those two files to `main` as `chore(release): vX.Y.Z` using the
   workflow-scoped `GITHUB_TOKEN`, creates annotated tag `vX.Y.Z`, and pushes
   both.
7. It dispatches `.github/workflows/release.yaml` for that exact tag/commit.
   The explicit dispatch is intentional: GitHub does not use a `GITHUB_TOKEN`
   tag push to start another push-triggered workflow, so relying on the tag
   event would silently do nothing.
8. `release.yaml` verifies tag/code/CHANGELOG consistency, tests the tagged
   tree, builds and pushes the image with supply-chain attestations, and only
   then publishes the GitHub release.

The automation is serialized with a non-cancelling concurrency group. Before pushing,
it resets its ephemeral checkout to the latest `origin/main` and recomputes the
version and changelog. In the normal case each merge receives its own immutable
patch/minor/major version. If several merges race the automation queue, the first
published release commit can include more than one merge; superseded runs detect
that release commit and skip instead of publishing duplicate artifacts.

## Choosing the release type

Explicit labels win over title inference:

| Label | Result |
|---|---|
| `release:none` | No version, tag, package, or GitHub release |
| `release:major` | `X+1.0.0` |
| `release:minor` | `X.Y+1.0` |
| `release:patch` | `X.Y.Z+1` |

If several bump labels are present, the most severe wins (`major` → `minor` →
`patch`), while `release:none` wins over all of them. Use `release:none` for
documentation-only or repository-maintenance PRs that users do not need to
pull as a new image.

Without an explicit label, the merged PR title is interpreted as a
Conventional Commit:

| PR title shape | Bump | Release-notes section |
|---|---:|---|
| `feat: ...` / `feat(scope): ...` | minor | 🚀 Features |
| `fix: ...` | patch | 🐛 Bug Fixes |
| `perf: ...` | patch | ⚡ Performance |
| `security: ...` or `security` label | patch | 🔒 Security |
| `docs: ...` | patch | 📚 Documentation |
| `ci: ...` | patch | 🛠️ CI/CD |
| `deps: ...` / `dependencies` label | patch | ⬆️ Dependencies |
| `type!: ...` or `BREAKING CHANGE:` in the PR body | major | type-specific section |
| anything else | patch | 🧹 Maintenance |

Labels also influence the release-notes category. For example, a `ci`-titled
security hardening PR with the `security` label is rendered under
`🔒 Security` rather than CI/CD.

## What users see

A generated release section starts like this:

```markdown
## [1.6.0] - 2026-01-15

### 🚀 Features

- **Add project automation** by @jersonmartinez in [#74](...).

### 📦 Release data

- **Version:** `v1.6.0` (`minor` bump from `v1.5.1` — new user-facing capability).
- **Compatibility:** ✅ backwards-compatible feature release.
- **Source:** pull request [#74](...); merge commit `abc123def456`.
- **Compare:** [`v1.5.1...v1.6.0`](...).
- **Container image:** `ghcr.io/jersonmartinez/mcp-github-projects:1.6.0` (also `:1.6` and `:latest`).
- **Upgrade:** `docker pull ghcr.io/jersonmartinez/mcp-github-projects:1.6.0`.
```

Curated entries that were already accumulated under `[Unreleased]` are kept in
full before `### 📦 Release data`. Do not delete them in feature PRs; the
automation moves them automatically.

## Operating rules

- Direct pushes to `main` do **not** create releases. If an emergency commit was pushed outside a PR, cut the release manually or move the change through a PR; this keeps publication tied to review metadata.

- Do not manually bump `core/version.py` (`VERSION`) in ordinary feature PRs. The
  automation owns it on `main`.
- Do not create release sections or tags manually unless the automation is
  broken. Manual intervention is documented below.
- The release commit and tag are immutable. Never move or repush a tag that
  may already have produced an image; cut a new patch release instead.
- `contents: write` is limited to the release-automation job. No PAT or
  long-lived repository secret is required.
- The current repository does not require pull-request approval before the
  automation writes to `main`. If branch protection is enabled later, the bot
  identity must be allowed to push release commits, or this workflow must be
  changed to use a carefully scoped GitHub App.

## Recovering from failures

The pipeline is ordered to avoid publishing partial user-facing artifacts:
version/changelog verification and tests happen before the image push, and the
GitHub release is created last.

| Failure point | State | Recovery |
|---|---|---|
| Before release commit | Nothing changed | Fix the workflow/script and merge another PR, or run the manual fallback. |
| Release commit pushed, tag missing | `main` already has version `X.Y.Z` | Create/push tag `vX.Y.Z` manually, then dispatch `release.yaml` for it. |
| Tag pushed, dispatch failed | No image/release published | Run `gh workflow run release.yaml -f tag=vX.Y.Z -f ref=<release-sha>`. |
| Build/test/push failed | Tag exists; release normally absent | Fix the cause and redispatch the same tag. Do not move the tag. |
| GitHub release creation failed | Image may exist | Redispatch the same tag after fixing permissions/release state. |
| Non-fast-forward push | Two main branches raced | The workflow resets and recomputes up to three times; if it still fails, inspect the newer `main` and rerun the failed workflow after the race settles. |

A manual fallback remains available:

```bash
git fetch origin --tags
git checkout main && git pull --ff-only
# Set `core/version.py` (`VERSION`) and CHANGELOG.md consistently first.
git commit -am 'chore(release): vX.Y.Z'
git push origin main
git tag -a vX.Y.Z -m vX.Y.Z
git push origin refs/tags/vX.Y.Z
gh workflow run release.yaml -f tag=vX.Y.Z -f ref="$(git rev-parse HEAD)"
```

## Verifying a published release

```bash
docker pull ghcr.io/jersonmartinez/mcp-github-projects:X.Y.Z
```

The first time a repository publishes a GHCR package, GitHub may create it as
**private**. Open the package on GitHub → *Package settings* → *Change
visibility* → *Public* if anonymous pulls are intended.
