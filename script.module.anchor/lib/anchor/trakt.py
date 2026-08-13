"""Minimal Trakt.tv client: device-code auth + scrobble (start/pause/stop).

Records in-progress playback ("currently watching") and watch history on Trakt.
Content ids are IMDB (``tt...``), which Trakt accepts directly, so no TMDB
lookup is needed. Tokens are stored 0600 in the add-on profile. App credentials
are bring-your-own: nothing is embedded here (see set_credentials below); the
using add-on binds live getters from its own settings.
"""

from __future__ import annotations

import json
import os
import threading
import time

from . import _gate, _net, client, ids, store

try:
    import xbmc
    def log(msg, level=1):
        xbmc.log("[anchor.trakt] " + msg, level)
except ImportError:  # outside Kodi (tests)
    def log(msg, level=1):
        print("[trakt] " + msg)

API = "https://api.trakt.tv"
# OAuth MUST target auth.trakt.tv, not api.trakt.tv (Trakt "API URL" guide). Both
# hosts answered /oauth/* historically, but Trakt is migrating the OAuth surface
# onto auth.trakt.tv (the device-code response now returns
# verification_url=https://auth.trakt.tv/activate) and the api.trakt.tv/oauth/*
# alias can be sunset without notice. Only the OAuth calls move; scrobble/sync
# stay on API.
OAUTH = "https://auth.trakt.tv"
TIMEOUT = 15
# Fallback only - Trakt always returns the real expires_in (currently ~7 days),
# which is what we store. This is used solely if that field is ever missing.
DEFAULT_EXPIRY = 604800  # 7 days
# Proactively refresh when the access token is within this of expiry, so the
# token chain stays rotated on an always-on box even without playback.
KEEPALIVE_MARGIN = 172800  # 2 days


# ---------------------------------------------------------------------------
# HTTP (POST/GET with JSON) - requests if available, urllib fallback
# ---------------------------------------------------------------------------
# api.trakt.tv is behind Cloudflare, which 403s the default "Python-urllib/3.x"
# User-Agent. The urllib branch is what runs on boxes WITHOUT
# script.module.requests (e.g. CoreELEC), so every Trakt call there failed -
# auth included. Always send an explicit UA.
USER_AGENT = "Anchor/1.0 (Kodi)"


MAX_RETRY_AFTER = 60


def _retry_after(body, default=2):
    """Seconds to wait after a 429, bounded. We don't get headers back from
    _http, so fall back to a small fixed delay."""
    try:
        v = int(json.loads(body or "{}").get("retry_after", default))
    except Exception:  # noqa: BLE001
        v = default
    return max(1, min(v, MAX_RETRY_AFTER))


def _http(method, url, headers, body=None):
    return _net.http_json(method, url, headers, body,
                          user_agent=USER_AGENT, timeout=TIMEOUT)


# ---------------------------------------------------------------------------
# Credentials + token storage
# ---------------------------------------------------------------------------

# Trakt app credentials are BRING-YOUR-OWN. The user registers a Trakt API
# application (app.trakt.tv/oauth/applications; redirect uri
# urn:ietf:wg:oauth:2.0:oob) and enters the client id + secret in the add-on's
# settings. Nothing is embedded here; the add-on binds live getters via
# set_credentials(), so no credential ever ships in the source or the zip.
CLIENT_ID = ""
CLIENT_SECRET = ""


def _client_id():
    return CLIENT_ID


def _client_secret():
    return CLIENT_SECRET


def set_credentials(id_getter, secret_getter):
    """Bind live credential getters (the add-on wires these to its settings)."""
    global _client_id, _client_secret
    _client_id = id_getter
    _client_secret = secret_getter


def configured():
    """True once the user has entered both the client id and the secret."""
    return bool(_client_id() and _client_secret())


def _token_path():
    return os.path.join(store._store_dir(), "trakt.json")


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
        # 0600 from creation - holds the Trakt access/refresh tokens.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(tok, fh)
        os.replace(tmp, p)
    except OSError:
        pass


def is_authorized():
    return bool(_load().get("access_token"))


def logout():
    try:
        os.remove(_token_path())
    except OSError:
        pass
    _clear_reauth_needed()   # a deliberate sign-out must not nag


# -- re-auth sentinel: set when a refresh is rejected (token dead), so the
#    service can warn once and the UI can prompt the user to re-authorize. -----

def _reauth_flag_path():
    return os.path.join(store._store_dir(), "trakt_reauth")


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
    """True after a refresh was rejected (refresh token revoked/expired) and the
    user hasn't re-authorized yet."""
    return os.path.exists(_reauth_flag_path())


