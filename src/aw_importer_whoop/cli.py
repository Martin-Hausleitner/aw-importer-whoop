from __future__ import annotations

import json
import logging
import os
import secrets
import urllib.parse
import webbrowser
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import click
import httpx

from .config import (
    DEFAULT_INTERVAL_SECONDS,
    REDIRECT_URI,
    SCOPES,
    WHOOP_AUTH_URL,
    WHOOP_TOKEN_URL,
    token_path,
)
from .sync import SyncLoop

LOG_FORMAT = "time=%(asctime)s level=%(levelname)s logger=%(name)s msg=%(message)s"


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format=LOG_FORMAT)


@click.group()
def main() -> None:
    """Import WHOOP v2 data into local ActivityWatch."""


@main.command()
@click.option("--client-id", envvar="WHOOP_CLIENT_ID", required=True, help="WHOOP OAuth client id.")
@click.option("--client-secret", envvar="WHOOP_CLIENT_SECRET", required=True, help="WHOOP OAuth client secret.")
@click.option("--interval", default=DEFAULT_INTERVAL_SECONDS, show_default=True, type=click.IntRange(1), help="Continuous sync interval in seconds.")
@click.option("--once", is_flag=True, help="Run exactly one sync tick, then exit.")
@click.option("--type", "data_types", multiple=True, type=click.Choice(["sleep", "workout", "cycle", "recovery"]), help="Limit sync to one or more data types.")
@click.option("--verbose", is_flag=True, help="Enable debug logging.")
def sync(client_id: str, client_secret: str, interval: int, once: bool, data_types: tuple[str, ...], verbose: bool) -> None:
    """Run the importer. Default mode is a continuous 15-minute loop."""
    configure_logging(verbose)
    loop = SyncLoop(client_id, client_secret, interval=interval, data_types=data_types or None)
    loop.run(once=once)


@main.command()
@click.option("--client-id", envvar="WHOOP_CLIENT_ID", required=True, help="WHOOP OAuth client id.")
@click.option("--client-secret", envvar="WHOOP_CLIENT_SECRET", required=True, help="WHOOP OAuth client secret.")
@click.option("--redirect-uri", default=REDIRECT_URI, show_default=True, help="OAuth redirect URI registered in WHOOP dashboard.")
@click.option("--open-browser/--no-open-browser", default=True, show_default=True, help="Open authorization URL in the default browser.")
@click.option("--verbose", is_flag=True, help="Enable debug logging.")
def login(client_id: str, client_secret: str, redirect_uri: str, open_browser: bool, verbose: bool) -> None:
    """Complete local OAuth and persist refresh/access tokens."""
    configure_logging(verbose)
    state = secrets.token_urlsafe(24)
    redirect = urllib.parse.urlparse(redirect_uri)
    server = HTTPServer((redirect.hostname or "127.0.0.1", redirect.port or 8765), _CallbackHandler)
    server.expected_state = state  # type: ignore[attr-defined]
    server.auth_code = None  # type: ignore[attr-defined]
    server.auth_error = None  # type: ignore[attr-defined]

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
    }
    auth_url = f"{WHOOP_AUTH_URL}?{urllib.parse.urlencode(params)}"
    click.echo("Authorize WHOOP access here:")
    click.echo(auth_url)
    if open_browser:
        webbrowser.open(auth_url)

    click.echo(f"Waiting for OAuth callback on {redirect_uri} ... Press Ctrl+C to cancel.")
    server.handle_request()
    error = getattr(server, "auth_error", None)
    if error:
        raise click.ClickException(str(error))
    code = getattr(server, "auth_code", None)
    if not code:
        raise click.ClickException("OAuth callback did not include an authorization code.")

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    with httpx.Client(timeout=30.0) as client:
        response = client.post(WHOOP_TOKEN_URL, data=data)
        response.raise_for_status()
        tokens: dict[str, Any] = response.json()
    import time

    tokens["expires_at"] = time.time() + int(tokens.get("expires_in", 3600))
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(tokens, indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)
    click.echo(f"Saved WHOOP OAuth tokens to {path}")


@main.command("import-export")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--type", "data_types", multiple=True, type=click.Choice(["sleep", "workout", "cycle", "journal"]), help="Limit import to one or more export data types.")
@click.option("--dry-run", is_flag=True, help="Parse the export without writing ActivityWatch events or state.")
def import_export_command(path: Path, data_types: tuple[str, ...], dry_run: bool) -> None:
    """Backfill local ActivityWatch from a WHOOP data export ZIP."""
    from .export import EXPORT_TYPES, import_export

    stats = import_export(path, data_types or EXPORT_TYPES, dry_run=dry_run)
    click.echo(
        f"parsed={stats.parsed} inserted={stats.inserted} updated={stats.updated} "
        f"skipped={stats.skipped} dry_run={dry_run}"
    )


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib API
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path != "/callback":
            self._reply(404, "Unknown callback path")
            return
        expected_state = getattr(self.server, "expected_state", "")
        if query.get("state", [""])[0] != expected_state:
            self.server.auth_error = "OAuth state mismatch"  # type: ignore[attr-defined]
            self._reply(400, "State mismatch. You can close this tab.")
            return
        if "error" in query:
            self.server.auth_error = query["error"][0]  # type: ignore[attr-defined]
            self._reply(400, "WHOOP authorization failed. You can close this tab.")
            return
        self.server.auth_code = query.get("code", [None])[0]  # type: ignore[attr-defined]
        self._reply(200, "WHOOP authorization complete. You can close this tab.")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib API
        logging.getLogger("aw-importer-whoop.oauth").debug(format, *args)

    def _reply(self, status: int, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


if __name__ == "__main__":
    main()
