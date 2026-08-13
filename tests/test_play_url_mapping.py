"""default.play_url - what actually reaches the Kodi ListItem.

Sibling of test_play_url.py, which covers the identity stash; this one covers
the METADATA contract. It exists because that contract fails silently: a dropped
field just looks like a slightly emptier info screen, never an error. Confirmed
on hardware 2026-08-06 (Kodi 22.0 beta1) that eight fields were being discarded
and that a supplied episode still was overwritten by the poster.
"""

import json

import default
import xbmcgui


def _play(**params):
    xbmcgui.reset()
    params.setdefault("url", "http://host/The.Release.Name.mkv")
    default.play_url(params)
    return xbmcgui.ListItem.last.getArt()


def test_flat_params_keep_working():
    """The original contract must survive the mapper rewrite untouched."""
    art = _play(type="movie", imdb="tt1", title="T", year="2008",
                genre="A,B", plot="P", poster="http://poster.jpg")
    calls = xbmcgui.info_calls
    assert calls.get("setTitle") == [("T",)]
    assert calls.get("setYear") == [(2008,)]
    assert calls.get("setGenres") == [(["A", "B"],)]
    assert art.get("thumb") == "http://poster.jpg", "poster remains the thumb fallback"


def test_supplied_thumb_is_not_overwritten_by_the_poster():
    """The bug this release exists for: art["thumb"] was unconditionally set to
    the poster, discarding the per-episode still the provider returns."""
    art = _play(type="series", imdb="tt2", season="2", episode="6",
                poster="http://poster.jpg", thumb="http://still.jpg")
    assert art.get("poster") == "http://poster.jpg"
    assert art.get("thumb") == "http://still.jpg"


def test_meta_json_carries_the_fields_the_flat_contract_lacked():
    meta = {
        "rating": "7.4", "duration": 2100, "mpaa": "TV-14",
        "premiered": "2026-07-24T04:00:00.000Z", "studio": "Apple TV+",
        "director": ["A B"], "writer": "C D",
        "cast": [{"name": "X", "role": "Y"}],
        "art": {"clearlogo": "http://logo.png", "landscape": "http://land.jpg"},
    }
    art = _play(type="series", imdb="tt3", season="1", episode="1",
                poster="http://p.jpg", meta=json.dumps(meta))
    calls = xbmcgui.info_calls
    assert calls.get("setRating") == [(7.4, 0, "imdb", True)]
    assert calls.get("setDuration") == [(2100,)], "duration is seconds, as an int"
    assert calls.get("setMpaa") == [("TV-14",)]
    assert calls.get("setPremiered") == [("2026-07-24",)], "ISO-8601 trimmed to a date"
    assert calls.get("setStudios") == [(["Apple TV+"],)]
    assert calls.get("setWriters") == [(["C D"],)], "a csv string becomes a list"
    assert len(calls.get("setCast", [([],)])[0][0]) == 1
    assert art.get("clearlogo") == "http://logo.png"
    assert art.get("landscape") == "http://land.jpg"


def test_bad_input_degrades_and_never_raises():
    """A cast must still play when the metadata is broken."""
    _play(type="movie", imdb="tt4", meta="{not json")
    _play(type="movie", imdb="tt5",
          meta=json.dumps({"unknown_field": 1, "duration": "abc"}))
    assert "setDuration" not in xbmcgui.info_calls, "unparsable value skipped, not fatal"


def test_a_non_dict_art_does_not_stop_playback():
    """Broken metadata must degrade, never raise: `art` as a list or a string used to escape
    play_url entirely, so the cast never resolved."""
    for broken in (["http://x.jpg"], "http://x.jpg", 7):
        art = _play(type="movie", imdb="tt7", poster="http://p.jpg",
                    meta=json.dumps({"art": broken}))
        assert art.get("poster") == "http://p.jpg", "the flat art survives a broken meta.art"


def test_one_bad_cast_entry_does_not_discard_the_others():
    """A single unusable entry used to return [], costing the user every actor."""
    _play(type="movie", imdb="tt8", meta=json.dumps({"cast": [
        {"name": "Good"}, {"name": "Bad", "order": "not-a-number"},
        {"name": "AlsoGood", "order": None}, {"no_name": 1}, "PlainString",
    ]}))
    actors = xbmcgui.info_calls.get("setCast", [([],)])[0][0]
    assert [a.name for a in actors] == ["Good", "Bad", "AlsoGood", "PlainString"]


def test_meta_wins_where_it_overlaps_a_flat_param():
    _play(type="movie", imdb="tt6", title="Flat",
          meta=json.dumps({"title": "FromMeta"}))
    assert xbmcgui.info_calls.get("setTitle") == [("FromMeta",)]


def test_a_string_cast_is_ignored_not_split_into_characters():
    """A non-list `cast` is malformed input. It must be dropped, not iterated:
    enumerate('Tom Hanks') would otherwise make one actor per character."""
    _play(type="movie", imdb="tt10", meta=json.dumps({"cast": "Tom Hanks"}))
    actors = xbmcgui.info_calls.get("setCast", [([],)])[0][0]
    assert actors == [], "a string cast yields no actors, not one per letter"


def test_meta_cannot_override_the_scrobble_identity():
    """season/episode build the id the resident service scrobbles, and it reads
    them from the flat params - so meta must never change them, or Kodi would
    show an episode the service never reports. Other meta fields still overlay."""
    _play(type="series", imdb="tt11", season="1", episode="2",
          meta=json.dumps({"season": 9, "episode": 9, "year": 2020}))
    calls = xbmcgui.info_calls
    assert calls.get("setSeason") == [(1,)], "flat season wins over meta"
    assert calls.get("setEpisode") == [(2,)], "flat episode wins over meta"
    assert calls.get("setYear") == [(2020,)], "a non-identity meta field still applies"
