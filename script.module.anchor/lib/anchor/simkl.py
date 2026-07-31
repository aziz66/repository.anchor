"""Minimal Simkl client: PIN auth + scrobble (start/pause/stop) + resume points.

Simkl is a scrobbling service in the same mould as Trakt, and this add-on treats
the two as co-equal: the user may connect either or both, and every playback
event goes to all connected services (see :mod:`anchor.sync`).

The public surface deliberately mirrors :mod:`anchor.trakt` so the two are
interchangeable. Three things are absent because they do not apply:
``keepalive`` / ``reauth_needed``-driven refresh / ``_refresh`` - Simkl access
tokens do not expire (the PIN exchange returns no ``refresh_token`` and no
``expires_in``), and Simkl's API rules forbid unconditional background timers.

Simkl's API rules are strict and enforced by suspension "without warning, no
appeal", so a few constraints are load-bearing rather than stylistic:

* 10 GET/sec and 1 POST/sec, counted per client_id AND per user token.
* Requests must be sequential; parallelism is only sanctioned on their
  Cloudflare-cached catalog endpoints, which we never touch.
* Every request carries ``client_id``/``app-name``/``app-version`` plus a
  descriptive User-Agent.
* No polling of ``/scrobble/*``, and no background polling timers at all.
  Everything here runs in response to a real playback event.
* Re-reads of ``/sync/playback`` are gated on ``/sync/activities``.
"""

from __future__ import annotations

import json
import os
import threading
import time
from urllib.parse import quote, urlencode

from . import _gate, _net, client, ids, store

try:
    import xbmc
    def log(msg, level=1):
        xbmc.log("[anchor.simkl] " + msg, level)
except ImportError:  # outside Kodi (tests)
    def log(msg, level=1):
        print("[simkl] " + msg)

API = "https://api.simkl.com"
TIMEOUT = 15

# Simkl requires an identifying UA and app-name/app-version on every call.
# app-name identifies this add-on in Simkl's dashboard/analytics.
APP_NAME = "Anchor"

# Reported as app-version so analytics can break out releases instead of pooling
# every version together. Read from the RUNNING add-on rather than this shared
# library, since that is the thing users install.
_FALLBACK_VERSION = "0"     # outside Kodi (tests/diagnostics)
_app_version_memo = None


def _app_version():
    global _app_version_memo
    if _app_version_memo is None:
        try:
            import xbmcaddon
            _app_version_memo = (xbmcaddon.Addon().getAddonInfo("version")
                                 or _FALLBACK_VERSION)
        except Exception:  # noqa: BLE001 - not running inside Kodi
            _app_version_memo = _FALLBACK_VERSION
    return _app_version_memo


def _user_agent():
    return "Anchor/%s (Kodi)" % _app_version()

# Simkl app credentials are BRING-YOUR-OWN. The user registers a Simkl API app
# (simkl.com/settings/developer) and enters the client id in the add-on's
# settings; the PIN flow needs no secret. Nothing is embedded here - the add-on
# binds a live getter via set_credentials().
CLIENT_ID = ""


def _client_id():
    return CLIENT_ID


def set_credentials(id_getter):
    """Bind a live client-id getter (the add-on wires this to its settings)."""
    global _client_id
    _client_id = id_getter


def configured():
    """True once the user has entered the Simkl client id."""
    return bool(_client_id())


# ---------------------------------------------------------------------------
# Token storage
# ---------------------------------------------------------------------------

def _token_path():
    return os.path.join(store._store_dir(), "simkl.json")


def _load():
    try:
        with open(_token_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save(tok):
    p = _token_path()
    tmp = p + ".tmp"
    try:
        # 0600 from creation - holds the Simkl access token.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(tok, fh)
        os.replace(tmp, p)
    except OSError:
        pass


def _access_token():
    return _load().get("access_token") or None


def is_authorized():
    return bool(_access_token())


def logout():
    try:
        os.remove(_token_path())
    except OSError:
        pass
    _clear_reauth_needed()
    _invalidate_playback()


def _reauth_flag_path():
    return os.path.join(store._store_dir(), "simkl_reauth")


def _set_reauth_needed():
    try:
        with open(_reauth_flag_path(), "w", encoding="utf-8") as fh:
            fh.write("1")
    except OSError:
        pass


def _clear_reauth_needed():
    try:
        os.remove(_reauth_flag_path())
    except OSError:
        pass


def reauth_needed():
    """True after Simkl rejected our token (401) and the user hasn't
    re-authorized. Unlike Trakt this cannot happen from mere expiry - the token
    has to have been revoked in the user's Simkl account."""
    return os.path.exists(_reauth_flag_path())


# ---------------------------------------------------------------------------
# Request helper
# ---------------------------------------------------------------------------

class SimklUnavailable(Exception):
    """The lookup itself failed (transport/HTTP), as opposed to a successful
    lookup that found no stored progress - so the caller can retry rather than
    concluding 'nothing to resume'."""


def _error_of(data):
    """The machine-readable error code, per /conventions/errors: 'branch on
    `error`, never on `message`'. Returns None when the body is not an error.

    Simkl can return an error envelope inside a 2xx, so this is checked on
    success responses too.
    """
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, str) and err:
            return err
        if isinstance(err, dict):          # nested {"error": {"code": ...}}
            return str(err.get("code") or err.get("message") or "error")
    return None


