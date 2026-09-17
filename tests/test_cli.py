"""Tests for the command line interface.

The CLI is how anyone will actually meet this project, and its exit codes are part
of its contract: CI uses them to assert that the bug is still findable and that
the fix still holds. A `search` that silently exits 0 on a violation would make
the CI job in `.github/workflows/ci.yml` a no-op.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deterministic_testing.cli import build_parser, main


def run_cli(argv: list[str], capsys) -> tuple[int, str]:
    code = main(argv)
    return code, capsys.readouterr().out


def test_verify_reports_determinism_and_exits_zero(capsys) -> None:
    code, out = run_cli(["verify", "--runs", "25", "--seed", "1"], capsys)
    assert code == 0
    assert "1 distinct trace digest" in out
    assert "determinism holds" in out


def test_search_finds_the_bug_and_exits_nonzero(capsys) -> None:
    """Exit code 1 on a finding is what makes the CI assertion meaningful."""
    code, out = run_cli(["search", "--seeds", "50", "--variant", "buggy"], capsys)
    assert code == 1
    assert "first failing seed: 1" in out
    assert "each item credited at most once" in out


def test_search_over_the_fixed_variant_exits_zero(capsys) -> None:
    code, out = run_cli(["search", "--seeds", "300", "--variant", "fixed", "--all"], capsys)
    assert code == 0
    assert "no violation found" in out


def test_replay_reproduces_the_reported_seed(capsys) -> None:
    code, out = run_cli(["replay", "1", "--variant", "buggy", "--focus"], capsys)
    assert code == 1
    assert "VIOLATION" in out
    assert "trace digest:" in out

    _, again = run_cli(["replay", "1", "--variant", "buggy", "--focus"], capsys)
    assert _digest_from(out) == _digest_from(again)


def _digest_from(output: str) -> str:
    for line in output.splitlines():
        if line.startswith("trace digest:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError("no digest in CLI output")


def test_shrink_writes_a_replayable_scenario(tmp_path: Path, capsys) -> None:
    target = tmp_path / "minimal.json"
    code, out = run_cli(
        ["shrink", "1", "--variant", "buggy", "--focus", "--save", str(target)], capsys
    )
    assert code == 1
    assert "shrank" in out
    assert target.exists()

    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved["seed"] == 1
    assert saved["plan"], "a shrunk scenario must record the faults it needs"

    # The saved file must be enough to reproduce the bug on its own.
    replay_code, replay_out = run_cli(
        ["replay", "1", "--variant", "buggy", "--plan", str(target), "--focus"], capsys
    )
    assert replay_code == 1
    assert "VIOLATION" in replay_out


def test_search_checkpoint_file_is_written(tmp_path: Path, capsys) -> None:
    checkpoint = tmp_path / "seen.jsonl"
    run_cli(
        [
            "search",
            "--seeds",
            "15",
            "--variant",
            "fixed",
            "--all",
            "--checkpoint",
            str(checkpoint),
        ],
        capsys,
    )
    lines = checkpoint.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 15
    assert all("digest" in json.loads(line) for line in lines)


@pytest.mark.parametrize("command", ["search", "replay", "shrink", "verify", "demo"])
def test_every_command_is_registered(command: str) -> None:
    parser = build_parser()
    subparsers = [
        action for action in parser._actions if hasattr(action, "choices") and action.choices
    ]
    assert any(command in (action.choices or {}) for action in subparsers)
