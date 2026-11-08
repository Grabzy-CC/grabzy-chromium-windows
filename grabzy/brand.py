#!/usr/bin/env python3
"""Rebrand a patched Chromium tree as Grabzy.CC.

    python3 brand.py <chromium src dir> [--check]

Runs after ungoogled-chromium's patches and before domain substitution. Standard
library only. It does four things:

1. Product strings. Every "Chromium" in chromium_strings.grd (and its
   settings part) and components_chromium_strings.grd becomes "Grabzy.CC",
   except "Chromium OS" and "The Chromium Authors". Translations are keyed by a
   hash of the English text, so each changed message gets its new key in every
   .xtb file and "Chromium" is replaced in the translated text too.
2. BRANDING: company and short product name everywhere; the full product and
   installer names on Windows and Linux. macOS keeps PRODUCT_FULLNAME, because
   the app, helper and framework bundle names and the signing scripts follow it.
3. Icons: every file under files/ replaces the file at the same path in the tree.
4. macOS only: rebuilds mac/Assets.car from the new AppIcon set with actool.
5. Release tag: when release.txt sits next to this script (apply.py writes it
   from the tag being built), it goes into chrome/browser/grabzy/release_tag.h,
   which turns the built-in updater on. Without it the updater stays off.

--check only computes and verifies, and writes nothing.
"""

import glob
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile

NAME = "Grabzy.CC"
HERE = os.path.dirname(os.path.abspath(__file__))
# "Chromium OS" (also with a no-break space or a hyphen) and "The Chromium
# Authors" name other things and stay.
WORD = re.compile(r"\bChromium\b(?!\s*-?OS\b|\s+Authors\b)")

# (grd, its parts, its translations)
STRINGS = [
    ("chrome/app/chromium_strings.grd", ["chrome/app/settings_chromium_strings.grdp"],
     "chrome/app/resources/chromium_strings_*.xtb"),
    ("components/components_chromium_strings.grd", [],
     "components/strings/components_chromium_strings_*.xtb"),
]

MESSAGE = re.compile(r'(<message\s)((?:[^>"]|"[^"]*")*)(>)(.*?)(</message>)', re.S)
ATTR = re.compile(r'([\w-]+)="([^"]*)"')
PH = re.compile(r'<ph\s+name="([^"]+)"\s*(?:/>|>.*?</ph>)', re.S)
TRANSLATION = re.compile(r'(<translation id=")(\d+)(">)(.*?)(</translation>)', re.S)
TRANSLATION_IDS = re.compile(r'<translation id="(\d+)"')
TAG = re.compile(r"(<[^>]+>)")


def log(msg):
    print("[brand] " + msg, flush=True)


# --- grit message ids (grit/extern/FP.py, grit/tclib.py) --------------------

def _fingerprint(s):
    fp = int(hashlib.md5(s.encode("utf-8")).hexdigest()[:16], 16)
    return fp - (1 << 64) if fp & (1 << 63) else fp


def message_id(text, meaning=""):
    fp = _fingerprint(text)
    if meaning:
        fp = _fingerprint(meaning) + (fp << 1) + (1 if fp < 0 else 0)
    return str(fp & 0x7FFFFFFFFFFFFFFF)


def _unescape(s):
    s = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), s)
    s = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), s)
    for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"), ("&amp;", "&")):
        s = s.replace(a, b)
    return s


def presentable(inner):
    """The text grit hashes: placeholders by name, entities decoded, outer
    whitespace and ''' quotes trimmed."""
    text = PH.sub(lambda m: m.group(1).upper(), inner.replace("\r\n", "\n"))
    text = _unescape(re.sub(r"<!--.*?-->", "", text, flags=re.S)).strip()
    if text.startswith("'''"):
        text = text[3:]
    if text.endswith("'''"):
        text = text[:-3]
    return text.strip()


def rename_text(s):
    """Replace the name in text, never inside a tag or its attributes."""
    return "".join(p if p.startswith("<") else WORD.sub(NAME, p) for p in TAG.split(s))


# --- 1. strings --------------------------------------------------------------

def brand_strings(src, write):
    for grd, parts, xtb_glob in STRINGS:
        remap, changed = {}, 0
        for path in [grd] + parts:
            full = os.path.join(src, path)
            if not os.path.exists(full):
                sys.exit("[brand] missing %s" % path)
            data = open(full, encoding="utf-8").read()

            def one(m):
                nonlocal changed
                attrs = dict(ATTR.findall(m.group(2)))
                inner = m.group(4)
                new_inner = rename_text(inner)
                if new_inner == inner:
                    return m.group(0)
                changed += 1
                if attrs.get("translateable") != "false" and attrs.get("use_name_for_id") != "true":
                    meaning = attrs.get("meaning", "")
                    old = message_id(presentable(inner), meaning)
                    new = message_id(presentable(new_inner), meaning)
                    if old != new:
                        remap[old] = new
                return m.group(1) + m.group(2) + m.group(3) + new_inner + m.group(5)

            out = MESSAGE.sub(one, data)
            if write and out != data:
                open(full, "w", encoding="utf-8", newline="").write(out)

        xtbs = sorted(glob.glob(os.path.join(src, xtb_glob)))
        if not xtbs:
            sys.exit("[brand] no translations at %s" % xtb_glob)
        known = set()
        for x in xtbs:
            data = open(x, encoding="utf-8").read()
            ids = set(TRANSLATION_IDS.findall(data))
            known |= ids

            def tr(m):
                tid = remap.get(m.group(2), m.group(2))
                if tid != m.group(2) and tid in ids:  # a translation already sits there
                    return ""
                return m.group(1) + tid + m.group(3) + rename_text(m.group(4)) + m.group(5)

            out = TRANSLATION.sub(tr, data)
            if write and out != data:
                open(x, "w", encoding="utf-8", newline="").write(out)

        # Nearly every message is translated into some locale, so if our ids
        # match few of theirs, grit's hashing changed and every locale would
        # fall back to English. Stop instead.
        hit = sum(1 for k in remap if k in known)
        log("%s: %d messages renamed, %d re-keyed, %d of them translated, %d .xtb files"
            % (grd, changed, len(remap), hit, len(xtbs)))
        if remap and hit < len(remap) * 0.95:
            sys.exit("[brand] message ids do not match the translations; grit's id scheme may have changed")


