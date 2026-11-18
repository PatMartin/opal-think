"""CLI `tree` command: rooted command tree with per-command usage labels."""

from __future__ import annotations

from typer.testing import CliRunner

from opal_think.cli.main import think_app

runner = CliRunner()


def test_tree_renders_rooted_rich() -> None:
    result = runner.invoke(think_app, ["tree"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].startswith("opal-think")  # root label carries its own option usage labels
    assert any(line.startswith(("├──", "└──", "│")) for line in lines[1:])  # unicode guides, plain when piped
    assert "\x1b[" not in result.output  # CliRunner is not a TTY: no ANSI escapes


def test_tree_ascii_variant() -> None:
    result = runner.invoke(think_app, ["tree", "--ascii"])
    assert result.exit_code == 0, result.output
    assert "──" not in result.output and "\x1b[" not in result.output
    assert result.output.isascii()  # pure-ASCII: safe for logs and non-UTF-8 environments
    assert "|-- " in result.output and "`-- " in result.output


def test_tree_covers_commands_and_arguments() -> None:
    out = runner.invoke(think_app, ["tree"]).output
    for command in (
        "ask", "backup", "chat", "config", "gui", "info", "model", "service", "session", "task", "tree",
    ):
        assert f" {command}" in f"\n{out}"
    assert "prompt" in out  # required argument, unbracketed
    assert "model_id" in out
    assert "[--model]" in out  # optional option bracketed
    assert "[--ascii]" in out  # the --ascii flag appears as an optional parameter
    assert "--help" not in out  # help options omitted
    assert "completion" not in out  # shell-completion plumbing omitted (expose_value=False)


def test_tree_skips_hidden_commands() -> None:
    """Hidden click commands are not rendered in either tree variant."""
    import click
    from rich.console import Console

    from opal_think.cli.main import _ascii_tree, _rich_tree

    group = click.Group("g")
    group.add_command(click.Command("visible"))
    group.add_command(click.Command("secret", hidden=True))

    out = _ascii_tree(group, "g")
    assert "visible" in out and "secret" not in out

    console = Console(record=True, width=80)
    console.print(_rich_tree(group, "g"))
    out = console.export_text()
    assert "visible" in out and "secret" not in out
