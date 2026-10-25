"""service.py - the cross-process view of a rerouted live cast, and the two
log-noise fixes from the box review (2026-10-25).

* A live cast routed through ffmpegdirect plays as ``url|seekable=0&...``; the
  service must still bind it to the bare URL the plugin stashed.
* getTime()/getTotalTime() raise whenever Kodi's IsPlaying() is false, and Kodi
  logs that raise at ERROR even when Python catches it - once per stop. The
  position reads now ask isPlaying() first, which skips only reads that would
  have failed.
* run() releases the module-level Addon/Window objects on exit, or Kodi warns
  that the script "left several classes in memory".
"""

import time

import default
import pytest

PROP = "anchor."
LIVE = "http://provider.example:8080/user/pass/100001.ts"


@pytest.fixture
def player(monkeypatch, home, settings):
    import service
    from anchor import sync
    monkeypatch.setattr(sync, "any_authorized", lambda: True)
    monkeypatch.setattr(sync, "scrobble", lambda *a: None)
    return service.Scrobbler()


def test_rerouted_live_path_still_matches_the_stash(player, home):
    # The plugin stashes the bare URL; Kodi reports the decorated path.
    home.setProperty(PROP + "playing_id", "tt1375666")
    home.setProperty(PROP + "playing_type", "movie")
    home.setProperty(PROP + "playing_file", LIVE)
    home.setProperty(PROP + "playing_ts", str(time.time()))
    player.getPlayingFile = lambda: LIVE + "|" + default._LIVE_OPTS
    assert player._fresh_stash() == ("movie", "tt1375666")
    player._begin("movie", "tt1375666")
    assert player._cid_file == LIVE
    assert not player._moved_on()


def test_position_reads_skip_the_raising_call_when_not_playing(player):
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("Kodi is not playing any media file")

    player.getTime = player.getTotalTime = boom
    player.isPlaying = lambda: False
    player.progress, player.cur_time, player.cur_total = 42.0, 420.0, 1000.0
    player._snap_position()
    assert player._pct() == 42.0
    assert calls == [], "a read Kodi would log at ERROR must not be made"
    assert (player.progress, player.cur_time) == (42.0, 420.0)


def test_position_is_still_read_during_teardown(player):
    # onPlayBackEnded: isPlayingVideo() is already False, but Kodi still answers
    # (IsPlaying() true) - the stop-time reading the docstring protects.
    player.isPlayingVideo = lambda: False
    player.isPlaying = lambda: True
    player.getTotalTime = lambda: 1000.0
    player.getTime = lambda: 900.0
    player._snap_position()
    assert player.progress == 90.0


def test_run_releases_the_kodi_globals_on_exit(monkeypatch, home, settings):
    import service
    # monkeypatch restores both after the test, for the rest of the suite.
    monkeypatch.setattr(service, "ADDON", service.ADDON)
    monkeypatch.setattr(service, "HOME", service.HOME)
    import popup  # as if a styled prompt was shown
    monkeypatch.setattr(popup, "ADDON", popup.ADDON)
    service.run()                     # the stub monitor aborts immediately
    assert not hasattr(service, "ADDON")
    assert not hasattr(service, "HOME")
    assert not hasattr(popup, "ADDON")
