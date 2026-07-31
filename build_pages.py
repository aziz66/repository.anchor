#!/usr/bin/env python3
"""Build a GitHub-Pages-hostable Kodi repository for Anchor.

Output layout (served at https://<user>.github.io/repository.anchor/):

    docs/
      addons.xml          - every add-on's <addon> block
      addons.xml.md5      - checksum of addons.xml
      index.html          - simple landing page
      zips/<id>/<id>-<ver>.zip   - installable zips (incl. repository.anchor)

Install flow: users sideload zips/repository.anchor/repository.anchor-<ver>.zip
once, then install/auto-update the Anchor add-ons from the repo.

Run:  python3 build_pages.py
"""

import hashlib
import os
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile

SRC = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(SRC, "docs")
ZIPS = os.path.join(OUT, "zips")

# The published add-ons: the Anchor player and its shared support library,
# plus the repository definition itself.
ADDONS = [
    "repository.anchor",
    "script.module.anchor",
    "plugin.video.anchor",
]

EXCLUDE_DIRS = {"__pycache__", ".git", ".github", ".vscode", "docs"}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".swp")
EXCLUDE_NAMES = {".DS_Store"}


def addon_version(addon_dir):
    return ET.parse(os.path.join(addon_dir, "addon.xml")).getroot().get("version")


# Reproducible zips: every entry gets a FIXED timestamp, order and mode, so the
# bytes depend only on file CONTENT - not on checkout mtimes or walk order. This
# is what lets CI rebuild docs/ and byte-match the committed copy (the staleness
# guard); a plain zf.write() bakes in each file's mtime and fails across machines.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)   # zip minimum; stable across builds


def _addon_files(addon_dir):
    """(full_path, arcname) for every packaged file, in a deterministic order."""
    out = []
    for dirpath, dirnames, filenames in os.walk(addon_dir):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
        for fn in sorted(filenames):
            if fn in EXCLUDE_NAMES or fn.endswith(EXCLUDE_SUFFIX):
                continue
            full = os.path.join(dirpath, fn)
            out.append((full, os.path.relpath(full, SRC)))  # arcname: <id>/...
    return sorted(out, key=lambda p: p[1])


def zip_addon(addon_id):
    addon_dir = os.path.join(SRC, addon_id)
    version = addon_version(addon_dir)
    out_dir = os.path.join(ZIPS, addon_id)
    os.makedirs(out_dir, exist_ok=True)
    zip_path = os.path.join(out_dir, "%s-%s.zip" % (addon_id, version))
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=9) as zf:
        for full, rel in _addon_files(addon_dir):
            zi = zipfile.ZipInfo(rel, date_time=_ZIP_EPOCH)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16   # fixed mode, not the file's
            with open(full, "rb") as fh:
                zf.writestr(zi, fh.read())
    # drop stale version zips
    keep = os.path.basename(zip_path)
    for fn in os.listdir(out_dir):
        if fn.endswith(".zip") and fn != keep:
            os.remove(os.path.join(out_dir, fn))
    # icon next to the zip, for the repo browser
    for cand in (os.path.join(addon_dir, "icon.png"),
                 os.path.join(addon_dir, "resources", "icon.png")):
        if os.path.exists(cand):
            with open(cand, "rb") as s, open(os.path.join(out_dir, "icon.png"), "wb") as d:
                d.write(s.read())
            break
    print("  packaged %s-%s.zip" % (addon_id, version))
    return addon_dir


def build_addons_xml(addon_dirs):
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', "<addons>"]
    for addon_dir in addon_dirs:
        with open(os.path.join(addon_dir, "addon.xml"), encoding="utf-8") as fh:
            xml = fh.read()
        parts.append(re.sub(r"<\?xml[^>]*\?>\s*", "", xml).strip())
    parts.append("</addons>\n")
    return "\n".join(parts)


