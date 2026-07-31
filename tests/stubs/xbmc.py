"""Minimal xbmc stub for running the pure-Python logic outside Kodi."""

LOGDEBUG = 0
LOGINFO = 1
LOGWARNING = 2
LOGERROR = 3
LOGFATAL = 4

log_lines = []  # (level, msg) captured for assertions


def log(msg, level=LOGINFO):
    log_lines.append((level, msg))


class Player:
    def __init__(self):
        pass

    def isPlayingVideo(self):
        return False

    def getTime(self):
        return 0.0

    def getTotalTime(self):
        return 0.0

    def getPlayingFile(self):
        return ""

    def seekTime(self, seconds):
        pass


class Monitor:
    def __init__(self):
        pass

    def abortRequested(self):
        return False

    def waitForAbort(self, timeout=0):
        return True


def executebuiltin(*args, **kwargs):
    pass


def getCondVisibility(*args, **kwargs):
    return False


def getInfoLabel(*args, **kwargs):
    return ""


def sleep(ms):
    pass
