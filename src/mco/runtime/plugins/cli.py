"""Inspect a plugin without running any of its commands."""
from __future__ import annotations

from pathlib import Path

import typer

from .trust import MANIFEST_NAME, inspect_plugin, trust_text


plugin_app = typer.Typer(no_args_is_help=True)


@plugin_app.callback()
def plugin_group() -> None:
    """Inspect declarative runtime plugins."""


@plugin_app.command()
def validate(
    manifest: Path = typer.Argument(..., help="Plugin directory or path to its manifest."),
    lock_entry: bool = typer.Option(False, "--lock-entry", help="Print the candidate trust lock for owner review."),
) -> None:
    """Validate a plugin and resolve its command files without running them."""
    try:
        candidate = inspect_plugin(manifest / MANIFEST_NAME if manifest.is_dir() else manifest)
    except (OSError, ValueError, RuntimeError):
        typer.echo("Invalid plugin manifest or unresolved command file.", err=True)
        raise typer.Exit(1) from None
    if lock_entry:
        typer.echo(trust_text(candidate), nl=False)
    else:
        typer.echo(f"{candidate.manifest.id}: valid ({len(candidate.files)} command file pins)")
