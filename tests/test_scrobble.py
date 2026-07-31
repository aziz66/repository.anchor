"""_payload shaping and the scrobble POST path for both backends, with the
network stubbed at _net.http_json."""

import time

import pytest
from anchor import simkl, trakt

MOVIE = "tt1375666"
EPISODE = "tt0903747:1:2"


@pytest.fixture(autouse=True)
def byo_creds(monkeypatch):
    """BYO app credentials, as the add-on would bind them from settings."""
    monkeypatch.setattr(trakt, "_client_id", lambda: "cid")
    monkeypatch.setattr(trakt, "_client_secret", lambda: "sec")
    monkeypatch.setattr(simkl, "_client_id", lambda: "cid")


# --- payload shaping (pure) ------------------------------------------------

def test_trakt_movie_payload():
    p = trakt._payload("movie", MOVIE, 42.0)
    assert p == {"progress": 42.0, "movie": {"ids": {"imdb": MOVIE}}}


def test_trakt_episode_payload_uses_show_ids_plus_season_number():
    p = trakt._payload("series", EPISODE, 10.0)
    assert p["show"] == {"ids": {"imdb": "tt0903747"}}
    assert p["episode"] == {"season": 1, "number": 2}
    assert "ids" not in p["episode"]        # episode-level imdb is NOT accepted


def test_simkl_episode_payload_matches_trakt_shape():
    p = simkl._payload("series", EPISODE, 10.0)
    assert p["show"] == {"ids": {"imdb": "tt0903747"}}
    assert p["episode"] == {"season": 1, "number": 2}


def test_non_imdb_ids_are_skipped():
    assert trakt._payload("movie", "tmdb:603", 5.0) is None
    assert simkl._payload("movie", "kitsu:1", 5.0) is None


def test_progress_is_clamped_to_0_100():
    assert trakt._payload("movie", MOVIE, 150.0)["progress"] == 100.0
    assert trakt._payload("movie", MOVIE, -5.0)["progress"] == 0.0


# --- scrobble POST path ----------------------------------------------------

def test_trakt_scrobble_posts_and_returns_status(stub_http, monkeypatch):
    trakt._save({"access_token": "T", "refresh_token": "R",
                 "expires_at": time.time() + 99999})
    trakt._gate_.reset()
    stub_http.responses["/scrobble/start"] = (201, "")
    st = trakt.scrobble("start", "movie", MOVIE, 5.0)
    assert st == 201
    posts = [c for c in stub_http.calls if c[0] == "POST" and "/scrobble/start" in c[1]]
    assert len(posts) == 1
    assert posts[0][3] == {"progress": 5.0, "movie": {"ids": {"imdb": MOVIE}}}


def test_trakt_duplicate_start_is_gated_out(stub_http):
    trakt._save({"access_token": "T", "refresh_token": "R",
                 "expires_at": time.time() + 99999})
    trakt._gate_.reset()
    stub_http.responses["/scrobble/start"] = (201, "")
    trakt.scrobble("start", "movie", MOVIE, 5.0)
    trakt.scrobble("start", "movie", MOVIE, 6.0)   # redundant -> must not POST
    posts = [c for c in stub_http.calls if "/scrobble/start" in c[1]]
    assert len(posts) == 1


def test_simkl_scrobble_posts_with_required_query_params(stub_http, monkeypatch):
    simkl._save({"access_token": "S"})
    simkl._gate_.reset()
    monkeypatch.setattr(simkl, "_pace", lambda: None)   # skip the 1/sec sleep
    stub_http.responses["/scrobble/start"] = (201, "")
    st = simkl.scrobble("start", "movie", MOVIE, 5.0)
    assert st == 201
    url = [c[1] for c in stub_http.calls if "/scrobble/start" in c[1]][0]
    for q in ("client_id=", "app-name=Anchor", "app-version="):
        assert q in url
