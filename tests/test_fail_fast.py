"""Clean fail-fast on misconfiguration (#3).

A missing or invalid setting is an operator error: the server must print one
actionable line to stderr and exit with CONFIG_ERROR_EXIT_CODE, never a Python
traceback. Entry points are exercised as real subprocesses with a scrubbed
environment so ambient GH_PROJECT_* values cannot mask the behaviour.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import (  # noqa: E402
    CONFIG_ERROR_EXIT_CODE,
    GitHubProjectSettings,
    config_error_summary,
)


def _scrubbed_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GH_PROJECT_")}
    env.pop("MCP_PROFILE", None)
    env.pop("MCP_ACCESS_LEVEL", None)
    env.update({"GH_TOKEN": "placeholder", "PYTHONPATH": str(ROOT)})
    env.update(extra)
    return env


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # cwd is a temp dir so no repo-local .env file is picked up.
    return subprocess.run(
        [sys.executable, *args],
        cwd=os.environ.get("TMPDIR", "/tmp"),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize(
    "entry",
    [[str(ROOT / "server.py")], [str(ROOT / "__main__.py")]],
    ids=["server.py", "__main__.py"],
)
def test_missing_target_exits_cleanly(entry: list[str]) -> None:
    result = _run(entry, _scrubbed_env())
    assert result.returncode == CONFIG_ERROR_EXIT_CODE
    assert "Traceback" not in result.stderr
    assert "Configuration error: Missing required configuration" in result.stderr
    assert "GH_PROJECT_ORG_NAME" in result.stderr
    assert result.stdout == "", "stdout is reserved for the MCP protocol"


def test_invalid_access_level_exits_cleanly() -> None:
    env = _scrubbed_env(
        GH_PROJECT_ORG_NAME="ExampleOrg",
        GH_PROJECT_REPO_NAME="ExampleRepo",
        GH_PROJECT_PROJECT_NUMBER="1",
        MCP_ACCESS_LEVEL="root",
    )
    result = _run([str(ROOT / "server.py")], env)
    assert result.returncode == CONFIG_ERROR_EXIT_CODE
    assert "Traceback" not in result.stderr
    assert "Configuration error:" in result.stderr


def test_exit_code_is_distinct_from_auth_failure() -> None:
    # 0 = normal shutdown, 1 = auth/runtime failure, 2 = configuration.
    assert CONFIG_ERROR_EXIT_CODE not in (0, 1)


def test_summary_is_single_line(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("GH_PROJECT_ORG_NAME", "GH_PROJECT_REPO_NAME", "GH_PROJECT_PROJECT_NUMBER"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(Exception) as info:
        GitHubProjectSettings(_env_file=None)
    summary = config_error_summary(info.value)
    assert "\n" not in summary
    assert not summary.startswith("Value error")
    assert "Missing required configuration" in summary
