# Releasing

Releases follow [Semantic Versioning](https://semver.org/). A release is a
`vX.Y.Z` tag on `main`; `.github/workflows/release.yaml` does the rest.

## What the workflow does

1. `scripts/release_notes.py vX.Y.Z` refuses the release unless the tag
   matches `VERSION` in `core/version.py` and `CHANGELOG.md` has a non-empty
   `## [X.Y.Z]` section. That section becomes the release notes.
2. Builds the test image and runs the suite on the tagged tree.
3. Builds the runtime image for `linux/amd64` and `linux/arm64` and pushes it
   to GHCR as `ghcr.io/jersonmartinez/mcp-github-projects` with the tags
   `X.Y.Z`, `X.Y` and `latest`.
4. Publishes the GitHub release for the tag.

`server_info` reports the running version (`data.version`), and the MCP
handshake advertises it as the server version.

## Steps

1. In a PR: move the `[Unreleased]` entries of `CHANGELOG.md` under
   `## [X.Y.Z] - YYYY-MM-DD` and set `VERSION` in `core/version.py`. Check:

   ```bash
   docker run --rm mcp-github-projects:latest python3 scripts/release_notes.py vX.Y.Z
   ```

2. Merge the PR once every check is green.
3. Tag the merge commit on `main` and push the tag:

   ```bash
   git fetch origin && git tag -a vX.Y.Z origin/main -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```

4. Watch the **Release** workflow; when it is green, confirm the image:

   ```bash
   docker pull ghcr.io/jersonmartinez/mcp-github-projects:X.Y.Z
   ```

The first time a package is published, GHCR may create it as **private**. To
allow anonymous `docker pull`, open the package on GitHub → *Package
settings* → *Change visibility* → *Public* (the repository is public).

## If a release fails

Nothing is published before the version check and the tests pass, and the
GitHub release is created last, so a failed run can be fixed and re-run from
the Actions tab for the same tag. Do not move or re-push a tag that already
produced a published image; release a new patch version instead.
