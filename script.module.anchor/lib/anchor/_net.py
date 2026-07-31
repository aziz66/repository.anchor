"""Tiny JSON-over-HTTP helper shared by the scrobble backends.

Uses ``requests`` when the box has script.module.requests and falls back to
urllib otherwise (CoreELEC/LibreELEC ship without it).

Two things here are load-bearing:

* **An explicit User-Agent on every request.** api.trakt.tv sits behind
  Cloudflare, which 403s the default "Python-urllib/3.x" - so on a box without
  requests every Trakt call failed, authorization included. Simkl's API rules
  independently require a descriptive UA.
* **Never raise.** These calls run on Kodi's player-callback thread; an
  escaping network error there breaks playback handling. A transport failure is
  reported as status 0.
"""

from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

DEFAULT_TIMEOUT = 15


def _headers(headers, user_agent):
    h = dict(headers or {})
    h.setdefault("User-Agent", user_agent)
    return h


try:
    import requests

    def http_json(method, url, headers, body=None, user_agent="Anchor/1.0 (Kodi)",
                  timeout=DEFAULT_TIMEOUT):
        """Return ``(status, text)``; status 0 means the request never landed."""
        try:
            r = requests.request(method, url, headers=_headers(headers, user_agent),
                                 timeout=timeout,
                                 data=json.dumps(body) if body is not None else None)
            return r.status_code, (r.text or "")
        except Exception:  # noqa: BLE001 - a network blip must not raise inside a
            return 0, ""   # player callback (matches the urllib fallback)
except ImportError:
    def http_json(method, url, headers, body=None, user_agent="Anchor/1.0 (Kodi)",
                  timeout=DEFAULT_TIMEOUT):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = Request(url, data=data, headers=_headers(headers, user_agent),
                      method=method)
        try:
            with urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except HTTPError as exc:
            try:
                return exc.code, exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                return exc.code, ""
        except Exception:  # noqa: BLE001 - NEVER raise: this runs on player-callback
            # threads. URLError/OSError are the common cases, but http.client
            # (IncompleteRead/BadStatusLine, not an OSError) and a non-UTF-8
            # body must not escape either - this is the designated safety
            # boundary (see module docstring).
            return 0, ""