def write_dir_index(path, header="", body="", only=None):
    """Write an index.html that Kodi's HTTP file browser can navigate.

    Kodi's CHTTPDirectory parses <a href="child"> links and only follows
    SINGLE-SEGMENT children (sub-dirs end with '/'), so each directory needs
    its own listing of its immediate children - GitHub Pages won't autogenerate
    one. Without this, "Install from zip file" shows an empty folder.

    ``only`` restricts the listed children to that set of names, so the browse
    view surfaces just the repository add-on and not the internal component
    zips. ``header`` is human HTML shown above the links; ``body`` replaces the
    link list entirely (used on the root landing page).
    """
    names = sorted(n for n in os.listdir(path) if n != "index.html")
    if only is not None:
        names = [n for n in names if n in only]
    if body:
        content = body
    else:
        links = []
        for n in names:
            slash = "/" if os.path.isdir(os.path.join(path, n)) else ""
            links.append("<a href=\"%s%s\">%s%s</a><br>" % (n, slash, n, slash))
        content = "\n".join(links)
    html = ("<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
            "<title>Anchor Repository</title></head><body>%s\n%s\n</body></html>\n"
            % (header, content))
    with open(os.path.join(path, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(html)


def main():
    os.makedirs(ZIPS, exist_ok=True)
    print("Building Pages repo into %s" % OUT)
    addon_dirs = [zip_addon(a) for a in ADDONS]
    addons_xml = build_addons_xml(addon_dirs)
    with open(os.path.join(OUT, "addons.xml"), "w", encoding="utf-8") as fh:
        fh.write(addons_xml)
    md5 = hashlib.md5(addons_xml.encode("utf-8")).hexdigest()
    with open(os.path.join(OUT, "addons.xml.md5"), "w", encoding="utf-8") as fh:
        fh.write(md5)
    # GitHub Pages serves a real site only with an index; also stops Jekyll
    # from trying to process the tree.
    open(os.path.join(OUT, ".nojekyll"), "w").close()
    repo_ver = addon_version(os.path.join(SRC, "repository.anchor"))
    repo_zip = "repository.anchor-%s.zip" % repo_ver
    # The component add-ons (the plugin and its library) are installed BY the
    # repository over addons.xml via direct URLs, so users never browse to them.
    # Only the repository zip is surfaced: its own dir lists the zip, and the
    # zips/ listing shows only that dir. The internal component dirs get no
    # index.html, so a human browsing sees just the repository add-on.
    write_dir_index(os.path.join(ZIPS, "repository.anchor"))
    write_dir_index(ZIPS, only={"repository.anchor"})
    # Component dirs are never browsed (installed via addons.xml), so they get no
    # index.html; remove any left by an earlier build so nothing stale is served.
    for addon_id in ADDONS:
        if addon_id != "repository.anchor":
            stale = os.path.join(ZIPS, addon_id, "index.html")
            if os.path.exists(stale):
                os.remove(stale)
    # Put the repository zip at the site ROOT so it is the first thing a human
    # sees and can sideload directly, and Kodi's browse of the source URL shows
    # it immediately (single-segment href) without descending into zips/. The
    # copy under zips/repository.anchor/ stays for the repo's own self-update.
    shutil.copyfile(os.path.join(ZIPS, "repository.anchor", repo_zip),
                    os.path.join(OUT, repo_zip))
    root_body = (
        "<h1>Anchor Repository</h1>"
        "<p>Anchor - a Kodi playback companion. It plays a stream handed to it "
        "by a companion app and adds Trakt/Simkl scrobbling, a resume prompt "
        "and skip-intro. It does not browse, scrape or resolve anything.</p>"
        "<p style=\"font-size:1.15em\"><b>&rarr; <a href=\"%s\">%s</a></b> "
        "&mdash; the repository add-on (download, or point Kodi's file source at "
        "this page).</p>"
        "<p><b>Install in Kodi:</b> Add-ons &rarr; <i>Install from zip file</i> "
        "&rarr; the <code>%s</code> above. Then Add-ons &rarr; <i>Install from "
        "repository</i> &rarr; <b>Anchor Repository</b> &rarr; install "
        "<b>Anchor</b> (it auto-updates afterwards). If you add this page as a "
        "file source, use the URL <b>with a trailing slash</b>.</p>"
        "<hr><p style=\"font-size:0.85em;color:#666;max-width:48em\">"
        "Anchor is an independent project and is <b>not affiliated with, "
        "endorsed by, or associated with</b> the Kodi/XBMC Foundation, Trakt, "
        "Simkl or IntroDB. It ships <b>no media, content or stream sources</b> - "
        "it only plays streams handed to it by a companion app. Do not use it "
        "for piracy or to access content you are not authorised to. "
        "Licensed under GPL-3.0-or-later.</p>"
        % (repo_zip, repo_zip, repo_zip))
    write_dir_index(OUT, body=root_body)
    print("addons.xml md5: %s" % md5)
    print("Done. Serve docs/ via GitHub Pages.")


if __name__ == "__main__":
    main()