# Simkl's dashboard flags "3+ requests in the same second from the same IP" as a
# traffic burst that can get a client rate-limited or blocked - a far lower bar
# than the documented 10 GET/sec. Our calls are already sequential, but a
# cold-cache resume lookup is inherently two back-to-back GETs (activities, then
# playback), and a "start over" puts a DELETE right behind them.
#
# We pace to ONE request per second rather than two. Two is legal and would not
# trip the burst warning, but it still shows up as same-second pairs in the
# dashboard, and there is nothing to gain from being that close to a limit whose
# penalty is suspension with no appeal. The cost is ~1s added to a resume lookup
# once per stream, on the service-loop thread - imperceptible, and never on the
# path that would delay playback itself.
_MAX_PER_SECOND = 1
_req_times = []
_pace_lock = threading.Lock()


def _pace():
    with _pace_lock:
        now = time.time()
        del _req_times[:max(0, len(_req_times) - 8)]     # keep it bounded
        recent = [t for t in _req_times if now - t < 1.0]
        if len(recent) >= _MAX_PER_SECOND:
            wait = 1.0 - (now - min(recent)) + 0.02
            if wait > 0:
                time.sleep(wait)
                now = time.time()
        _req_times[:] = [t for t in _req_times if now - t < 1.0]
        _req_times.append(now)


def _req(method, path, body=None, auth=True, params=None, timeout=TIMEOUT):
    """One Simkl call -> ``(status, data)``.

    ``status`` 0 means the request never landed. ``data`` is the decoded body
    (or None). A 2xx carrying an error envelope is downgraded to status 0 with
    the error logged, so callers never mistake it for success.
    """
    qp = {"client_id": _client_id(), "app-name": APP_NAME,
          "app-version": _app_version()}
    qp.update(params or {})
    url = "%s%s?%s" % (API, path, urlencode(qp))
    headers = {"Content-Type": "application/json",
               "simkl-api-key": _client_id()}
    if auth:
        tok = _access_token()
        if not tok:
            return 0, None
        headers["Authorization"] = "Bearer " + tok
    _pace()
    st, raw = _net.http_json(method, url, headers, body,
                             user_agent=_user_agent(), timeout=timeout)
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        data = None

    err = _error_of(data)
    if st == 401:
        # Token revoked in the user's account. Drop it and flag re-auth rather
        # than failing every scrobble forever in silence.
        log("token rejected (401) - clearing", 3)
        logout()
        _set_reauth_needed()
    elif st == 412:
        # client_id missing/wrong/SUSPENDED, or an active throttle block. This
        # is not a per-user problem: our key is dead for everyone, so it must
        # be loud rather than swallowed.
        log("412 from %s - client_id rejected (suspended or throttle-blocked): %s"
            % (path, (raw or "")[:200]), 3)
    elif err and 200 <= st < 300:
        log("%s returned %s with error envelope %r" % (path, st, err), 3)
        return 0, data
    return st, data


# ---------------------------------------------------------------------------
# PIN auth
# ---------------------------------------------------------------------------

def device_code():
    """Start PIN auth -> dict with user_code/verification_url/interval, or None.

    Named to match trakt.device_code so the add-on's auth dialog is shared.
    """
    if not configured():
        return None
    st, data = _req("GET", "/oauth/pin", auth=False)
    if st != 200 or not isinstance(data, dict):
        return None
    if not data.get("user_code"):
        return None
    return data


def poll_token(device, should_cancel=None):
    """Poll /oauth/pin/{user_code} until authorized. Returns True on success."""
    code = device.get("user_code")
    if not code:
        return False
    interval = max(int(device.get("interval", 5) or 5), 1)
    deadline = time.time() + int(device.get("expires_in", 900) or 900)
    while time.time() < deadline:
        if should_cancel and should_cancel():
            return False
        time.sleep(interval)
        st, data = _req("GET", "/oauth/pin/" + quote(str(code)), auth=False)
        if st != 200 or not isinstance(data, dict):
            if st == 429:          # slow down
                interval += 1
            continue
        if data.get("result") == "OK" and data.get("access_token"):
            _save({"access_token": data["access_token"]})
            _clear_reauth_needed()
            _invalidate_playback()
            return True
        # result "KO" = still pending; keep polling until expiry.
    return False