# Trakt ROTATES the refresh token on first use. The service loop (keepalive)
# and the player-callback threads (scrobble -> _auth_headers -> _access_token)
# can both reach _refresh at once; the loser would then POST an already-spent
# grant, get invalid_grant, and trip the "token dead" path - wiping a perfectly
# good login and nagging the user to re-authorize. Serialise, and re-read the
# token inside the lock so the loser picks up the winner's fresh one.
_refresh_lock = threading.Lock()
_REFRESH_COOLDOWN = 300.0   # after repeated transient failures, stop hammering
_REFRESH_MAX_TRIES = 3
_refresh_fails = 0
_refresh_blocked_until = 0.0


def _refresh(tok):
    """Exchange the stored refresh_token for a fresh access+refresh pair.
    Returns the new access token, the old one (transient failure), or None
    (token permanently rejected -> cleared + re-auth flagged)."""
    global _refresh_fails, _refresh_blocked_until
    with _refresh_lock:
        now = time.time()
        if now < _refresh_blocked_until:
            return tok.get("access_token")   # cooling down after failures
        # Another thread may have refreshed while we waited for the lock.
        fresh = _load()
        if fresh.get("access_token") and fresh.get("access_token") != tok.get("access_token"):
            return fresh.get("access_token")
        if fresh.get("expires_at", 0) > now + 60:
            return fresh.get("access_token")
        tok = fresh or tok
        out = _do_refresh(tok)
        if out is None or out == tok.get("access_token"):
            _refresh_fails += 1
            if _refresh_fails >= _REFRESH_MAX_TRIES:
                _refresh_blocked_until = now + _REFRESH_COOLDOWN
                _refresh_fails = 0
                log("Trakt refresh failing; backing off %ds" % _REFRESH_COOLDOWN, 2)
        else:
            _refresh_fails = 0
        return out


def _do_refresh(tok):
    """The actual token exchange. Callers must hold _refresh_lock."""
    rt = tok.get("refresh_token")
    at = tok.get("access_token")
    if not rt:
        return at
    st, body = _http("POST", OAUTH + "/oauth/token",
                     {"Content-Type": "application/json"},
                     {"refresh_token": rt, "client_id": _client_id(),
                      "client_secret": _client_secret(),
                      "grant_type": "refresh_token",
                      "redirect_uri": "urn:ietf:wg:oauth:2.0:oob"})
    if st == 200:
        try:
            new = json.loads(body)
        except ValueError:
            return at
        new["expires_at"] = time.time() + new.get("expires_in", DEFAULT_EXPIRY)
        _save(new)
        _clear_reauth_needed()
        return new.get("access_token")
    if st == 401 or "invalid_grant" in (body or ""):
        # refresh token revoked/expired -> drop it and flag re-auth so the user
        # is told, instead of silently failing every scrobble forever.
        log("Trakt refresh rejected (%s); clearing tokens" % st, 3)
        logout()
        _set_reauth_needed()
        return None
    if st == 503:
        log("Trakt unavailable (503) - keeping token, will retry", 2)
    return at  # transient (network/5xx) - keep token, retry later


def _access_token():
    """Return a valid access token, refreshing if (near) expired; or None."""
    tok = _load()
    at = tok.get("access_token")
    if not at:
        return None
    if tok.get("expires_at", 0) > time.time() + 60:
        return at
    return _refresh(tok)


def keepalive(margin=KEEPALIVE_MARGIN):
    """Proactive refresh so the token chain never lapses from idleness. Safe to
    call frequently (e.g. hourly from the service loop); only hits the network
    when the access token is within `margin` of expiry."""
    tok = _load()
    if not tok.get("access_token"):
        return
    if tok.get("expires_at", 0) < time.time() + margin:
        _refresh(tok)


def _auth_headers():
    h = {"Content-Type": "application/json", "trakt-api-version": "2",
         "trakt-api-key": _client_id()}
    at = _access_token()
    if at:
        h["Authorization"] = "Bearer " + at
    return h


# ---------------------------------------------------------------------------
# Device-code OAuth
# ---------------------------------------------------------------------------

def device_code():
    """Start device auth -> dict with user_code/verification_url/interval, or None."""
    cid = _client_id()
    if not cid:
        return None
    st, body = _http("POST", OAUTH + "/oauth/device/code",
                     {"Content-Type": "application/json"}, {"client_id": cid})
    if st == 200:
        try:
            return json.loads(body)
        except ValueError:
            return None
    return None


