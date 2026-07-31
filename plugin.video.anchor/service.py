"""Anchor - background playback companion.

A resident xbmc.Player subclass that, for streams cast in by the companion app
(plugin://plugin.video.anchor/?action=play_url), reports playback to Trakt
and/or Simkl (scrobble start/pause/stop = watch history + resume points), offers
the "Resume from N%" prompt, and skips intro/recap segments via IntroDB.

Identity comes from the stash written at resolve time (default.py:_stash_id).
There is no metadata layer: the cast carries its own title/art/ids, and the
resume position comes from Trakt and/or Simkl (via the anchor.sync fan-out).
"""

from __future__ import annotations

import time

import xbmc
import xbmcaddon
import xbmcgui
from anchor import _gate, client, introdb, simkl, store, sync, trakt

# Point the shared library at THIS add-on's own addon_data (cache + tokens).
client.CACHE_ADDON = "plugin.video.anchor"
store.DATA_ADDON = "plugin.video.anchor"

ADDON = xbmcaddon.Addon()
HOME = xbmcgui.Window(10000)
PROP = "anchor."

# Bring-your-own app credentials, read live from this add-on's settings (see
# default.py). The resident service needs them too: the client id rides in every
# Trakt API header, and the secret drives token refresh.
trakt.set_credentials(lambda: ADDON.getSetting("trakt_client_id").strip(),
                      lambda: ADDON.getSetting("trakt_client_secret").strip())
simkl.set_credentials(lambda: ADDON.getSetting("simkl_client_id").strip())

# Trakt and Simkl both mark an item watched (and drop its resume point) at
# >=80%. On an abnormal stop we report just under that so a died/interrupted
# stream can never be recorded as finished. See Scrobbler._stop. The threshold
# is the shared gate's, so the two definitions can never drift apart.
WATCHED_THRESHOLD_PCT = _gate.WATCHED_PCT
WATCHED_SAFE_PCT = 79.9
# A cast-over (the companion app advancing to the next item) at/above this counts
# as a genuine finish, not a stalled stream: the user watched to the end and
# skipped the credits, so report the real position and let it mark watched.
# Below it, an abnormal stop is still capped to WATCHED_SAFE_PCT to protect the
# resume point of a stream that actually died. 90% matches _maybe_offer_resume's
# own ">=90% = basically watched" line, so a capped point in [80,90) is one the
# prompt would still offer, while >=90% (which the prompt ignores anyway) would
# be a useless resume point AND a wrongly-unwatched episode.
CAST_OVER_FINISH_PCT = 90.0

# How many times a failed resume lookup is retried before we stop asking for
# this stream. Bounded so an unreachable service can never turn the 5s service
# loop into a polling hammer (see _maybe_offer_resume).
RESUME_MAX_TRIES = 3


# Which scrobbling services are active is the add-on's decision, not the
# library's - one settings toggle per service.
sync.is_enabled = lambda name: ADDON.getSetting(name + "_scrobble") != "false"


def _resume_enabled():
    return ADDON.getSetting("resume_prompt") != "false"


def _resume_default_start_over():
    """True when the resume prompt's timeout action is 'start from beginning'
    instead of resuming."""
    return ADDON.getSetting("resume_default") == "1"


def _skip_enabled():
    return ADDON.getSetting("skip_segments") != "false"


def _skip_auto():
    return ADDON.getSetting("skip_auto") == "true"