def account_name():
    """The signed-in user's name via POST /users/settings, or None.

    Called ONCE right after authorizing (it is a POST and so counts against the
    1/sec budget) purely to confirm the token works and label the settings
    screen. Never call this on a timer.
    """
    st, data = _req("POST", "/users/settings")
    if st != 200 or not isinstance(data, dict):
        return None
    user = data.get("user") or {}
    return user.get("name") or user.get("nickname") or None


# ---------------------------------------------------------------------------
# Scrobble
# ---------------------------------------------------------------------------

def _payload(ctype, content_id, progress):
    base = ids.base_id(content_id)
    if not base.startswith("tt"):
        return None  # by-imdb only; skip kitsu/tmdb-only ids
    try:
        pct = max(0.0, min(100.0, float(progress)))
    except (TypeError, ValueError):
        pct = 0.0
    payload = {"progress": pct}
    if ctype == "series":
        _b, season, episode = ids.split_series_id(content_id)
        if season is None or episode is None:
            return None
        # Simkl does NOT accept an episode-level imdb id: the show carries the
        # ids and the episode is addressed by season+number.
        payload["show"] = {"ids": {"imdb": base}}
        payload["episode"] = {"season": season, "number": episode}
    else:
        payload["movie"] = {"ids": {"imdb": base}}
    return payload


# Simkl allows one scrobble operation per user at a time behind a 20-SECOND
# duplicate lock ("If a request looks like a duplicate within 20s, expect
# 429-style throttling") - twice Trakt's window, hence the wider dedupe.
#
# pause >= 80% is NOT suppressed here: Simkl stores it ("User pauses at 90% ->
# saved at 90%, not marked watched"). Only Trakt rejects that.
DEDUPE_WINDOW = 20.0
MIN_POST_INTERVAL = 1.2

# Simkl sends no Retry-After header, and its own guidance (1/2/4/8/16s) is
# unusable here: scrobbles run on Kodi's player-callback thread, so a full
# ladder would freeze the player for ~31s. One short retry, then give up and
# log - losing a scrobble costs a resume point, blocking the player costs the
# user their playback.
RETRY_DELAY = 2.0

_gate_ = _gate.ScrobbleGate("simkl", dedupe_window=DEDUPE_WINDOW,
                            min_post_interval=MIN_POST_INTERVAL,
                            suppress_pause_at_watched=False)


def scrobble(action, ctype, content_id, progress):
    """POST scrobble/{start,pause,stop}. Returns the HTTP status (0 = no POST
    made / transport failure). Simkl marks watched when stop >= 80%; below that
    the stop is what stores the resume point."""
    if action not in ("start", "pause", "stop"):
        return 0
    payload = _payload(ctype, content_id, progress)
    if payload is None:
        return 0
    now = time.time()

    # A stop must ALWAYS invalidate the cached playback list, even when the
    # POST below is suppressed - the caller believes the session is closing, so
    # a stale cached position must not outlive it.
    if action == "stop":
        _invalidate_playback()

    allowed, pct = _gate_.check(action, content_id, payload["progress"], now)
    if not allowed:
        return 0
    payload["progress"] = pct

    if not _access_token():
        return 0
    path = "/scrobble/" + action
    st, data = _req("POST", path, payload)
    if st == 429 or 500 <= st < 600:
        log("scrobble/%s -> %s, one retry in %.0fs" % (action, st, RETRY_DELAY), 2)
        time.sleep(RETRY_DELAY)
        st, data = _req("POST", path, payload)
    # 409 = the session was already completed within the last hour. Simkl has
    # the event, so this is a soft success - and it MUST NOT be retried: a
    # re-watch is only ever recorded through an explicit user action, never
    # from an automatic scrobble.
    ok = st in (200, 201, 409)
    if ok:
        log("scrobble/%s %s @%.1f%% -> %s" % (action, content_id, pct, st))
    else:
        log("scrobble/%s %s @%.1f%% -> %s %s"
            % (action, content_id, pct, st,
               json.dumps(data)[:200] if data is not None else ""), 3)
    _gate_.record(action, content_id, pct, now, success=ok)
    return st


# ---------------------------------------------------------------------------
# Resume points
# ---------------------------------------------------------------------------

