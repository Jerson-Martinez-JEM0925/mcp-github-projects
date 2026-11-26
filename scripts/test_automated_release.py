#!/usr/bin/env python3
"""Unit tests for scripts/automated_release.py (stdlib unittest)."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("automated_release.py")
SPEC = importlib.util.spec_from_file_location("automated_release", MODULE_PATH)
assert SPEC and SPEC.loader
automated_release = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = automated_release
SPEC.loader.exec_module(automated_release)


class AutomatedReleaseTests(unittest.TestCase):
    """Cover versioning policy, categorization, and changelog rewriting."""

    def test_release_labels_take_precedence(self) -> None:
        self.assertEqual(
            automated_release.choose_bump("feat(api): add thing", ["release:patch"]),
            "patch",
        )
        self.assertEqual(
            automated_release.choose_bump("fix(api): repair thing", ["release:none"]),
            "none",
        )

    def test_conventional_commit_fallback(self) -> None:
        self.assertEqual(automated_release.choose_bump("feat(api): add thing", []), "minor")
        self.assertEqual(automated_release.choose_bump("fix(api): repair thing", []), "patch")
        self.assertEqual(automated_release.choose_bump("feat(api)!: break thing", []), "major")
        self.assertEqual(
            automated_release.choose_bump("fix(api): repair", [], "BREAKING CHANGE: contract"),
            "major",
        )

    def test_category_uses_labels_and_conventional_type(self) -> None:
        section, change_type, _ = automated_release.category_for("ci: harden pipeline", ["ci", "security"])
        self.assertEqual(section, "🔒 Security")
        self.assertEqual(change_type, "security")
        section, change_type, _ = automated_release.category_for("perf(core): reduce latency", [])
        self.assertEqual(section, "⚡ Performance")
        self.assertEqual(change_type, "perf")

    def test_semver_bumps(self) -> None:
        self.assertEqual(automated_release.bump_version((1, 5, 1), "patch"), (1, 5, 2))
        self.assertEqual(automated_release.bump_version((1, 5, 1), "minor"), (1, 6, 0))
        self.assertEqual(automated_release.bump_version((1, 5, 1), "major"), (2, 0, 0))

    def test_merge_message_pr_number_formats(self) -> None:
        self.assertEqual(
            automated_release.pull_request_number_from_message("Merge pull request #73 from x/y\n"),
            73,
        )
        self.assertEqual(
            automated_release.pull_request_number_from_message("feat(api): add thing (#165)\n"),
            165,
        )
        self.assertEqual(automated_release.pull_request_number_from_message("direct push\n"), 0)

    def test_release_entry_contains_user_operational_data(self) -> None:
        pr = automated_release.PullRequestInfo(
            number=12,
            title="feat(api): add useful thing",
            author="jersonmartinez",
            url="https://github.com/acme/repo/pull/12",
        )
        entry = automated_release.release_entry(
            version="1.6.0",
            base_version="1.5.1",
            compare_base="1.5.1",
            bump="minor",
            change_type="feat",
            explanation="new user-facing capability",
            section="🚀 Features",
            pr=pr,
            sha="abcdef1234567890",
            repo="acme/repo",
            image="ghcr.io/acme/repo",
            accumulated="### Added\n\n- Existing unreleased note.",
            today="2026-01-02",
        )
        self.assertIn("## [1.6.0] - 2026-01-02", entry)
        self.assertIn("### 🚀 Features", entry)
        self.assertIn("[#12](https://github.com/acme/repo/pull/12)", entry)
        self.assertIn("ghcr.io/acme/repo:1.6.0", entry)
        self.assertIn("docker pull ghcr.io/acme/repo:1.6.0", entry)
        self.assertIn("backwards-compatible feature release", entry)
        self.assertIn("curated changes that were accumulated", entry)
        self.assertIn("v1.5.1...v1.6.0", entry)
        self.assertIn("Existing unreleased note", entry)

    def test_changelog_update_moves_unreleased_content(self) -> None:
        old_root = automated_release.ROOT
        with tempfile.TemporaryDirectory() as directory:
            automated_release.ROOT = Path(directory)
            changelog = Path(directory) / "CHANGELOG.md"
            changelog.write_text(
                "# Changelog\n\n## [Unreleased] — themed\n\n### Added\n\n- Pending note.\n\n"
                "## [1.0.0] - 2025-01-01\n\n- Old.\n",
                encoding="utf-8",
            )
            automated_release.update_changelog("## [1.1.0] - 2025-02-01\n\n### Added\n\n- Pending note.\n")
            updated = changelog.read_text(encoding="utf-8")
        automated_release.ROOT = old_root
        self.assertIn("## [Unreleased]\n\n## [1.1.0]", updated)
        self.assertLess(updated.index("## [1.1.0]"), updated.index("## [1.0.0]"))

    def test_python_version_rewrite_is_exact(self) -> None:
        old_root = automated_release.ROOT
        with tempfile.TemporaryDirectory() as directory:
            automated_release.ROOT = Path(directory)
            core = Path(directory) / "core"
            core.mkdir()
            version_file = core / "version.py"
            version_file.write_text('VERSION = "1.5.1"\n', encoding="utf-8")
            automated_release.write_current_version("python", "1.6.0")
            self.assertEqual(version_file.read_text(encoding="utf-8"), 'VERSION = "1.6.0"\n')
        automated_release.ROOT = old_root


if __name__ == "__main__":
    unittest.main()