def _ask(title, subtitle, primary, secondary, timeout_ms, back="secondary",
         focus="primary", thumb=""):
    """Playback prompt -> True when the PRIMARY action is chosen (primary is
    always the timeout default; ``focus`` may highlight the secondary so one
    OK press is the override). Routes to the minimal bottom-right overlay or
    the classic skin yesno per the popup_style setting; any popup failure
    falls back to classic so a broken skin file can never kill a prompt."""
    if ADDON.getSetting("popup_style") != "1":
        try:
            import popup
            return popup.ask(title, subtitle, primary, secondary,
                             timeout_ms, back, focus, thumb)
        except Exception as exc:  # noqa: BLE001
            xbmc.log("[anchor] popup failed (%r) - classic fallback" % exc,
                     xbmc.LOGWARNING)
    # Classic: autoclose always returns False = the No button, so the timeout
    # default (primary) lives on No; defaultbutton only moves the FOCUS.
    # NOTE: Kodi's yesno returns False for BOTH autoclose and Back/ESC, so this
    # path cannot honour `back` - Back behaves as the timeout default. Only the
    # minimal popup implements `back` properly.
    line = title + ("\n" + subtitle if subtitle else "")
    return not xbmcgui.Dialog().yesno(
        "Anchor", line, yeslabel=secondary, nolabel=primary,
        autoclose=timeout_ms,
        defaultbutton=(getattr(xbmcgui, "DLG_YESNO_NO_BTN", 10)
                       if focus == "primary"
                       else getattr(xbmcgui, "DLG_YESNO_YES_BTN", 11)))


