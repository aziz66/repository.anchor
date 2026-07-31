"""Shared scrobble gate for the Trakt and Simkl backends.

Both services take the same start/pause/stop events with a 0-100 percentage,
mark an item watched at a stop >=80%, store a resume point below that, and cap
clients at 1 POST/sec. The rules that keep us from corrupting a user's resume
point are therefore identical, and were paid for the hard way against Trakt:

  * ``start`` DELETES stored playback progress on BOTH services (Simkl:
    "Replaces the existing session and clears prior pauses"), so a redundant
    start silently destroys the user's resume point. The ``open_cid`` latch
    makes a second start structurally impossible.
  * A stop below 1% is refused and IGNORED, which would leave the session open
    forever - so a session-closing stop is floored at 1%.
  * Kodi re-emits player callbacks (a cast replacing another fires three POSTs
    in ~0.6s), which both trips rate limits and posts nonsense.

Keeping one implementation means the two backends cannot silently drift apart.
Where the services genuinely differ, the difference is a constructor argument -
see ``suppress_pause_at_watched``.
"""

from __future__ import annotations

import threading

WATCHED_PCT = 80.0      # both services: stop >= this = watched
DEDUPE_PCT = 1.0        # positions within this are "the same position"


class ScrobbleGate:
    """Decides whether a scrobble POST should actually go out.

    Usage::

        ok, pct = gate.check(action, content_id, pct, now)
        if not ok:
            return 0
        st = do_the_post(pct)
        gate.record(action, content_id, pct, now, success=st in (200, 201, 409))
    """

    def __init__(self, name, dedupe_window=10.0, min_post_interval=1.2,
                 suppress_pause_at_watched=False):
        self.name = name
        self.dedupe_window = dedupe_window
        self.min_post_interval = min_post_interval
        # Trakt REFUSES a pause at/above the watched threshold with an
        # undocumented 422 ("Progress is 88.0%. Use stop to scrobble."), so
        # there the pause must be skipped. Simkl explicitly ACCEPTS it and
        # stores the position ("User pauses at 90% -> saved at 90%, not marked
        # watched"), so suppressing it there would throw away good resume data
        # for the last 20% of every film.
        self.suppress_pause_at_watched = suppress_pause_at_watched
        # open_cid is a one-way latch: set only by a SUCCESSFUL start, cleared
        # only by a stop.
        self.open_cid = None
        self.last = None        # (action, cid, pct, ts) of the last POST sent
        # The deferred start is sent from the service loop while pause/stop
        # arrive on Kodi's player-callback thread, so these really are touched
        # by two threads. The lock covers the read-modify-write of the latch;
        # it is deliberately NOT held across the HTTP call, which would let a
        # slow POST block a player callback for the full 15s timeout.
        self._lock = threading.Lock()

    def reset(self):
        with self._lock:
            self.open_cid = None
            self.last = None

    def check(self, action, content_id, pct, now):
        """Return ``(allowed, pct)``. ``pct`` may be adjusted upward to the
        minimum the service will accept."""
        with self._lock:
            return self._check(action, content_id, pct, now)

    def _check(self, action, content_id, pct, now):
        # 1. Never open a second session for an item that already has one open.
        #    (start wipes stored progress, so a redundant start = lost resume.)
        if action == "start" and self.open_cid == content_id:
            return False, pct
        # 2. Don't record a barely-started item - UNLESS we have a session open
        #    for it, in which case the stop MUST go out to close it. `start` is
        #    never suppressed here: it is what seeds the resume point.
        if action in ("pause", "stop") and pct < 1.0:
            if self.open_cid != content_id:
                return False, pct
            # Sending the true sub-1% value gets the scrobble IGNORED, leaving
            # the session open - the exact thing we are trying to close. Send
            # the minimum that is accepted. Harmless: the resume prompt already
            # ignores anything under 1%.
            pct = 1.0
        # 2b. See suppress_pause_at_watched. Do NOT take Trakt's advice and
        #     send `stop` instead: that marks the item watched and closes the
        #     session while the user is still watching it.
        if action == "pause" and self.suppress_pause_at_watched \
                and pct >= WATCHED_PCT:
            return False, pct
        # 3. Drop an identical re-emission (same action, item and position).
        last = self.last
        if last and last[0] == action and last[1] == content_id \
                and now - last[3] < self.dedupe_window \
                and abs(pct - last[2]) < DEDUPE_PCT:
            return False, pct
        # 4. Rate floor. `stop` is EXEMPT: a dead link does start->die well
        #    inside the interval, and dropping that stop would lose the resume
        #    point entirely.
        if action != "stop" and last and now - last[3] < self.min_post_interval:
            return False, pct
        return True, pct

    def record(self, action, content_id, pct, now, success):
        """Update the latch after a POST actually went out."""
        with self._lock:
            # A failed start (e.g. Simkl's 404 id_err = unmatched item) must
            # NOT latch a session open: nothing is open server-side, and the
            # latch would then suppress the real start that follows.
            if action == "start":
                if success:
                    self.open_cid = content_id
            elif action == "stop":
                self.open_cid = None
            self.last = (action, content_id, pct, now)