# --- 2. BRANDING -------------------------------------------------------------

def brand_file(src, write, mac):
    path = os.path.join(src, "chrome/app/theme/chromium/BRANDING")
    values = {
        "COMPANY_FULLNAME": NAME,
        "COMPANY_SHORTNAME": NAME,
        "PRODUCT_SHORTNAME": NAME,
        "COPYRIGHT": "Copyright @LASTCHANGE_YEAR@ Grabzy.CC and The Chromium Authors. All rights reserved.",
    }
    if not mac:
        values.update(PRODUCT_FULLNAME=NAME,
                      PRODUCT_INSTALLER_FULLNAME=NAME + " Installer",
                      PRODUCT_INSTALLER_SHORTNAME=NAME + " Installer")
    lines, seen = [], set()
    for line in open(path, encoding="utf-8").read().splitlines():
        key = line.split("=", 1)[0]
        if key in values:
            line = key + "=" + values[key]
            seen.add(key)
        lines.append(line)
    if seen != set(values):
        sys.exit("[brand] BRANDING lacks %s" % ", ".join(sorted(set(values) - seen)))
    if write:
        open(path, "w", encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")
    log("BRANDING: %s" % ", ".join(sorted(values)))


# --- 3. icons ----------------------------------------------------------------

def brand_icons(src, write):
    root = os.path.join(HERE, "files")
    copied, missing = 0, []
    for dirpath, _, files in os.walk(root):
        for f in files:
            rel = os.path.relpath(os.path.join(dirpath, f), root)
            dst = os.path.join(src, rel)
            if not os.path.exists(dst):
                missing.append(rel.replace("\\", "/"))
                continue
            if write:
                shutil.copyfile(os.path.join(dirpath, f), dst)
            copied += 1
    log("icons: %d replaced" % copied)
    for rel in missing:  # Chromium moved or dropped it: worth a look, not a failed build
        log("warning: no %s in this Chromium, left out" % rel)


# --- 4. macOS asset catalog --------------------------------------------------

RELEASE_TAG_H = "chrome/browser/grabzy/release_tag.h"
PLACEHOLDER = 'kReleaseTag[] = "dev";'


def stamp_release(src, write):
    """The tag being built goes into the updater, so it knows what is running."""
    header = os.path.join(src, RELEASE_TAG_H)
    data = open(header, encoding="utf-8").read()
    path = os.path.join(HERE, "release.txt")
    tag = open(path, encoding="utf-8").read().strip() if os.path.exists(path) else None
    if tag and 'kReleaseTag[] = "%s";' % tag in data:
        return log("release tag %s already set" % tag)
    if PLACEHOLDER not in data:
        sys.exit("[brand] %s: the release tag placeholder is missing" % RELEASE_TAG_H)
    if not tag:
        return log("no release.txt, so the updater stays off")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", tag):
        sys.exit("[brand] release.txt: %r is not a release tag" % tag)
    if write:
        open(header, "w", encoding="utf-8", newline="\n").write(
            data.replace(PLACEHOLDER, 'kReleaseTag[] = "%s";' % tag))
    log("release tag " + tag)


def brand_asset_catalog(src):
    mac = os.path.join(src, "chrome/app/theme/chromium/mac")
    with tempfile.TemporaryDirectory() as tmp:
        cat = os.path.join(tmp, "Assets.xcassets")
        os.makedirs(cat)
        shutil.copyfile(os.path.join(mac, "Assets.xcassets/Contents.json"), os.path.join(cat, "Contents.json"))
        shutil.copytree(os.path.join(mac, "Assets.xcassets/AppIcon.appiconset"), os.path.join(cat, "AppIcon.appiconset"))
        out = os.path.join(tmp, "out")
        os.makedirs(out)
        cmd = ["xcrun", "actool", "--compile", out, "--platform", "macosx",
               "--minimum-deployment-target", "12.0", "--app-icon", "AppIcon",
               "--output-partial-info-plist", os.path.join(tmp, "partial.plist"),
               "--errors", "--warnings", cat]
        r = subprocess.run(cmd, capture_output=True, text=True)
        car = os.path.join(out, "Assets.car")
        if r.returncode != 0 or not os.path.exists(car):
            log("warning: actool failed, the Dock keeps Chromium's icon: %s" % (r.stderr or r.stdout).strip()[:500])
            return
        shutil.copyfile(car, os.path.join(mac, "Assets.car"))
        log("Assets.car rebuilt")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        sys.exit(__doc__)
    src, write = args[0], "--check" not in sys.argv
    mac = sys.platform == "darwin"
    brand_strings(src, write)
    brand_file(src, write, mac)
    brand_icons(src, write)
    stamp_release(src, write)
    if mac and write:
        brand_asset_catalog(src)
    log("done" if write else "check passed")


if __name__ == "__main__":
    main()
