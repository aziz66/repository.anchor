"""ID helpers for content ids.

Content IDs are prefixed strings, e.g.::

    tt0111161            IMDB movie
    tt0386676:1:1        IMDB series episode (id:season:episode)
    tmdb:603             TMDB
    kitsu:1              Kitsu anime

Anchor identifies playback by the IMDB (``tt...``) forms above.
"""

from __future__ import annotations


def split_series_id(content_id):
    """Return ``(base, season, episode)`` for ``tt123:1:2`` style ids.

    For non-episode ids returns ``(content_id, None, None)``.
    """
    parts = content_id.split(":")
    if len(parts) == 3 and parts[-2].isdigit() and parts[-1].isdigit():
        return ":".join(parts[:-2]), int(parts[-2]), int(parts[-1])
    return content_id, None, None


def base_id(content_id):
    """The content id without any ``:season:episode`` suffix."""
    return split_series_id(content_id)[0]
