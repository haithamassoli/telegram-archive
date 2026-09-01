"""Convex from Python, over the deployment's HTTP API.

ponytail: urllib + the documented `/api/mutation` JSON envelope instead of a
Convex client library — there is no first-party Python one, and the envelope is
three fields. Swap in a real client if subscriptions or auth ever matter.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from .config import require_env

TIMEOUT = 60
RETRIES = 4


class ConvexError(RuntimeError):
    """The deployment answered, and said no."""


def _number(text: str) -> float | int:
    """Convex numbers are float64, so an id round-trips as `1.0`.

    Telegram message ids, timestamps and counts are integers on both sides of
    that wire and are used as such (`get_messages(ids=...)`, `{id:08d}` keys), so
    narrow integral values back to int here rather than at every call site.
    Genuinely fractional values — confidences, ratios — pass through untouched.
    """
    value = float(text)
    return int(value) if value.is_integer() else value


def _url(kind: str) -> str:
    base = require_env("CONVEX_URL")["CONVEX_URL"].rstrip("/")
    return f"{base}/api/{kind}"


def _call(kind: str, path: str, args: dict) -> object:
    # Convex optionals mean "absent", not "null" — a None here would fail the
    # validator, so drop them. Fields that must be null are set inside the
    # mutation, never passed from here.
    body = json.dumps(
        {
            "path": path,
            "args": {key: value for key, value in args.items() if value is not None},
            "format": "json",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        _url(kind), data=body, headers={"Content-Type": "application/json"}
    )
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                payload = json.loads(response.read(), parse_float=_number)
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:400].decode("utf-8", "replace")
            if exc.code < 500:
                raise ConvexError(f"{path}: HTTP {exc.code} {detail}") from None
            last = ConvexError(f"{path}: HTTP {exc.code} {detail}")
        except OSError as exc:  # DNS, TLS, timeouts — a multi-hour run sees these
            last = exc
        time.sleep(2**attempt)
    else:
        raise ConvexError(f"{path}: {RETRIES} attempts failed: {last}") from last
    if payload.get("status") != "success":
        raise ConvexError(f"{path}: {payload.get('errorMessage') or payload}")
    return payload["value"]


def mutation(path: str, **args) -> object:
    return _call("mutation", path, args)


def query(path: str, **args) -> object:
    return _call("query", path, args)
