#!/usr/bin/env python3
"""Prepare a Ren'Py game distribution for an Android build.

This works around two limitations of Ren'Py 8.5.x on Android:

1. ``.rpa`` archives shipped as APK assets are never indexed. ``renpy.loader``
   only scans the filesystem for archives (``scandirfiles_from_filesystem``
   fills ``arc_files``), while ``scandirfiles_from_apk`` only adds asset
   entries.  Archives therefore have to be unpacked into loose game files.

2. aapt stores asset names as UTF-8 bytes without setting the ZIP "UTF-8"
   flag (bit 11).  Python's ``zipfile`` then decodes them as cp437, so a file
   called ``京華老宋体GB.ttf`` can never be loaded by name.  Every non-ASCII
   asset name is renamed to ASCII, and references from ``.rpy`` sources are
   patched (the corresponding ``.rpyc`` is dropped so Ren'Py recompiles it).

The script also writes ``android.json`` for RAPT.

Usage::

    python3 scripts/prepare.py --source ZATO-linux.zip --out build/project
    python3 scripts/prepare.py --source /path/to/game-directory --out build/project
"""

import argparse
import hashlib
import os
import pickle
import re
import shutil
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path

# Ren'Py's font lookup tries "tl/<lang>/fonts/<name>" and "fonts/<name>".
# Moving the CJK fonts to game/fonts/<ascii> makes them loadable from any
# translation, and the ASCII names below are what the patch script refers to.
RENAME_HINTS = {
    "京華老宋体GB.ttf": "JingHuaLaoSongGB.ttf",
    "博雅宋_Cour_Final.ttf": "BoyaSong_Cour.ttf",
}

COPY_IGNORE = ("saves", "cache", "log.txt", "errors.txt", "traceback.txt",
               "image_cache.txt", ".nomedia")
SKIP_ARCHIVE_DIRS = ("cache/",)

DEFAULT_CONFIG = {
    "package": "org.zato.android",
    "name": "Z.A.T.O. // I Love the World and Everything in It",
    "icon_name": "Z.A.T.O.",
    "version": "1.0",
    "numeric_version": 1,
    "orientation": "sensorLandscape",
    "permissions": ["VIBRATE", "INTERNET"],
    "store": "none",
    "update_icons": True,
    "update_always": True,
    "update_keystores": True,
    "include_pil": False,
    "include_sqlite": False,
    "expansion": False,
    "source": False,
    "layout": None,
    "heap_size": None,
    "google_play_key": None,
    "google_play_salt": None,
}


def log(msg=""):
    print(msg, flush=True)


# --------------------------------------------------------------------------- #
# source handling
# --------------------------------------------------------------------------- #

def find_distribution_root(base: Path) -> Path:
    """Locate the directory that contains ``game/`` (and usually ``renpy/``)."""
    if (base / "game").is_dir():
        return base
    candidates = [p for p in base.rglob("game") if p.is_dir()]
    if not candidates:
        sys.exit(f"error: no 'game' directory found under {base}")
    # Prefer a directory that also has a renpy/ tree (a real distribution).
    candidates.sort(key=lambda p: (len(p.parts), 0 if (p.parent / "renpy").is_dir() else 1))
    return candidates[0].parent


def stage_source(source: Path, workdir: Path) -> Path:
    if source.is_file() and source.suffix.lower() == ".zip":
        log(f"Extracting {source} ...")
        extract = workdir / "source"
        extract.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(source) as zf:
            zf.extractall(extract)
        return find_distribution_root(extract)
    if not source.is_dir():
        sys.exit(f"error: {source} is neither a .zip file nor a directory")
    return find_distribution_root(source)


# --------------------------------------------------------------------------- #
# .rpa archives
# --------------------------------------------------------------------------- #

def read_rpa_index(path: Path):
    """Return (key, {name: entry}) for an RPA-3.0 / RPA-2.0 archive."""
    with open(path, "rb") as f:
        line = f.readline(80)
        parts = line.split()
        if not parts or not parts[0].startswith(b"RPA-"):
            sys.exit(f"error: {path} is not an RPA archive (header {line!r})")
        magic = parts[0].decode("ascii", "replace")
        if magic == "RPA-3.0":
            offset, key = int(parts[1], 16), int(parts[2], 16)
        elif magic == "RPA-2.0":
            offset, key = int(parts[1], 16), 0
        else:
            sys.exit(f"error: {path}: unsupported archive version {magic}")
        f.seek(offset)
        index = pickle.loads(zlib.decompress(f.read()))
    return key, index


