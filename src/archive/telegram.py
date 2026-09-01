"""Telegram session bootstrap (plan §0 / Phase 0).

The `.session` file is a full credential: anyone holding it is logged in as the
archive account. It lives outside git and is written user-only.
"""

from __future__ import annotations

import os
from pathlib import Path

from .config import REPO_ROOT, env, require_env

DEFAULT_SESSION = "secrets/archive.session"


def session_path() -> Path:
    """Where the SQLite session lives, from TELEGRAM_SESSION or the default."""
    path = Path(env("TELEGRAM_SESSION", DEFAULT_SESSION) or DEFAULT_SESSION)
    return path if path.is_absolute() else REPO_ROOT / path


def account(path: Path | None = None) -> dict | None:
    """The logged-in account, or None when the session is not authorized.

    File-exists proves nothing: Telethon completes the MTProto key exchange and
    writes `auth_key` *before* it prompts for a phone, so an abandoned login
    leaves a fully-formed session file with nobody signed in. Only Telegram can
    answer this, so connect and ask.
    """
    from telethon.sync import TelegramClient

    path = session_path() if path is None else path
    if not path.is_file():
        return None
    creds = require_env("TELEGRAM_API_ID", "TELEGRAM_API_HASH")
    client = TelegramClient(
        str(path), int(creds["TELEGRAM_API_ID"]), creds["TELEGRAM_API_HASH"]
    )
    client.connect()
    try:
        if not client.is_user_authorized():
            return None
        me = client.get_me()
    finally:
        client.disconnect()
    return {
        "id": me.id,
        "username": me.username,
        "phone": me.phone,
        "name": " ".join(filter(None, (me.first_name, me.last_name))),
    }


def login() -> dict:
    """Interactive first login. Prompts for phone, code, and 2FA password."""
    from telethon.sync import TelegramClient

    creds = require_env("TELEGRAM_API_ID", "TELEGRAM_API_HASH")
    path = session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)

    # Telethon's start() runs the whole phone/code/2FA flow; nothing to reimplement.
    with TelegramClient(
        str(path), int(creds["TELEGRAM_API_ID"]), creds["TELEGRAM_API_HASH"]
    ) as client:
        me = client.get_me()

    os.chmod(path, 0o600)
    return {
        "id": me.id,
        "username": me.username,
        "phone": me.phone,
        "name": " ".join(filter(None, (me.first_name, me.last_name))),
        "session": str(path),
    }


def clean_unauthorized(path: Path | None = None) -> bool:
    """Delete a session file that no account ever signed into. Returns True if removed."""
    path = session_path() if path is None else path
    if path.is_file() and account(path) is None:
        path.unlink()
        return True
    return False
