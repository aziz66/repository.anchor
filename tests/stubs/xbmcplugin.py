"""Minimal xbmcplugin stub. Captures resolved urls / directory items so tests
can assert what the plugin handed back to Kodi."""

resolved = []   # (handle, succeeded, listitem)
items = []      # (handle, url, listitem, isFolder)


def reset():
    resolved.clear()
    items.clear()


def setResolvedUrl(handle, succeeded, listitem):
    resolved.append((handle, succeeded, listitem))


def addDirectoryItem(handle, url, listitem, isFolder=False, totalItems=0):
    items.append((handle, url, listitem, isFolder))
    return True


def endOfDirectory(handle, succeeded=True, updateListing=False, cacheToDisc=True):
    pass


def setContent(*a, **k):
    pass


def setPluginCategory(*a, **k):
    pass


def addSortMethod(*a, **k):
    pass
