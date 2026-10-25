"""default.play_url - live IPTV casts go through inputstream.ffmpegdirect.

Kodi's own curl path reconnects a dropped live stream as a byte-range resume,
which a live server can't honour, so playback ends about an hour in. These pin
the routing decision: which casts take the ffmpegdirect path, what reaches the
ListItem, and that everything else (and a box without the add-on) keeps
today's path untouched.
"""

import default
import pytest
import xbmc
import xbmcgui

PROP = "anchor."
LIVE = "http://provider.example:8080/user/pass/100001.ts"


@pytest.fixture
def ffmpegdirect(monkeypatch):
    """Report inputstream.ffmpegdirect as enabled (the stub says no add-on)."""
    monkeypatch.setattr(
        xbmc, "getCondVisibility",
        lambda cond: cond == "System.AddonIsEnabled(inputstream.ffmpegdirect)")


def _play(**params):
    xbmcgui.reset()
    default.play_url(params)
    return xbmcgui.ListItem.last


def _routed(li):
    return li.getProperty("inputstream") == "inputstream.ffmpegdirect"


def test_xtream_live_url_is_routed_with_reconnect_options(ffmpegdirect):
    li = _play(url=LIVE, type="movie", title="Test Channel")
    assert _routed(li)
    assert li.getProperty("inputstream.ffmpegdirect.is_realtime_stream") == "true"
    assert li.getProperty("inputstream.ffmpegdirect.open_mode") == "ffmpeg"
    # stream_mode must stay unset: absent = plain live (timeshift/catchup otherwise).
    assert li.getProperty("inputstream.ffmpegdirect.stream_mode") == ""
    base, opts = li.getPath().split("|")
    assert base == LIVE
    opts = dict(o.split("=") for o in opts.split("&"))
    # seekable=0 keeps FFmpeg in streamed mode: reconnect from offset 0, no Range.
    assert opts["seekable"] == "0"
    assert opts["reconnect"] == opts["reconnect_streamed"] == "1"
    assert opts["reconnect_at_eof"] == "1"
    # 0/1/3/7s of backoff: uses, without overrunning, ffmpegdirect's 20s read
    # timeout (a timeout ends playback just like running out of retries).
    assert opts["reconnect_delay_max"] == "8"
    assert li.mimetype == "video/mp2t"


def test_xtream_live_prefix_shape_is_routed(ffmpegdirect):
    assert _routed(_play(url="http://p.example/live/u/p/42.ts"))


def test_existing_headers_are_extended_not_replaced(ffmpegdirect):
    li = _play(url=LIVE + "|User-Agent=VLC%2F3.0")
    assert li.getPath() == LIVE + "|User-Agent=VLC%2F3.0&" + default._LIVE_OPTS


def test_explicit_live_flag_routes_any_url(ffmpegdirect):
    li = _play(url="http://p.example/channel/thmanyah", live="1")
    assert _routed(li)
    assert li.mimetype == ""      # no .ts extension: let FFmpeg probe


@pytest.mark.parametrize("url, extra", [
    ("http://host/The.Release.Name.mkv", {}),                     # debrid VOD
    ("https://x.download.real-debrid.com/d/ABC/Show.S01E02.ts", {}),
    ("http://p.example/movie/u/p/100001.mkv", {}),                # Xtream VOD
    ("http://p.example/movie/u/p/100001.ts", {}),                 # VOD as .ts
    ("http://p.example/series/u/p/100001.ts", {}),                # episode .ts
    ("http://p.example/u/p/100001.m3u8", {}),                     # HLS: Kodi's
    ("http://p.example/u/p/100001.m3u8", {"live": "1"}),          # even flagged
    ("http://p.example/u/p/100001.ts", {"imdb": "tt1375666"}),    # has an id
    # Extensionless Xtream live is NOT detected on purpose (too close to other
    # URL shapes); the app sends live=1 for it. Widening this must be deliberate.
    ("http://p.example/u/p/100001", {}),
])
def test_everything_else_keeps_todays_path(ffmpegdirect, url, extra):
    li = _play(url=url, **extra)
    assert not _routed(li)
    assert li.getPath() == url


def test_missing_addon_falls_back_to_todays_path(home):
    # The stub's getCondVisibility is False: ffmpegdirect missing or disabled.
    xbmc.log_lines.clear()
    li = _play(url=LIVE)
    assert not _routed(li)
    assert li.getPath() == LIVE
    assert any("not enabled" in m for _, m in xbmc.log_lines)


@pytest.fixture
def builtins(monkeypatch):
    calls = []
    monkeypatch.setattr(xbmc, "executebuiltin", lambda cmd, *a: calls.append(cmd))
    return calls


def test_missing_addon_is_offered_once_per_session_after_resolving(home, builtins):
    import xbmcplugin
    xbmcplugin.reset()
    default.play_url({"url": LIVE})
    assert xbmcplugin.resolved[-1][1] is True      # the cast is never held up
    assert builtins == ["InstallAddon(inputstream.ffmpegdirect)"]
    default.play_url({"url": LIVE})
    assert builtins == ["InstallAddon(inputstream.ffmpegdirect)"], "once only"


def test_installed_but_disabled_addon_gets_a_notification_not_an_install(
        home, builtins, monkeypatch):
    monkeypatch.setattr(
        xbmc, "getCondVisibility",
        lambda cond: cond == "System.HasAddon(inputstream.ffmpegdirect)")
    notes = []
    monkeypatch.setattr(default, "notify", lambda msg, *a: notes.append(msg))
    li = _play(url=LIVE)
    assert not _routed(li)                         # HasAddon alone is not enough
    assert builtins == []
    assert notes and "Enable" in notes[0]


def test_no_offer_when_routed_or_not_live(home, builtins, ffmpegdirect):
    _play(url=LIVE)
    _play(url="http://host/The.Release.Name.mkv")
    assert builtins == []
    assert home.getProperty(PROP + "ffmpegdirect_offered") == ""


def test_live_cast_still_resolves_and_clears_the_stash(home, ffmpegdirect):
    import xbmcplugin
    xbmcplugin.reset()
    default.play_url({"url": "http://host/a.mkv", "imdb": "tt1375666"})
    assert home.getProperty(PROP + "playing_id") == "tt1375666"
    default.play_url({"url": LIVE})
    assert home.getProperty(PROP + "playing_id") == ""
    assert xbmcplugin.resolved[-1][1] is True


def test_flagged_live_cast_with_an_id_binds_the_bare_url(home, ffmpegdirect):
    # The service compares getPlayingFile().split("|")[0]; the stash must stay
    # the URL without the reconnect options so the identity still matches.
    li = _play(url=LIVE, imdb="tt1375666", live="1")
    assert _routed(li)
    assert home.getProperty(PROP + "playing_file") == LIVE
    assert li.getPath().split("|")[0] == LIVE