def read_rpa_entry(f, key: int, segments) -> bytes:
    """Read one archive entry.

    RPA-3.0 indexes map a file name to a *list* of segments, each either
    (offset, length) or (offset, length, prefix).  Offsets and lengths are
    XOR-obfuscated with the archive key; the prefix is literal data.
    """
    out = []
    for segment in segments:
        if len(segment) == 2:
            offset, dlen = segment
            start = b""
        else:
            offset, dlen, start = segment
        if isinstance(start, str):
            start = start.encode("latin-1")
        f.seek(offset ^ key)
        out.append(start + f.read(dlen ^ key))
    return b"".join(out)


def unpack_archives(game_dir: Path) -> int:
    """Unpack every .rpa next to the loose files, then delete the archives."""
    archives = sorted(game_dir.glob("*.rpa"))
    if not archives:
        return 0

    log(f"Unpacking {len(archives)} archive(s) ...")
    indexes = [(path, *read_rpa_index(path)) for path in archives]

    # Later archives win for duplicate names (e.g. intro.webm in the OP patch).
    entries = {}
    for path, key, index in indexes:
        for name, entry in index.items():
            segments = entry if isinstance(entry, list) else [entry]
            entries[name] = (path, key, segments)

    written = skipped = 0
    for name, (path, key, segments) in sorted(entries.items()):
        if name.startswith(SKIP_ARCHIVE_DIRS):
            skipped += 1
            continue
        target = game_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "rb") as f:
            target.write_bytes(read_rpa_entry(f, key, segments))
        written += 1

    for path in archives:
        path.unlink()
    cache = game_dir / "cache"
    if cache.is_dir():
        shutil.rmtree(cache)
    log(f"  {written} files written, {skipped} cache entries skipped")
    return written


# --------------------------------------------------------------------------- #
# non-ASCII asset names
# --------------------------------------------------------------------------- #

def ascii_component(name: str) -> str:
    """Best-effort ASCII replacement for a single path component."""
    if name in RENAME_HINTS:
        return RENAME_HINTS[name]
    stem, ext = os.path.splitext(name)
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    if not cleaned:
        cleaned = f"asset-{digest}"
    return cleaned + ext


