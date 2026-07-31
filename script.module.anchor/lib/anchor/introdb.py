"""IntroDB (introdb.app) client - crowdsourced intro/recap/outro timestamps.

GET https://api.introdb.app/segments?imdb_id=tt..&season=N&episode=N
  -> {intro|recap|outro: {start_sec, end_sec, confidence, submission_count} | null}

Used by the scrobbler service to offer "Skip intro/recap" and to time the
Up Next popup at the outro start. Results are disk-cached (segments don't
change often); 404 ("no data for this episode") is cached too so we don't
re-ask on every playback. Never raises.
"""

from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from . import client, ids

API = "https://api.introdb.app"
TIMEOUT = 8
HIT_TTL = 7 * 86400   # found segments: re-check weekly (community data refines)
MISS_TTL = 86400      # no data yet: re-check daily

try:
    import xbmc
    def log(msg, level=1):
        xbmc.log("[anchor.introdb] " + msg, level)
except ImportError:  # outside Kodi (tests)
    def log(msg, level=1):
        print("[introdb] " + msg)


def _get(url):
    try:
        req = Request(url, headers={"User-Agent": "Kodi-Anchor/1.0",
                                    "Accept": "application/json"})
        with urlopen(req, timeout=TIMEOUT) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")
    except HTTPError as exc:
        return exc.code, {}
    except Exception:  # noqa: BLE001 - never raise into the caller (see segments()):
        # URLError/OSError/ValueError (bad JSON) are the common cases, plus
        # http.client (IncompleteRead/BadStatusLine, not an OSError).
        return 0, {}


def segments(content_id):
    """``{"intro": {"start": s, "end": s}, "recap": ..., "outro": ...}`` for a
    series episode id (``tt...:S:E``). Missing kinds are absent. ``{}`` = no
    data; ``None`` = transient API failure (not cached)."""
    base, season, episode = ids.split_series_id(content_id)
    if not base.startswith("tt") or season is None or episode is None:
        return {}
    key = "introdb::%s:%d:%d" % (base, season, episode)
    hit, val = client.disk_get(key)
    if hit and val is not None:
        return val
    st, body = _get("%s/segments?imdb_id=%s&season=%d&episode=%d"
                    % (API, base, season, episode))
    if st == 200 and isinstance(body, dict):
        out = {}
        for kind in ("intro", "recap", "outro"):
            seg = body.get(kind)
            if isinstance(seg, dict) and seg.get("end_sec") is not None:
                try:
                    start = float(seg.get("start_sec") or 0)
                    end = float(seg["end_sec"])
                except (TypeError, ValueError):
                    continue
                if end > start >= 0:
                    out[kind] = {"start": start, "end": end,
                                 "confidence": seg.get("confidence")}
        client.disk_set(key, out, HIT_TTL if out else MISS_TTL)
        if out:
            log("segments %s -> %s" % (content_id, sorted(out)))
        return out
    if st == 404:
        client.disk_set(key, {}, MISS_TTL)
        return {}
    return None  # transient - retry next playback
