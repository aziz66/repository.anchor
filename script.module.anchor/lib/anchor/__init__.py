"""Shared support library for Anchor (Kodi): Trakt/Simkl scrobbling,
cross-device resume, and IntroDB intro/recap skipping."""

from . import _compat  # noqa: F401 - inject _scproxy stub before urllib.request (iOS/tvOS)

__version__ = "0.1.21"