def plan_renames(game_dir: Path):
    """Return [(old_rel, new_rel)] for every non-ASCII path under game/."""
    renames = []
    used = set()
    for path in sorted(game_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(game_dir).as_posix()
        if rel.isascii():
            continue
        new_name = ascii_component(path.name)
        parts = Path(rel).parts
        # Translation fonts live under tl/<lang>/fonts/; hoist them to fonts/
        # so that both "tl/<lang>/fonts/<name>" and "fonts/<name>" resolve.
        if len(parts) >= 3 and parts[0] == "tl" and parts[-2] == "fonts":
            new_rel = f"fonts/{new_name}"
        else:
            new_rel = str(Path(rel).with_name(new_name)).replace(os.sep, "/")
        candidate = new_rel
        n = 2
        while candidate in used or (game_dir / candidate).exists():
            stem, ext = os.path.splitext(new_rel)
            candidate = f"{stem}-{n}{ext}"
            n += 1
        used.add(candidate)
        renames.append((rel, candidate))
    return renames


def apply_renames(game_dir: Path, renames) -> None:
    for old_rel, new_rel in renames:
        src = game_dir / old_rel
        dst = game_dir / new_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        log(f"  renamed {old_rel} -> {new_rel}")


def patch_sources(game_dir: Path, renames):
    """Rewrite references to renamed files inside .rpy / .rpym sources."""
    if not renames:
        return []
    patterns = []
    for old_rel, new_rel in renames:
        old_base = Path(old_rel).name
        new_base = Path(new_rel).name
        # Longest first so "fonts/x.ttf" is replaced before bare "x.ttf".
        for old in {old_rel, f"fonts/{old_base}", old_base}:
            if old and old not in {p[0] for p in patterns}:
                replacement = new_rel if old != old_base else new_base
                patterns.append((old, replacement))
    patterns.sort(key=lambda p: len(p[0]), reverse=True)

    patched = []
    for path in sorted(game_dir.rglob("*")):
        if not path.is_file() or path.suffix not in (".rpy", ".rpym"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            log(f"  warning: {path} is not UTF-8, skipped")
            continue
        original = text
        for old, new in patterns:
            text = text.replace(old, new)
        if text == original:
            continue
        path.write_text(text, encoding="utf-8")
        patched.append(path)
        log(f"  patched {path.relative_to(game_dir)}")
        rpyc = path.with_suffix(path.suffix + "c")
        if rpyc.exists():
            rpyc.unlink()
            log(f"  removed stale {rpyc.relative_to(game_dir)} (will be recompiled)")
    return patched


def drop_redundant_sources(game_dir: Path, patched) -> int:
    """Keep only compiled scripts where no patch is needed.

    Sources are copied with fresh mtimes, so leaving both foo.rpy and foo.rpyc
    would make Ren'Py recompile everything (slow, and some fan translations
    only ship .rpyc for a reason).  Keep the .rpyc unless we patched the .rpy.
    """
    patched_set = {str(p) for p in patched}
    removed = 0
    for path in sorted(game_dir.rglob("*.rpy")):
        if str(path) in patched_set:
            continue
        if path.with_suffix(".rpyc").exists():
            path.unlink()
            removed += 1
    return removed


def check_ascii(game_dir: Path) -> None:
    leftovers = [
        p.relative_to(game_dir).as_posix()
        for p in game_dir.rglob("*")
        if p.is_file() and not p.relative_to(game_dir).as_posix().isascii()
    ]
    if leftovers:
        log("  warning: non-ASCII asset names remain (they will not load on Android):")
        for name in leftovers[:20]:
            log(f"    {name}")
    return leftovers


# --------------------------------------------------------------------------- #
# icons
# --------------------------------------------------------------------------- #

ICON_SIZE = 432          # adaptive icon canvas
ICON_ART_SIZE = 264      # 61% of the 108dp safe zone
ICON_SOURCES = (
    "gui/icon.icns",
    "gui/icon.ico",
    "gui/icon.png",
    "gui/window_icon.png",
)
ICON_OVERRIDES = (
    "android-icon_foreground.png",
    "android-icon_background.png",
    "android-icon_mask.png",
)


def copy_icon_overrides(project: Path, dist_root: Path) -> None:
    """Copy android-icon_*.png shipped at the distribution root, if any."""

    copied = 0
    for name in ICON_OVERRIDES:
        src = dist_root / name
        if src.is_file() and not (project / name).exists():
            shutil.copy2(src, project / name)
            copied += 1
    if copied:
        log(f"Icons: copied {copied} file(s) from the distribution")


def load_icon_art(game_dir: Path):
    """Return the game's icon artwork as a square RGBA image, or None."""

    try:
        from PIL import Image
    except ImportError:
        return None

    best = None
    for rel in ICON_SOURCES:
        path = game_dir / rel
        if not path.is_file():
            continue
        try:
            im = Image.open(path)
            sizes = im.info.get("sizes")
            if im.format == "ICO" and sizes:
                im.size = max(sizes)
            im = im.convert("RGBA")
            im.load()
        except Exception:
            continue
        if best is None or im.width * im.height > best.width * best.height:
            best = im.copy()

    if best is None:
        return None

    bbox = best.getbbox()
    if bbox:
        best = best.crop(bbox)

    side = max(best.width, best.height)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(best, ((side - best.width) // 2, (side - best.height) // 2))
    return canvas


def setup_icons(project: Path) -> None:
    """Create android-icon_foreground/background.png from the game's icon."""

    fg_path = project / "android-icon_foreground.png"
    bg_path = project / "android-icon_background.png"

    if fg_path.is_file() and bg_path.is_file():
        log("Icons: using android-icon_*.png from the distribution")
        return

    try:
        from PIL import Image, ImageDraw, ImageFilter
    except ImportError:
        log("Icons: warning: Pillow is not installed; RAPT's default icon will be used")
        return

    art = load_icon_art(project / "game")
    if art is None:
        log("Icons: warning: no artwork found under game/gui/; RAPT's default icon will be used")
        return

    # Background: the artwork's average colour, darkened, with a soft highlight.
    average = art.resize((1, 1), Image.LANCZOS).getpixel((0, 0))[:3]
    base = tuple(int(c * 0.45) for c in average)
    background = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), base + (255,))

    highlight = Image.new("L", (ICON_SIZE, ICON_SIZE), 0)
    margin = int(ICON_SIZE * 0.12)
    ImageDraw.Draw(highlight).ellipse(
        (margin, margin, ICON_SIZE - margin, ICON_SIZE - margin), fill=56)
    highlight = highlight.filter(ImageFilter.GaussianBlur(ICON_SIZE * 0.14))
    light = tuple(min(255, int(c * 1.6)) for c in base) + (255,)
    background = Image.composite(
        Image.new("RGBA", (ICON_SIZE, ICON_SIZE), light), background, highlight)

    # Foreground: the artwork on the adaptive-icon safe zone, with feathered edges.
    face = art.resize((ICON_ART_SIZE, ICON_ART_SIZE), Image.LANCZOS)
    alpha = Image.new("L", (ICON_ART_SIZE, ICON_ART_SIZE), 0)
    ImageDraw.Draw(alpha).rounded_rectangle(
        (0, 0, ICON_ART_SIZE - 1, ICON_ART_SIZE - 1),
        radius=int(ICON_ART_SIZE * 0.175), fill=255)
    alpha = alpha.filter(ImageFilter.GaussianBlur(ICON_ART_SIZE * 0.045))
    face.putalpha(Image.composite(face.getchannel("A"), Image.new("L", face.size, 0), alpha))

    foreground = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    offset = (ICON_SIZE - ICON_ART_SIZE) // 2
    foreground.paste(face, (offset, offset), face)

    foreground.save(fg_path)
    background.save(bg_path)
    log(f"Icons: generated {fg_path.name} and {bg_path.name} from game artwork")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def write_android_json(project: Path, args) -> None:
    import json

    config = dict(DEFAULT_CONFIG)
    config.update({
        "package": args.package,
        "name": args.name,
        "icon_name": args.icon_name,
        "version": args.version,
    })
    with open(project / "android.json", "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4, sort_keys=True)
        f.write("\n")
    log(f"Wrote {project / 'android.json'} (package {args.package})")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", required=True,
                    help="itch.io distribution .zip, or a folder that contains game/")
    ap.add_argument("--out", required=True, help="build project directory to create")
    ap.add_argument("--package", default=DEFAULT_CONFIG["package"])
    ap.add_argument("--name", default=DEFAULT_CONFIG["name"])
    ap.add_argument("--icon-name", default=DEFAULT_CONFIG["icon_name"])
    ap.add_argument("--version", default=DEFAULT_CONFIG["version"])
    ap.add_argument("--keep-archives", action="store_true",
                    help="do not unpack .rpa files (only useful once Ren'Py fixes APK archives)")
    args = ap.parse_args()

    source = Path(args.source).expanduser().resolve()
    project = Path(args.out).expanduser().resolve()
    if project.exists() and any(project.iterdir()):
        log(f"warning: {project} is not empty, files will be overwritten")

    with tempfile.TemporaryDirectory(prefix="zato-prepare-") as tmp:
        dist_root = stage_source(source, Path(tmp))
        log(f"Distribution root: {dist_root}")

        game_dst = project / "game"
        if game_dst.exists():
            shutil.rmtree(game_dst)
        game_dst.parent.mkdir(parents=True, exist_ok=True)
        log(f"Copying {dist_root / 'game'} -> {game_dst}")
        shutil.copytree(
            dist_root / "game",
            game_dst,
            ignore=shutil.ignore_patterns(*COPY_IGNORE),
        )
        copy_icon_overrides(project, dist_root)

    if not args.keep_archives:
        unpack_archives(game_dst)
    else:
        log("Keeping .rpa archives (--keep-archives)")

    log("Sanitizing non-ASCII asset names ...")
    renames = plan_renames(game_dst)
    if renames:
        apply_renames(game_dst, renames)
        patched = patch_sources(game_dst, renames)
    else:
        patched = []
        log("  nothing to rename")
    removed = drop_redundant_sources(game_dst, patched)
    if removed:
        log(f"  removed {removed} source .rpy file(s) that have a compiled .rpyc")
    check_ascii(game_dst)

    setup_icons(project)
    write_android_json(project, args)

    size = sum(p.stat().st_size for p in game_dst.rglob("*") if p.is_file())
    count = sum(1 for p in game_dst.rglob("*") if p.is_file())
    log("")
    log(f"Prepared {count} game files ({size / 1e6:.1f} MB) in {game_dst}")
    log(f"Next: bash scripts/build.sh --project {project} --out {project.parent / 'out'}")


if __name__ == "__main__":
    main()