def poll_token(device, should_cancel=None):
    """Poll oauth/device/token until authorized. Returns True on success."""
    cid, cs = _client_id(), _client_secret()
    code = device.get("device_code")
    interval = max(int(device.get("interval", 5)), 1)
    deadline = time.time() + int(device.get("expires_in", 600))
    while time.time() < deadline:
        if should_cancel and should_cancel():
            return False
        time.sleep(interval)
        st, body = _http("POST", OAUTH + "/oauth/device/token",
                         {"Content-Type": "application/json"},
                         {"code": code, "client_id": cid, "client_secret": cs})
        if st == 200:
            try:
                tok = json.loads(body)
            except ValueError:
                return False
            tok["expires_at"] = time.time() + tok.get("expires_in", DEFAULT_EXPIRY)
            _save(tok)
            _clear_reauth_needed()
            return True
        if st == 429:        # slow down -> back off
            interval += 1
            continue
        if st == 400:        # pending -> keep polling
            continue
        return False         # 404/409/410/418 -> give up
    return False


# ---------------------------------------------------------------------------
# Scrobble
# ---------------------------------------------------------------------------

def _payload(ctype, content_id, progress):
    base = ids.base_id(content_id)
    if not base.startswith("tt"):
        return None  # Trakt-by-imdb only; skip kitsu/tmdb-only ids
    try:
        pct = max(0.0, min(100.0, float(progress)))
    except (TypeError, ValueError):
        pct = 0.0
    payload = {"progress": pct}
    if ctype == "series":
        _b, season, episode = ids.split_series_id(content_id)
        if season is None or episode is None:
            return None
        payload["show"] = {"ids": {"imdb": base}}
        payload["episode"] = {"season": season, "number": episode}
    else:
        payload["movie"] = {"ids": {"imdb": base}}
    return payload


# Scrobble session state. See anchor._gate for why each rule exists - Trakt's
# `start` DELETES any stored playback progress ("This will remove any playback
# progress if it exists"), so a redundant start wipes the user's resume point,
# and Kodi fires exactly such a redundant start when one cast replaces another.
#
# pause >= 80% is suppressed HERE ONLY: Trakt answers it with an undocumented
# 422 ("Use stop to scrobble"). Simkl accepts and stores it.
WATCHED_PCT = _gate.WATCHED_PCT   # Trakt: stop >=80% = watched; pause >=80% = 422
MIN_POST_INTERVAL = 1.2  # Trakt allows 1 POST/s; a re-cast fires 3 in ~0.6s
DEDUPE_WINDOW = 10.0

_gate_ = _gate.ScrobbleGate("trakt", dedupe_window=DEDUPE_WINDOW,
                            min_post_interval=MIN_POST_INTERVAL,
                            suppress_pause_at_watched=True)


def scrobble(action, ctype, content_id, progress):
    """POST scrobble/{start,pause,stop}. Returns the HTTP status (0 = no POST
    made / transport failure). Trakt marks watched when stop >= 80%; a stop
    between 1% and 79% is what stores the resume point."""
    if action not in ("start", "pause", "stop"):
        return 0
    payload = _payload(ctype, content_id, progress)
    if payload is None:
        return 0
    now = time.time()

    # A stop must ALWAYS invalidate the cached playback list, even when the
    # POST below is suppressed - the caller believes the session is closing,
    # so a stale cached position must not outlive it. Re-fetching is free.
    if action == "stop":
        client.disk_set("trakt_playback", None, 0)
        client.disk_set("trakt_playback_at", None, 0)

    allowed, pct = _gate_.check(action, content_id, payload["progress"], now)
    if not allowed:
        return 0
    payload["progress"] = pct

    headers = _auth_headers()
    if "Authorization" not in headers:
        return 0  # not authorized
    st, body = _http("POST", "%s/scrobble/%s" % (API, action), headers, payload)
    if st == 429:
        # Trakt allows 1 POST/s. Our own floor can't cover bursts we didn't
        # originate, so honour Retry-After once - a dropped `stop` costs the
        # user their resume point.
        delay = _retry_after(body)
        log("scrobble/%s rate-limited, retrying in %ss" % (action, delay), 2)
        time.sleep(delay)
        st, body = _http("POST", "%s/scrobble/%s" % (API, action), headers, payload)
    if st == 401:
        # Token REVOKED in the user's Trakt account (mere expiry is already
        # handled by _access_token/_refresh). Otherwise every scrobble 401s
        # silently forever and the user is never told. Drop it and flag re-auth
        # so the service's notifier nags once - mirrors simkl._req and _refresh.
        log("scrobble/%s -> 401, token revoked; clearing + flagging re-auth"
            % action, 3)
        logout()
        _set_reauth_needed()
    if st in (200, 201, 409):
        log("scrobble/%s %s @%.1f%% -> %s" % (action, content_id, pct, st))
    else:
        # Keep the body on failure - it is the only way to diagnose a 4xx
        # (e.g. an undocumented 422) after the fact.
        log("scrobble/%s %s @%.1f%% -> %s %s"
            % (action, content_id, pct, st, (body or "")[:200]), 3)
    # 409 = "already scrobbled": Trakt has the event, so the session IS open.
    _gate_.record(action, content_id, pct, now, success=st in (200, 201, 409))
    return st


