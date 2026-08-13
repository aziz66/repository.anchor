"""default.play_url - the cast-contract entry point. Verifies it parses the
identity params and writes the identity stash the resident service reads."""

import default
import xbmcplugin

PROP = "anchor."


def _play(home, **params):
    xbmcplugin.reset()
    params.setdefault("url", "http://host/The.Release.Name.mkv")
    default.play_url(params)
    return home


def test_movie_id_is_stashed(home):
    _play(home, imdb="tt1375666", type="movie")
    assert home.getProperty(PROP + "playing_id") == "tt1375666"
    assert home.getProperty(PROP + "playing_type") == "movie"
    # playback was resolved successfully
    assert xbmcplugin.resolved and xbmcplugin.resolved[-1][1] is True


def test_packed_episode_id_is_expanded(home):
    _play(home, imdb="tt0903747:1:2", type="tv")
    assert home.getProperty(PROP + "playing_id") == "tt0903747:1:2"
    assert home.getProperty(PROP + "playing_type") == "series"


def test_separate_season_episode_are_packed(home):
    _play(home, imdb="tt0903747", type="series", season="1", episode="2")
    assert home.getProperty(PROP + "playing_id") == "tt0903747:1:2"


def test_packed_id_wins_over_conflicting_separate_params(home):
    # The cast contract (ARCHITECTURE.md): the packed tt..:S:E form wins when
    # present. A stray separate season/episode must NOT override it, or the
    # service scrobbles the wrong episode.
    _play(home, imdb="tt0903747:1:2", type="tv", season="9", episode="9")
    assert home.getProperty(PROP + "playing_id") == "tt0903747:1:2"


def test_separate_params_fill_an_empty_packed_slot(home):
    # Packed present but a slot empty -> the separate param is the fallback.
    _play(home, imdb="tt0903747::", type="tv", season="3", episode="4")
    assert home.getProperty(PROP + "playing_id") == "tt0903747:3:4"


def test_idless_cast_clears_any_previous_stash(home):
    # First a real cast leaves a stash...
    _play(home, imdb="tt1375666", type="movie")
    assert home.getProperty(PROP + "playing_id") == "tt1375666"
    # ...then an id-less cast (e.g. Live TV) must clear it, or the service would
    # keep scrobbling the previous movie against the new stream.
    _play(home, url="http://host/livetv.ts")   # no imdb
    assert home.getProperty(PROP + "playing_id") == ""


def test_empty_url_resolves_failure_and_does_not_stash(home):
    xbmcplugin.reset()
    default.play_url({"imdb": "tt1375666", "type": "movie"})   # no url
    assert xbmcplugin.resolved[-1][1] is False
    assert home.getProperty(PROP + "playing_id") == ""
