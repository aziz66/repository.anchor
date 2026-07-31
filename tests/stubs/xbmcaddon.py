"""Minimal xbmcaddon stub. Settings live in a module-level dict tests can seed."""

settings = {}


class Addon:
    def __init__(self, id=None):
        self._id = id or "plugin.video.anchor"

    def getSetting(self, key):
        return settings.get(key, "")

    def setSetting(self, key, value):
        settings[key] = value

    def getAddonInfo(self, key):
        return {
            "version": "0.1.10",
            "path": "/tmp/anchor",
            "id": self._id,
            "name": "Anchor",
            "profile": "/tmp/anchor-profile",
        }.get(key, "")

    def getLocalizedString(self, string_id):
        return ""