# Simkl splits a library into THREE top-level types, not two: anime is its own
# type alongside movies and tv_shows, with its own activities key. Fetching the
# per-type playback paths for movies+shows would therefore silently lose every
# resume point for anything Simkl classifies as anime - so we always read the
# untyped /sync/playback, which returns all of them (and does not paginate:
# "up to 10,000 items in one shot").
_ACT_TYPES = ("movies", "tv_shows", "anime")


def _invalidate_playback():
    client.disk_set("simkl_playback", None, 0)
    client.disk_set("simkl_playback_act", None, 0)


def _playback_stamps():
    """The three ``*.playback`` timestamps from /sync/activities, or None.

    This is the sanctioned "is anything new?" gate: one small GET that lets us
    skip the playback fetch entirely when nothing has changed. Comparing the
    nested per-type keys rather than top-level ``all`` means an unrelated change
    (a rating, a list edit) doesn't force a refetch.
    """
    st, data = _req("GET", "/sync/activities")
    if st != 200 or not isinstance(data, dict):
        return None
    return {k: (data.get(k) or {}).get("playback") for k in _ACT_TYPES}


def _rows(data):
    """Flatten a /sync/playback body into rows.

    Handles both plausible shapes - a flat list, or an object keyed by type
    (movies/shows/anime) - because the two are indistinguishable from the docs
    and guessing wrong would silently return "nothing to resume". Also absorbs
    the ``null`` body Simkl uses for an empty result.
    """
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        out = []
        for val in data.values():
            if isinstance(val, list):
                out.extend(r for r in val if isinstance(r, dict))
        return out
    return []


def _imdb_of(row, *keys):
    """IMDb id from a row, whether the ids sit under a wrapper (``movie``,
    ``show``) or directly on the row."""
    for k in keys:
        sub = row.get(k)
        if isinstance(sub, dict):
            got = (sub.get("ids") or {}).get("imdb")
            if got:
                return str(got)
    got = (row.get("ids") or {}).get("imdb") if isinstance(row.get("ids"), dict) else None
    return str(got) if got else None


def _entry(row):
    """One /sync/playback row -> the shape anchor.sync compares and clears."""
    return {"progress": row.get("progress"),
            "id": row.get("id"),
            # Rows are documented with paused_at; fall back to watched_at,
            # which some rows carry instead.
            "paused_at": row.get("paused_at") or row.get("watched_at")}


def playback_progress(ctype, content_id):
    """Simkl's saved playback for this item as
    ``{"progress", "id", "paused_at"}``, or None when there is none.

    Raises :class:`SimklUnavailable` when the lookup itself failed, so the
    caller can retry instead of concluding there is nothing to resume.
    """
    base = ids.base_id(content_id)
    if not base.startswith("tt"):
        return None
    if not _access_token():
        return None

    # Cheap path first: one small activities GET. Only when a playback
    # timestamp has actually moved do we pull the list again.
    stamps = _playback_stamps()
    items = None
    hit, cached = client.disk_get("simkl_playback")
    if hit and cached is not None and stamps is not None:
        _h, cached_stamps = client.disk_get("simkl_playback_act")
        if cached_stamps == stamps:
            items = cached
    if items is None:
        st, data = _req("GET", "/sync/playback")
        if st != 200:
            raise SimklUnavailable("sync/playback -> %s" % st)
        items = _rows(data)
        # Long TTL is safe because the activities check gates freshness; the
        # stamps stored are the ones observed for THIS body.
        client.disk_set("simkl_playback", items, 86400)
        if stamps is not None:
            client.disk_set("simkl_playback_act", stamps, 86400)
    else:
        items = _rows(items)

    if ctype == "series":
        _b, season, episode = ids.split_series_id(content_id)
        for row in items:
            ep = row.get("episode")
            if not isinstance(ep, dict):
                continue
            if _imdb_of(row, "show", "tv_show", "anime") != base:
                continue
            if ep.get("season") == season and ep.get("number") == episode:
                return _entry(row)
    else:
        for row in items:
            if isinstance(row.get("episode"), dict):
                continue      # an episode row can never be this movie
            if _imdb_of(row, "movie") == base:
                return _entry(row)
    return None


def clear_playback(item_id):
    """DELETE /sync/playback/:id - drop a resume point when the user picks
    "start over", so it doesn't re-prompt here or on another device."""
    if not item_id:
        return False
    if not _access_token():
        return False
    st, _d = _req("DELETE", "/sync/playback/" + quote(str(item_id)))
    log("clear playback %s -> %s" % (item_id, st))
    if st in (200, 204, 404):
        _invalidate_playback()
        return True
    return False