class TraktUnavailable(Exception):
    """Raised when the lookup itself failed (transport/HTTP), as opposed to a
    successful lookup that found no stored progress. Lets the caller retry
    rather than concluding 'nothing to resume'."""


def _playback_changed_since(ts):
    """True if Trakt says playback progress changed since `ts`.

    GET /sync/last_activities is a tiny document listing when each kind of data
    last changed. Comparing its movies/episodes `paused_at` against our last
    fetch lets us cache /sync/playback for much longer without going stale -
    a resume point set on ANOTHER device shows up immediately, while binge
    playback stops refetching the list on every episode. (Pattern taken from
    TMDbHelper.) On any doubt or failure, say "changed" so we refetch.
    """
    headers = _auth_headers()
    if "Authorization" not in headers:
        return True
    st, body = _http("GET", API + "/sync/last_activities", headers)
    if st != 200:
        return True
    try:
        d = json.loads(body)
    except ValueError:
        return True
    stamps = [(d.get(k) or {}).get("paused_at") for k in ("movies", "episodes")]
    return any(p and p > ts for p in stamps if isinstance(p, str))


def clear_playback(item_id):
    """DELETE /sync/playback/:id - the documented way to drop a resume point.
    Used when the user picks "start over", so the stale position doesn't
    re-prompt here or on another device. 204 = removed, 404 = already gone."""
    if not item_id:
        return False
    headers = _auth_headers()
    if "Authorization" not in headers:
        return False
    st, _b = _http("DELETE", "%s/sync/playback/%s" % (API, item_id), headers)
    log("clear playback %s -> %s" % (item_id, st))
    if st in (204, 404):
        client.disk_set("trakt_playback", None, 0)   # list changed
        client.disk_set("trakt_playback_at", None, 0)
        return True
    return False


def playback_progress(ctype, content_id):
    """Trakt's saved playback for this item as
    ``{"progress", "id", "paused_at"}``, or None when there is none.

    ``id`` is what DELETE /sync/playback/:id needs; ``paused_at`` is what
    ``anchor.sync`` compares across services to pick the most recent position.

    Reads /sync/playback so a freshly-opened item can offer 'resume vs restart'.
    The list is disk-cached (and invalidated on scrobble stop) so binge playback
    doesn't re-fetch it on every episode start.
    """
    base = ids.base_id(content_id)
    if not base.startswith("tt"):
        return None
    hit, items = client.disk_get("trakt_playback")
    if hit and items is not None:
        # Cheap freshness check: refetch the (large) playback list only if
        # Trakt says progress actually changed since we cached it. Costs one
        # small GET per playback start and makes a resume point set on ANOTHER
        # device visible immediately. Any uncertainty -> refetch.
        try:
            _h, cached_at = client.disk_get("trakt_playback_at")
            if not cached_at or _playback_changed_since(cached_at):
                hit, items = False, None
        except Exception:  # noqa: BLE001
            hit, items = False, None
    if not hit or items is None:
        headers = _auth_headers()
        if "Authorization" not in headers:
            return None
        st, body = _http("GET", API + "/sync/playback?limit=100", headers)
        if st != 200:
            # Transport/HTTP failure - NOT "there is no resume point". Raising
            # lets the caller retry instead of burning its one-shot prompt.
            raise TraktUnavailable("sync/playback -> %s" % st)
        try:
            items = json.loads(body)
        except ValueError:
            raise TraktUnavailable("sync/playback -> bad JSON") from None
        if isinstance(items, list):
            # Long TTL is safe now that last_activities gates freshness.
            client.disk_set("trakt_playback", items, 3600)
            client.disk_set("trakt_playback_at",
                            time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                          time.gmtime()), 3600)
    if not isinstance(items, list):
        return None
    if ctype == "series":
        _b, season, episode = ids.split_series_id(content_id)
        for it in items:
            if it.get("type") != "episode":
                continue
            sh = (it.get("show") or {}).get("ids") or {}
            ep = it.get("episode") or {}
            if sh.get("imdb") == base and ep.get("season") == season \
                    and ep.get("number") == episode:
                return _entry(it)
    else:
        for it in items:
            if it.get("type") != "movie":
                continue
            if ((it.get("movie") or {}).get("ids") or {}).get("imdb") == base:
                return _entry(it)
    return None


def _entry(it):
    """One /sync/playback row -> the shape anchor.sync compares and clears."""
    return {"progress": it.get("progress"), "id": it.get("id"),
            "paused_at": it.get("paused_at")}

