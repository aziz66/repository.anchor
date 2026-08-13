"""Playlist auto-advance must not misreport the FINISHED episode.

The companion app pre-queues the next episode and lets Kodi autoplay it, so at
end-of-file Kodi opens the next item BEFORE it delivers onPlayBackEnded for the
one that just finished. getTime()/getTotalTime() then answer for the NEXT
episode - a few seconds into a full runtime - so scoring the finished episode on
that reads it as a died stream and posts stop@~1%, leaving it unwatched and
overwriting its resume point. The player is pinned to the file its id was
resolved for (Scrobbler._cid_file); a position read is refused once the player
has demonstrably moved on (_moved_on). These tests fail before that fix and pass
after it.
"""

import pytest

EP_A = "tt0903747:1:2"
EP_B = "tt0903747:1:3"
FILE_A = "http://debrid.test/ep-a.mkv"
FILE_B = "http://debrid.test/ep-b.mkv"
RUNTIME = 1440.0          # a 24-minute episode, in seconds


class _Player:
    """Drives a real Scrobbler's view of the player the way Kodi would, and
    records every scrobble the fan-out would send."""

    def __init__(self, monkeypatch):
        import service
        from anchor import sync
        self.posts = []
        monkeypatch.setattr(sync, "any_authorized", lambda: True)
        monkeypatch.setattr(
            sync, "scrobble",
            lambda action, ctype, cid, pct: self.posts.append(
                (action, cid, round(pct, 1))))
        self.s = service.Scrobbler()
        self.file, self.time, self.total, self.playing = "", 0.0, 0.0, True
        self.s.getPlayingFile = lambda: self.file
        self.s.getTime = lambda: self.time
        self.s.getTotalTime = lambda: self.total
        self.s.isPlayingVideo = lambda: self.playing

    def open(self, f, total):
        self.file, self.total, self.time, self.playing = f, total, 0.0, True

    def at(self, t):
        self.time = t

    def stops_for(self, cid):
        return [p for p in self.posts if p[0] == "stop" and p[1] == cid]


@pytest.fixture
def player(monkeypatch, home, settings):
    return _Player(monkeypatch)


def test_autoadvance_marks_the_finished_episode_watched(player):
    # Episode A plays; the 5s run loop samples it near the end.
    player.open(FILE_A, RUNTIME)
    player.s._begin("series", EP_A)
    player.at(RUNTIME - 8)          # last sample before EOF: ~99%
    player.s._snap()
    assert player.s.progress > 95

    # Kodi auto-advances: the NEXT episode is already open and two seconds in by
    # the time onPlayBackEnded fires for A.
    player.open(FILE_B, RUNTIME)
    player.at(2.0)
    player.s.onPlayBackEnded()

    stops = player.stops_for(EP_A)
    assert stops, "the finished episode must be scrobbled"
    assert stops[-1][2] >= 90.0, \
        "A is scored on its own last sample, not the next item's ~0%"
    assert player.stops_for(EP_B) == [], "the next episode is not scrobbled here"


def test_a_genuine_mid_stream_death_is_still_caught(player):
    # A plays only briefly, then the stream dies with no next item: the player
    # is torn down, so getPlayingFile() is empty - that is NOT "moved on", and
    # the died-stream guard must still fire.
    player.open(FILE_A, RUNTIME)
    player.s._begin("series", EP_A)
    player.at(120.0)                # ~8%
    player.s._snap()
    player.file, player.playing = "", False
    player.s.onPlayBackEnded()

    stops = player.stops_for(EP_A)
    assert stops, "the died stream is still reported"
    assert stops[-1][2] < 80.0, "kept below watched so its resume point survives"


def test_clean_end_without_advance_still_marks_watched(player):
    # A single item plays to the end with nothing queued after it: the player
    # still answers for A (not moved on), so the normal finish path applies.
    player.open(FILE_A, RUNTIME)
    player.s._begin("movie", "tt1375666")
    player.at(RUNTIME - 3)
    player.s._snap()
    player.at(RUNTIME)
    player.s.onPlayBackEnded()

    stops = player.stops_for("tt1375666")
    assert stops and stops[-1][2] >= 90.0
