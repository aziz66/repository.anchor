"""The plugin->service identity hand-off and the cast-over path.

The two Anchor processes share state only through Window(10000) properties.
`_fresh_stash` gates them by a 120s freshness window AND a current-file match, so
a stash written for one stream can never mislabel another. The cast-over path
(onAVStarted while an item is already playing) is the ONLY signal Kodi gives that
the outgoing item did not finish - Kodi delivers no onPlayBackStopped for it - so
it must cap a stalled item, or re-casting at 88% marks it watched and destroys
its resume point (the exact historical regression `_stop(replaced=True)` exists
for). This seam had zero coverage before (audit F1).
"""

import time

import pytest

PROP = "anchor."


@pytest.fixture
def player(monkeypatch, home, settings):
    import service
    from anchor import sync
    posts = []
    monkeypatch.setattr(sync, "any_authorized", lambda: True)
    monkeypatch.setattr(
        sync, "scrobble",
        lambda action, ctype, cid, pct: posts.append(
            (action, cid, round(pct, 1))))
    p = service.Scrobbler()
    p._posts = posts
    p._file = ""
    p.getPlayingFile = lambda: p._file
    return p


def _stash(home, cid, ctype, file, ts=None):
    """Write the identity stash the plugin process would leave for the service."""
    home.setProperty(PROP + "playing_id", cid)
    home.setProperty(PROP + "playing_type", ctype)
    home.setProperty(PROP + "playing_file", file)
    home.setProperty(PROP + "playing_ts",
                     str(time.time() if ts is None else ts))


# -- _fresh_stash: freshness + file binding ---------------------------------

def test_fresh_stash_rejects_a_stale_stash(player, home):
    _stash(home, "tt1", "movie", "http://a.mkv", ts=time.time() - 200)
    player._file = "http://a.mkv"
    assert player._fresh_stash() == (None, None)


def test_fresh_stash_rejects_a_file_mismatch(player, home):
    # Stash written for stream A must never identify a different stream B.
    _stash(home, "tt1", "movie", "http://a.mkv")
    player._file = "http://b.mkv"
    assert player._fresh_stash() == (None, None)


def test_fresh_stash_accepts_a_fresh_matching_stash(player, home):
    _stash(home, "tt1", "movie", "http://a.mkv")
    player._file = "http://a.mkv"
    assert player._fresh_stash() == ("movie", "tt1")


def test_fresh_stash_ignores_a_url_query_suffix(player, home):
    # Kodi may append |User-Agent=... to the playing file; the binding compares
    # only the part before the first '|'.
    _stash(home, "tt1", "movie", "http://a.mkv")
    player._file = "http://a.mkv|User-Agent=Kodi"
    assert player._fresh_stash() == ("movie", "tt1")


# -- cast-over: the stalled re-cast regression ------------------------------

def test_castover_caps_a_stalled_outgoing_item(player, home):
    # Episode A stalls at 88%. Re-casting B must post stop for A CAPPED to 79.9%
    # (not 88% -> Trakt/Simkl mark it watched and drop its resume point).
    player._file = "http://a.mkv"
    player._begin("movie", "tt_a")
    player.getTotalTime = lambda: 100.0
    player.getTime = lambda: 88.0
    player.isPlayingVideo = lambda: True
    player._snap()
    assert 87 < player.progress < 89

    _stash(home, "tt_b", "movie", "http://b.mkv")   # a new cast arrives
    player._file = "http://b.mkv"
    player.onAVStarted()

    stops = [x for x in player._posts if x[0] == "stop" and x[1] == "tt_a"]
    assert stops, "the outgoing (un-finished) item must be reported"
    assert stops[-1][2] == 79.9, "stalled re-cast is capped, not marked watched"
    assert player.cid == "tt_b", "the new cast is now the active item"


def test_castover_at_the_very_end_is_not_capped(player, home):
    # A genuine near-end advance (95%) is a real finish, not a stall: report it.
    player._file = "http://a.mkv"
    player._begin("movie", "tt_a")
    player.getTotalTime = lambda: 100.0
    player.getTime = lambda: 95.0
    player.isPlayingVideo = lambda: True
    player._snap()

    _stash(home, "tt_b", "movie", "http://b.mkv")
    player._file = "http://b.mkv"
    player.onAVStarted()

    stops = [x for x in player._posts if x[0] == "stop" and x[1] == "tt_a"]
    assert stops and stops[-1][2] >= 90.0, "a near-end advance keeps its real %"


# -- deferred start ---------------------------------------------------------

def test_start_is_deferred_not_sent_eagerly_in_begin(player):
    # start DELETES stored progress on both services, so it must never fire in
    # the callback that begins playback (before the resume lookup). _begin only
    # arms _pending_start; the service loop sends it after the resume offer is
    # resolved.
    player._file = "http://a.mkv"
    player._begin("movie", "tt_a")
    assert player._pending_start is True
    assert player._resume_offer is True
    assert [x for x in player._posts if x[0] == "start"] == [], \
        "start must not be posted from _begin"
