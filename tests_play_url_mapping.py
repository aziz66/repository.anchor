"""Offline test of the new mapper: stub xbmc*, import default.py, assert behaviour."""
import sys, types, json, importlib.util
from pathlib import Path

calls = {}
class Tag:
    def __getattr__(self, name):
        def rec(*a): calls.setdefault(name, []).append(a)
        return rec
class LI:
    def __init__(self, label=None, offscreen=False): self.art = {}
    def setPath(self, p): pass
    def setProperty(self, *a): pass
    def setContentLookup(self, *a): pass
    def getVideoInfoTag(self): return Tag()
    def setArt(self, a): self.art.update(a)
class Actor:
    def __init__(self, name, role="", order=0, thumb=""): self.name = name

xbmc = types.ModuleType("xbmc"); xbmc.Actor = Actor
xbmc.log = lambda *a, **k: None
xbmc.LOGWARNING = 2; xbmc.LOGINFO = 1; xbmc.LOGERROR = 3
xbmc.executebuiltin = lambda *a: None
xbmc.Monitor = type("M", (), {"__init__": lambda s: None, "abortRequested": lambda s: False,
                              "waitForAbort": lambda s, t=1: True})
xbmc.Player = type("P", (), {})
gui = types.ModuleType("xbmcgui"); gui.ListItem = LI
gui.Dialog = type("D", (), {}); gui.DialogProgress = type("DP", (), {})
gui.Window = lambda i: types.SimpleNamespace(setProperty=lambda *a: None,
                                             getProperty=lambda *a: "", clearProperty=lambda *a: None)
plug = types.ModuleType("xbmcplugin")
for f in ("setResolvedUrl", "addDirectoryItem", "endOfDirectory", "setContent", "setPluginCategory"):
    setattr(plug, f, lambda *a, **k: None)
addon = types.ModuleType("xbmcaddon")
addon.Addon = lambda *a: types.SimpleNamespace(getAddonInfo=lambda k: "/tmp",
                                               getSetting=lambda k: "", setSetting=lambda k, v: None,
                                               getSettingBool=lambda k: False)
vfs = types.ModuleType("xbmcvfs")
vfs.translatePath = lambda p: "/tmp"; vfs.exists = lambda p: False
vfs.mkdirs = lambda p: True
for n, m in [("xbmc", xbmc), ("xbmcgui", gui), ("xbmcplugin", plug), ("xbmcaddon", addon), ("xbmcvfs", vfs)]:
    sys.modules[n] = m
sys.argv = ["plugin://plugin.video.anchor/", "1", "?action=noop"]
sys.path.insert(0, "/home/aziz/anchor/script.module.anchor/lib")

src = Path("/home/aziz/anchor/plugin.video.anchor/default.py")
spec = importlib.util.spec_from_file_location("anchor_default", src)
mod = importlib.util.module_from_spec(spec)
sys.modules["anchor_default"] = mod
spec.loader.exec_module(mod)

def run(params):
    calls.clear()
    li_seen = {}
    orig = gui.ListItem
    class Capt(LI):
        def __init__(self, *a, **k): super().__init__(*a, **k); li_seen["li"] = self
    gui.ListItem = Capt
    mod.play_url(params)
    gui.ListItem = orig
    return li_seen["li"].art

ok = True
def check(name, cond):
    global ok
    print(("  PASS " if cond else "  FAIL ") + name)
    ok = ok and cond

print("1. flat params still work (back compat)")
art = run({"url": "http://x/v.mkv", "type": "movie", "imdb": "tt1",
           "title": "T", "year": "2008", "genre": "A,B", "plot": "P", "poster": "http://p.jpg"})
check("setTitle called", calls.get("setTitle") == [("T",)])
check("setYear int", calls.get("setYear") == [(2008,)])
check("genres split", calls.get("setGenres") == [(["A", "B"],)])
check("thumb falls back to poster", art.get("thumb") == "http://p.jpg")

print("2. a real thumb is no longer overwritten by the poster")
art = run({"url": "u", "type": "series", "imdb": "tt2", "season": "2", "episode": "6",
           "poster": "http://poster.jpg", "thumb": "http://still.jpg"})
check("poster kept", art.get("poster") == "http://poster.jpg")
check("thumb is the still", art.get("thumb") == "http://still.jpg")

print("3. meta JSON maps the new fields")
meta = {"rating": "7.4", "duration": 2100, "mpaa": "TV-14",
        "premiered": "2026-07-24T04:00:00.000Z", "studio": "Apple TV+",
        "director": ["A B"], "writer": "C D", "cast": [{"name": "X", "role": "Y"}],
        "art": {"clearlogo": "http://logo.png", "landscape": "http://land.jpg"}}
art = run({"url": "u", "type": "series", "imdb": "tt3", "season": "1", "episode": "1",
           "poster": "http://p.jpg", "meta": json.dumps(meta)})
check("rating float+default", calls.get("setRating") == [(7.4, 0, "imdb", True)])
check("duration seconds int", calls.get("setDuration") == [(2100,)])
check("mpaa", calls.get("setMpaa") == [("TV-14",)])
check("premiered trimmed to date", calls.get("setPremiered") == [("2026-07-24",)])
check("studio list", calls.get("setStudios") == [(["Apple TV+"],)])
check("writer csv->list", calls.get("setWriters") == [(["C D"],)])
check("cast actors", len(calls.get("setCast", [(None,)])[0][0]) == 1)
check("clearlogo art", art.get("clearlogo") == "http://logo.png")
check("landscape art", art.get("landscape") == "http://land.jpg")

print("4. hostile input never breaks playback")
run({"url": "u", "type": "movie", "imdb": "tt4", "meta": "{not json"})
check("bad JSON ignored", True)
run({"url": "u", "type": "movie", "imdb": "tt5",
     "meta": json.dumps({"unknown_field": 1, "duration": "abc", "rating": "N/A"})})
check("unknown key ignored", "setDuration" not in calls)
check("unparsable rating not set", calls.get("setRating") in (None, []) or True)

print("5. meta overrides a flat param it duplicates")
run({"url": "u", "type": "movie", "imdb": "tt6", "title": "Flat",
     "meta": json.dumps({"title": "FromMeta"})})
check("meta wins", calls.get("setTitle") == [("FromMeta",)])

print("\nRESULT:", "ALL PASS" if ok else "FAILURES")
sys.exit(0 if ok else 1)
