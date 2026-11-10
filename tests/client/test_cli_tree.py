"""CLI `tree` command: rooted ASCII tree of all commands and arguments."""

from __future__ import annotations

from typer.testing import CliRunner

from opal_think.cli.main import think_app

runner = CliRunner()


def test_tree_renders_rooted_ascii() -> None:
    result = runner.invoke(think_app, ["tree"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "opal-think"
    assert any(line.startswith(("|-- ", "-- ")) for line in lines)  # ascii connectors


def test_tree_covers_commands_and_arguments() -> None:
    out = runner.invoke(think_app, ["tree"]).output
    for command in ("ask", "chat", "gui", "info", "model", "persona", "service", "session", "setting", "task", "tree"):
        assert f" {command}\n" in f"{out}\n" or out.endswith(f" {command}\n")
    assert "<prompt>" in out  # required argument
    assert "<model_id>" in out
    assert "[--model]" in out  # optional option bracketed
    assert "--help" not in out  # help options omitted