class Scrobbler(xbmc.Player):
    def __init__(self):
        super().__init__()
        self.cid = None              # playing item's id (tt.. / tt..:S:E)
        self.ctype = None
        self.scrobble_on = False     # any connected service wants this item
        self.progress = 0.0          # percent
        self.cur_time = 0.0          # seconds
        self.cur_total = 0.0
        self._abnormal_stop = False  # last stop looked like a died stream
        self._eof_midstream = False  # "ended" fired far from the end
        self._resume_pending = None  # (cid, pct) awaiting a settled player
        self._resume_offer = False   # show the per-stream resume prompt once
        self._resume_tries = 0       # bounded retries on lookup failure
        self._resume_cached = None   # (cid, pct, entries) memo while player settles
        self._resumed_to = None      # seconds we resumed to (mutes passed skips)
        self.segments = None         # introdb {intro/recap/outro: {start,end}}
        self._seg_fetch = False      # segments still to be fetched (run loop)
        self._seg_done = set()       # segment kinds already skipped/offered
        self._pending_start = False  # start POST deferred to the loop

    # -- identity -----------------------------------------------------------

    def _fresh_stash(self):
        ts = HOME.getProperty(PROP + "playing_ts")
        try:
            if not ts or time.time() - float(ts) > 120:
                return None, None
        except ValueError:
            return None, None
        # File binding: a stash written for another file must never identify
        # this one (casting B while A plays leaves A's stash live until
        # onAVStarted - seconds in which it would mislabel B).
        pf = HOME.getProperty(PROP + "playing_file")
        if pf:
            try:
                cur = (self.getPlayingFile() or "").split("|")[0]
            except Exception:  # noqa: BLE001
                cur = ""
            # If we can't read the current file we cannot confirm the stash
            # belongs to THIS playback. A lingering stash (e.g. a cast whose
            # resolution failed, so no onPlayBackStopped cleared it) inside the
            # 120s window would otherwise be adopted by an unrelated stream and
            # scrobbled under the wrong id. When bound to a file, require a match.
            if pf.split("|")[0] != cur:
                return None, None
        return (HOME.getProperty(PROP + "playing_type"),
                HOME.getProperty(PROP + "playing_id"))

    @staticmethod
    def _clear_stash():
        """Drop the stash so a previous item's id can never be reused."""
        for p in ("playing_id", "playing_type", "playing_filename",
                  "playing_videosize", "playing_file", "playing_ts"):
            HOME.clearProperty(PROP + p)

    # -- position -----------------------------------------------------------

    def _pct(self):
        try:
            total = self.getTotalTime()
            return 100.0 * self.getTime() / total if total > 0 else self.progress
        except Exception:  # noqa: BLE001 - getTime invalid when not playing
            return self.progress

    def _snap(self):
        """Refresh progress/time/total while the player is still valid."""
        if self.isPlayingVideo():
            self._snap_position()

    def _snap_position(self):
        """Read position/duration WITHOUT the isPlayingVideo guard.

        Used from onPlayBackEnded, where the player is already tearing down so
        isPlayingVideo() is False and _snap would be a no-op - leaving the
        stale (often zero) values the died-stream guard has to judge on.
        Keeps the previous values if the player can no longer answer.
        """
        try:
            total = self.getTotalTime()
            cur = self.getTime()
        except Exception:  # noqa: BLE001 - invalid once fully stopped
            return
        if total and total > 0:
            self.cur_total = total
            self.cur_time = cur
            self.progress = 100.0 * cur / total

    def _is_abnormal_stop(self):
        """True when the stop was really a stream failure, not a genuine
        finish: the player hit eof far before the end (mid-stream network
        death, flagged by onPlayBackEnded)."""
        return self._eof_midstream

    # -- resume prompt -------------------------------------------------------

    def _resume_progress(self):
        """Saved progress percent for the current item, across all connected
        scrobbling services.

        Returns (pct, entries, ok): ``ok`` is False when the lookup failed and
        produced nothing, so the caller can retry instead of burning the
        one-shot prompt on a transient blip. ``entries`` lists every service
        holding a position, so "start over" can clear all of them. When both
        Trakt and Simkl have one, the most recently paused wins.
        """
        if not sync.any_authorized():
            return None, [], True     # definitively nothing to resume from
        try:
            return sync.resume(self.ctype, self.cid)
        except Exception as exc:  # noqa: BLE001
            xbmc.log("[anchor] resume lookup failed: %r" % exc,
                     xbmc.LOGWARNING)
            return None, [], False

    def _maybe_offer_resume(self):
        """Shown once per stream while it plays: 'Resume from N%?' with a 10s
        auto-close that defaults to RESUME."""
        cid_at = self.cid
        # Reuse a prior successful lookup while we wait for the player to settle
        # (below), so re-arming the offer each tick doesn't re-hit the network
        # for the whole buffering window - this path is NOT bounded by
        # RESUME_MAX_TRIES (that only counts failures), so an uncached re-query
        # could poll Trakt/Simkl every tick until a 4K remux finishes buffering.
        if self._resume_cached and self._resume_cached[0] == cid_at:
            _c, pct, entries = self._resume_cached
            ok = True
        else:
            pct, entries, ok = self._resume_progress()
        if not ok:
            # Transient failure - keep the offer alive for a retry, but BOUND
            # it. The service loop ticks every 5s, so an unreachable service
            # during a 2-hour film would otherwise mean ~1400 retries: exactly
            # the "aggressive polling" pattern Simkl warns gets a client
            # blocked. A resume point is worth a few attempts, not a whole film.
            self._resume_tries += 1
            if self._resume_tries >= RESUME_MAX_TRIES:
                self._resume_offer = False
                xbmc.log("[anchor] resume lookup failed %d times - giving up "
                         "for this stream" % self._resume_tries, xbmc.LOGWARNING)
            return
        self._resume_offer = False   # only burn the one-shot on a real answer
        if not pct or pct < 1.0 or pct >= 90.0:
            return  # nothing meaningful to resume / already basically watched
        if pct <= self._pct() + 1.5:
            return  # saved position is where we already are - nothing to gain
        # The lookup can block for seconds; playback may have died meanwhile.
        if self.cid != cid_at or not self.isPlayingVideo():
            return  # don't float a prompt over the UI after playback ended
        # Meaningful position confirmed - memo it so the settle-wait re-arm below
        # reuses it instead of re-querying the network each tick.
        self._resume_cached = (cid_at, pct, entries)
        # Only prompt once the player has actually settled. A 4K remux is still
        # buffering for the first seconds, which is exactly when a reflexive
        # OK on the remote would hit the destructive "Start over" button.
        try:
            if self.getTotalTime() <= 0 or self.getTime() < 1.0:
                self._resume_offer = True   # not ready - try again next tick
                return
        except Exception:  # noqa: BLE001
            self._resume_offer = True
            return
        xbmc.log("[anchor] resume prompt %s @%.0f%%" % (cid_at, pct),
                 xbmc.LOGINFO)
        # The configured timeout default rides the primary button; the FOCUS
        # sits on the other one. Back always means "no seek".
        if _resume_default_start_over():
            resumed = not _ask("Resume from %d%%" % round(pct),
                               "Continue watching", "Start over", "Resume",
                               10000, back="primary", focus="secondary")
        else:
            resumed = _ask("Resume from %d%%" % round(pct),
                           "Continue watching", "Resume", "Start over",
                           10000, focus="secondary")
        if self.cid != cid_at:
            return  # playback changed/stopped while the prompt was up
        if not resumed:
            xbmc.log("[anchor] resume: start-over for %s" % cid_at,
                     xbmc.LOGINFO)
            # Drop the stored position on EVERY service that had one, or it
            # survives and re-prompts on the next play (and on any other
            # device). DELETE /sync/playback/:id is the documented way, and
            # both services implement it.
            try:
                sync.clear_playback(entries)
            except Exception as exc:  # noqa: BLE001 - never break playback for this
                xbmc.log("[anchor] clear playback failed: %r" % exc,
                         xbmc.LOGWARNING)
            return
        self._resume_pending = (cid_at, float(pct))

    def _do_resume_seek(self):
        """Seek only once playback has settled (video rolling, duration known,
        >1s in) - never inside a player callback mid-surface-init."""
        if not self._resume_pending:
            return
        _rid, pct = self._resume_pending
        if _rid != self.cid:
            # Queued for a different item (playback switched while the seek
            # was waiting for the player to settle) - never seek THIS stream
            # to another title's position.
            self._resume_pending = None
            return
        try:
            if not self.isPlayingVideo():
                return
            total = self.getTotalTime()
            if not total or total <= 0 or self.getTime() < 1.0:
                return
            target = total * pct / 100.0
            self.seekTime(target)
            # Resuming jumps past earlier segments - never offer to "skip" an
            # intro/recap the seek already cleared.
            self._resumed_to = target
            self._mute_passed_segments()
            xbmc.log("[anchor] resumed %s to %.0f%% (%ds of %ds)"
                     % (_rid, pct, target, total), xbmc.LOGINFO)
        except Exception:  # noqa: BLE001
            pass
        self._resume_pending = None

    def _mute_passed_segments(self):
        """Mark intro/recap segments that end before the resumed position as
        already handled (called after the resume seek AND after a late segment
        fetch, whichever happens second)."""
        if self._resumed_to is None or not self.segments:
            return
        for kind in ("intro", "recap"):
            seg = self.segments.get(kind)
            if seg and seg["end"] <= self._resumed_to + 5:
                self._seg_done.add(kind)

    # -- IntroDB skip --------------------------------------------------------

    def _load_segments(self):
        """Fetch the episode's intro/recap timestamps (cached on disk)."""
        self._seg_fetch = False
        segs = introdb.segments(self.cid or "")
        self.segments = segs or {}
        self._mute_passed_segments()  # resume seek may have run before fetch

    def _seg_near(self):
        """True while a segment boundary is imminent -> tighten loop to 1s."""
        if not self.segments or not self.cid:
            return False
        pos = self.cur_time
        for kind in ("intro", "recap"):
            seg = self.segments.get(kind)
            if seg and kind not in self._seg_done \
                    and seg["start"] - 12 <= pos < seg["end"]:
                return True
        return False

    def _skip_segment(self, kind, seg):
        """Skip prompt (or auto-skip): seek past the segment's end."""
        if _skip_auto():
            try:
                self.seekTime(seg["end"])
                xbmcgui.Dialog().notification("Anchor",
                                              "Skipped %s" % kind, time=2500)
                xbmc.log("[anchor] auto-skipped %s (%ds-%ds)"
                         % (kind, seg["start"], seg["end"]), xbmc.LOGINFO)
            except Exception:  # noqa: BLE001
                pass
            return
        cid_at = self.cid
        # Timeout default = Skip (primary); highlight sits on "Keep watching"
        # so one OK press cancels the skip, or wait it out to skip.
        skip = _ask("Skip %s" % kind, "%d:%02d - %d:%02d"
                    % (seg["start"] // 60, seg["start"] % 60,
                       seg["end"] // 60, seg["end"] % 60),
                    "Skip", "Keep watching", 8000, focus="secondary")
        if not skip or self.cid != cid_at or not self.isPlayingVideo():
            return
        try:
            if self.getTime() < seg["end"]:  # still inside - jump past it
                self.seekTime(seg["end"])
                xbmc.log("[anchor] skipped %s -> %ds" % (kind, seg["end"]),
                         xbmc.LOGINFO)
        except Exception:  # noqa: BLE001
            pass

    def _watch_segments(self):
        """Per-tick: trigger skip prompts."""
        if not self.cid or not self.segments:
            return
        if self._resume_offer or self._resume_pending:
            return  # resume flow unresolved - the seek may jump past segments
        pos = self.cur_time
        for kind in ("recap", "intro"):
            seg = self.segments.get(kind)
            if not seg or kind in self._seg_done:
                continue
            if seg["start"] <= pos < seg["end"] - 2:
                self._seg_done.add(kind)
                if _skip_enabled():
                    self._skip_segment(kind, seg)

    # -- begin / end ---------------------------------------------------------

    def _begin(self, ctype, cid):
        self.ctype = ctype or ("series" if cid.count(":") >= 2 else "movie")
        self.cid = cid
        self._abnormal_stop = False
        self._eof_midstream = False
        # Reset BEFORE _pct(): it falls back to self.progress when the player
        # has no duration yet (normal for a network stream at onAVStarted), so
        # a leftover 100.0 from the previous item would seed this one at 100%
        # and mark it watched the moment it stops.
        self.progress = 0.0
        self.cur_time = self.cur_total = 0.0
        self.progress = self._pct()
        self._resume_pending = None
        self._resumed_to = None
        self._resume_offer = _resume_enabled()  # per-stream prompt (toggle)
        self._resume_tries = 0
        self._resume_cached = None
        self.scrobble_on = cid.startswith("tt") and sync.any_authorized()
        if self.ctype == "series" and cid.count(":") < 2:
            # Episode unresolved (bare show id): never write show-level junk
            # into watch history - marking a bare series id watched would drop
            # the whole show from the service's progress as "finished".
            self.scrobble_on = False
        is_episode = self.ctype == "series" and cid.startswith("tt") \
            and cid.count(":") >= 2
        self.segments = None
        self._seg_done = set()
        self._seg_fetch = is_episode and _skip_enabled()
        # scrobble/start DELETES any stored playback progress - on BOTH
        # services (Simkl: "Replaces the existing session and clears prior
        # pauses"). The resume prompt reads that same progress, so firing start
        # here (in the player callback) would wipe the position before the
        # prompt can read it. Defer the start to the service loop, which sends
        # it AFTER the resume lookup has run. Also keeps a 15s HTTP call off
        # the callback thread.
        self._pending_start = self.scrobble_on

    def _stop(self, snap=True, replaced=False):
        # snap=False when called from onAVStarted: the NEW video is already
        # playing there, so snapping would overwrite the PREVIOUS item's final
        # progress with the new video's ~0% (the run loop keeps it 5s-fresh).
        if snap:
            self._snap()
        # `replaced` = another cast took over while this item was still playing.
        # Kodi delivers onAVStarted for the new item with NO onPlayBackStopped
        # for the old one (verified live), so this is the ONLY signal that the
        # outgoing item did not finish - its position is merely wherever the
        # run loop last sampled. Without this, stalling at 88% and re-casting
        # posted stop@88 -> Trakt marked it watched and deleted the resume
        # point. A deliberate user stop is NOT flagged here: stopping at 95%
        # should still count as watched.
        if replaced:
            self._eof_midstream = True
        self._abnormal_stop = self._is_abnormal_stop()
        progress = self.progress
        if self._abnormal_stop and WATCHED_THRESHOLD_PCT <= progress < CAST_OVER_FINISH_PCT:
            # Cap ONLY in the [80, 90) band: a real died/stalled stream sits
            # wherever it froze, and capping keeps its resume point instead of
            # marking it watched. At/above CAST_OVER_FINISH_PCT a cast-over is a
            # genuine near-end advance (credits skipped), so the real position
            # is reported and the item is marked watched - otherwise binged
            # episodes were left permanently unwatched at 79.9%.
            progress = WATCHED_SAFE_PCT
            xbmc.log("[anchor] abnormal stop at %ds/%ds (%.0f%%) - reporting "
                     "%.1f%% so the resume point survives instead of being "
                     "marked watched"
                     % (int(self.cur_time), int(self.cur_total),
                        self.progress, progress), xbmc.LOGINFO)
        elif self._abnormal_stop:
            xbmc.log("[anchor] abnormal stop at %ds/%ds - reporting real "
                     "%.1f%%" % (int(self.cur_time), int(self.cur_total),
                                 progress), xbmc.LOGINFO)
        if self.scrobble_on:
            sync.scrobble("stop", self.ctype, self.cid, progress)
        self.scrobble_on = False
        self._pending_start = False  # never open a session for a stopped item
        self.cid = None
        self.progress = 0.0          # never seed the next item's _pct fallback
        self._eof_midstream = False  # consumed - never leak into the next stop

    # -- player callbacks ----------------------------------------------------

    def onAVStarted(self):
        if self.cid:
            # replaced=True: no onPlayBackStopped arrives on a cast-over
            self._stop(snap=False, replaced=True)
        ctype, cid = self._fresh_stash()
        if not cid:
            # Id-less playback (Live TV). _begin is skipped, so clear the
            # per-item state HERE too - otherwise a pending resume seek or the
            # previous item's segments survive and act on THIS stream.
            self._clear_stash()     # stale/absent stash - never reuse
            self._resume_pending = None
            self._resume_offer = False
            self._resume_tries = 0
            self._resume_cached = None
            self._resumed_to = None
            self.segments = None
            self._seg_done = set()
            self._seg_fetch = False
            self._pending_start = False
            self.progress = 0.0
            self.cur_time = self.cur_total = 0.0
            return
        self._begin(ctype, cid)

    def onPlayBackPaused(self):
        self._snap()
        if self.scrobble_on:
            sync.scrobble("pause", self.ctype, self.cid, self.progress)

    def onPlayBackResumed(self):
        if self.scrobble_on:
            self.progress = self._pct()
            sync.scrobble("start", self.ctype, self.cid, self.progress)

    def onPlayBackStopped(self):
        self._resume_pending = None
        self._resume_offer = False
        # Fresh reading first: _stop's _snap() no-ops here because it is guarded
        # by isPlayingVideo(), which is already False once the player has
        # stopped - so without this the stop is recorded at the last run-loop
        # sample (up to 5s stale). _snap_position() is unguarded and keeps the
        # previous value if the player can no longer answer (same as
        # onPlayBackEnded does).
        self._snap_position()
        self._stop()
        self._clear_stash()

    def onPlayBackEnded(self):
        self._resume_pending = None
        self._resume_offer = False
        # Kodi reports a mid-stream NETWORK death as this same "ended" event
        # (a stalled HTTP source returns eof), so "ended" alone cannot be
        # trusted to mean "watched to the credits".
        # Take a fresh reading first: cur_time/cur_total are only refreshed by
        # the run loop's _snap, which can be up to ~20s stale (5s idle tick,
        # plus a blocking resume prompt and the IntroDB fetch ahead of it). A
        # stream that dies in the opening seconds has never been snapped, so
        # both are still 0 - and "0 < 0" would read as a clean finish.
        self._snap_position()
        if self.cur_total <= 0 or self.cur_time <= 0:
            # No position data at all: we cannot show this played to the end,
            # and a dead link typically dies immediately. Treat as abnormal -
            # far better to under-report than to mark something watched that
            # never played.
            self._eof_midstream = True
            xbmc.log("[anchor] playback ended with no position data - "
                     "treating as a died stream, not a finish", xbmc.LOGINFO)
        elif self.cur_time < self.cur_total * 0.9:
            self._eof_midstream = True
            xbmc.log("[anchor] stream died mid-play at %ds/%ds - keeping "
                     "resume point, not marking watched"
                     % (int(self.cur_time), int(self.cur_total)), xbmc.LOGINFO)
        else:
            self.progress = 100.0
        self._stop()
        self._clear_stash()


def _tick(player, state):
    """One pass of the service loop. Split out so run() can contain the whole
    body in a try/except - an escaping exception used to kill the loop for the
    rest of the Kodi session (silently: no scrobbling, resume or skip until a
    restart). The urllib fallback in anchor._net/introdb only catches URLError/OSError,
    so an http.client.HTTPException (BadStatusLine, IncompleteRead - a captive
    portal or truncated response) escapes; that must not be fatal."""
    # Keep the Trakt token chain alive even during idle stretches, and warn
    # once per session if a sign-in has lapsed (runs before the idle return).
    # Simkl needs no keepalive - its tokens don't expire, and its API rules
    # forbid background timers - so sync.maintenance() skips it.
    if sync.any_authorized():
        now = time.time()
        if now >= state["next_keepalive"]:
            state["next_keepalive"] = now + 3600     # hourly
            sync.maintenance()
    lapsed = sync.reauth_names()
    if lapsed and not state["reauth_notified"]:
        state["reauth_notified"] = True
        xbmcgui.Dialog().notification(
            "Anchor",
            "%s sign-in expired - re-authorize in Anchor"
            % " and ".join(lapsed),
            xbmcgui.NOTIFICATION_WARNING, 8000)
    if player._resume_pending:
        player._do_resume_seek()
    if not player.isPlayingVideo():
        return
    if player._resume_offer and player.cid:
        # The resume position comes from the scrobbling service, so tell the
        # user ONCE if the prompt is enabled but no account is connected -
        # otherwise it just never appears and looks broken.
        if not sync.any_authorized() and not state["noauth_notified"]:
            state["noauth_notified"] = True
            player._resume_offer = False
            xbmcgui.Dialog().notification(
                "Anchor",
                "Resume needs Trakt or Simkl - authorize one in Anchor",
                xbmcgui.NOTIFICATION_INFO, 6000)
        else:
            player._maybe_offer_resume()  # per-stream prompt (10s -> Resume)
    # The deferred start goes out once the resume offer has been resolved
    # (consumed, or disabled) - i.e. after the resume position has been read,
    # so start's wipe can no longer destroy what the prompt needs.
    if player.cid and player._pending_start and not player._resume_offer:
        player._pending_start = False
        sync.scrobble("start", player.ctype, player.cid, player.progress)
    if player._seg_fetch and player.cid:
        player._load_segments()       # introdb intro/recap (cached)
    if player.cid:
        player._snap()  # keep progress fresh for stop/skip timing
        player._watch_segments()


def run():
    monitor = xbmc.Monitor()
    player = Scrobbler()
    state = {"next_keepalive": 0.0,   # one proactive Trakt refresh at startup
             "reauth_notified": False,
             "noauth_notified": False}
    while not monitor.abortRequested():
        # Idle-light: 5s normally; 1s only while a resume offer/seek is pending
        # or a skip boundary is imminent.
        busy = (player._resume_pending or player._resume_offer
                or player._seg_near())
        if monitor.waitForAbort(1 if busy else 5):
            break
        try:
            _tick(player, state)
        except Exception as exc:  # noqa: BLE001 - the loop must outlive any one tick
            xbmc.log("[anchor] service tick failed: %r" % exc,
                     xbmc.LOGWARNING)
    del player


if __name__ == "__main__":
    run()
