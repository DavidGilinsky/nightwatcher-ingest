#!/usr/bin/env python3
# ============================================================================
#  Author   : David Gilinsky
#  File     : nwingest.py
#  Purpose  : Config-driven FITS ingest. Watch a directory, classify each
#             frame from its header, rename it to a standard, and file it into
#             an archive tree. Optional SQM stamping, external hooks, and
#             NightWatcher2 web UI registration.
#  Created  : 2026-07-21
#  Modified : 2026-10-04
#  Version  : 0.2.0
#  License  : GPL-3.0-or-later
# ============================================================================
"""nwingest: watch, classify, rename, and file FITS frames by configuration.

Modes:
  plan      scan a directory and print what would happen (read-only, moves nothing)
  once      process the incoming directory a single time, then exit
  watch     poll the incoming directory forever (run under systemd)
  backfill  add nwingest's missing header cards to already-filed frames
            (FILTER, SQM); --dry-run lists what would change and writes nothing

Everything is header-driven: the paths and filenames the capture apps produce
are never trusted, only the FITS header. See nwingest.example.yaml.
"""

from __future__ import annotations

import argparse
import errno
import fnmatch
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta

__version__ = "0.2.0"

try:
    import yaml
except ImportError:
    sys.exit("nwingest: PyYAML is required  ->  pip install pyyaml")


# ---------------------------------------------------------------------------
# Built-in defaults. A user config is deep-merged over these, so a partial
# config only needs to state what it changes. Nothing here is site-specific
# except values a standalone user would obviously override (rigs, sites).
# ---------------------------------------------------------------------------
DEFAULTS = {
    "watch": {
        "incoming": "/astronomy/astro-imaging/incoming",
        "poll_seconds": 45,
        "stable_seconds": 10,
        # A file whose size is still short of header + data is left in place
        # (something is still writing it). After this many seconds without
        # growth it is quarantined as truncated instead.
        "truncated_after_s": 600,
        "patterns": ["*.fits", "*.fit", "*.fts"],
        "ignore": ["_gsdata_", ".tmp", ".part"],
    },
    "destination": {
        "root": "/astronomy/astro-imaging",
        "normalize_ext": ".fits",
        "on_conflict": "sequence",
    },
    "routes": {
        "light": {
            "path": "lights/{object}/{rig}/{night}/{filter}",
            "filename": "{object}_{utc}Z_{seq}_{rig}_{filter}_{exp}s_g{gain}_o{offset}_bin{bin}_{temp}C",
        },
        "dark": {
            "path": "calibration/dark/{camera}/{exp}s_g{gain}_o{offset}_bin{bin}_{temp}C/{night}",
            "filename": "Dark_{utc}Z_{seq}_{camera}_{exp}s_g{gain}_o{offset}_bin{bin}_{temp}C",
        },
        "flat": {
            "path": "calibration/flat/{rig}/{filter}/{night}",
            "filename": "Flat_{utc}Z_{seq}_{rig}_{filter}_g{gain}_o{offset}_bin{bin}",
        },
        "bias": {
            "path": "calibration/bias/{camera}/g{gain}_o{offset}_bin{bin}/{night}",
            "filename": "Bias_{utc}Z_{seq}_{camera}_g{gain}_o{offset}_bin{bin}",
        },
        "flatdark": {
            "path": "calibration/flatdark/{camera}/{exp}s_g{gain}_o{offset}_bin{bin}/{night}",
            "filename": "FlatDark_{utc}Z_{seq}_{camera}_{exp}s_g{gain}_o{offset}_bin{bin}",
        },
    },
    "buckets": {
        "review": "review/{source}",
        "quarantine": "quarantine/{reason}",
        "process": "process",
    },
    "resolve": {
        "night": {"mode": "noon-to-noon", "utc_offset_hours": -7},
        "object": {"messier_alias": True,
                   "compact_catalogs": ["NGC", "IC", "M", "UGC", "PGC", "AGC", "ARP", "MRK", "HCG"]},
        "filter": {"default": "CLEAR", "write_header": True},
        "gain_keywords": ["GAIN", "GAINRAW"],
        "sequence": {"width": 4},
    },
    "rigs": [],
    "camera_aliases": {},
    # Optional: source rig folder names from StarBase (the source of truth) when
    # it is reachable. Off by default; enable in nwingest.yaml. When on and a rig
    # matches, StarBase's v_rig_resolve view wins over the local `rigs` table.
    "starbase": {
        "enabled": False,
        "refresh_s": 300,
        "db": {
            "host": "127.0.0.1", "port": 3306, "user": "nightwatcher",
            "name": "starbase", "view": "v_rig_resolve",
            "password_env": "NWDB_PASSWORD",
        },
    },
    # A convenience subset of NGC -> Messier. Extend via config.messier.
    "messier": {
        "NGC224": "M31", "NGC598": "M33", "NGC1952": "M1", "NGC5194": "M51",
        "NGC6720": "M57", "NGC4594": "M104", "NGC6853": "M27", "NGC7654": "M52",
        "NGC1976": "M42", "NGC3031": "M81", "NGC5236": "M83", "NGC205": "M110",
    },
    "exclude_lights": {
        "path_tokens": ["@focus", "autofocus", "closed loop slew", "/slew/",
                        "/preview/", "failed", "platesolve"],
        "min_exposure_s": 30,
    },
    "nwdb": {"host": "127.0.0.1", "port": 3306, "name": "nightwatcher",
             "user": "nightwatcher", "password_env": "NWDB_PASSWORD"},
    "sqm": {"enabled": False, "keyword": "SQM", "provenance": True,
            "max_gap_minutes": 15, "sites": []},
    "hooks": [],
    "extension": {"register": False, "name": "ingest", "label": "Ingest",
                  "heartbeat_seconds": 20},
    "events": {"enabled": True},
    "logging": {"level": "info", "file": None},
}


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
def deep_merge(base, over):
    """Recursively merge dict `over` into dict `base` (in place)."""
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config(path):
    cfg = deepcopy(DEFAULTS)
    if path and os.path.exists(path):
        with open(path) as fh:
            deep_merge(cfg, yaml.safe_load(fh) or {})
    elif path:
        log(f"note: config {path} not found; using built-in defaults")
    return cfg


# ---------------------------------------------------------------------------
# Small logging helpers. One line per event, `[timestamp] message`, to stdout
# (the journal under systemd) and optionally to logging.file. logging.level
# filters; warnings keep the literal "warn:" prefix so existing greps still work.
# ---------------------------------------------------------------------------
_LOG_LEVELS = {"debug": 10, "info": 20, "warning": 30, "warn": 30, "error": 40}
_log_state = {"level": 20, "fh": None}


def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def configure_logging(cfg):
    lc = cfg.get("logging", {}) or {}
    _log_state["level"] = _LOG_LEVELS.get(str(lc.get("level", "info")).lower(), 20)
    path = lc.get("file")
    if path:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            _log_state["fh"] = open(path, "a")
        except OSError as e:
            print(f"[{_now_str()}]     warn: cannot open log file {path}: {e}", flush=True)


def log(msg, level="info"):
    if _LOG_LEVELS.get(level, 20) < _log_state["level"]:
        return
    line = f"[{_now_str()}] {msg}"
    print(line, flush=True)
    fh = _log_state["fh"]
    if fh is not None:
        try:
            fh.write(line + "\n")
            fh.flush()
        except OSError:
            pass


def warn(msg):
    log(f"    warn: {msg}", "warning")


# ---------------------------------------------------------------------------
# Header helpers
# ---------------------------------------------------------------------------
def _need_astropy():
    try:
        from astropy.io import fits
        return fits
    except ImportError:
        sys.exit("nwingest: astropy is required  ->  pip install astropy")


def hget(h, *keys, default=None):
    """First present, non-empty header value among keys."""
    for k in keys:
        if k in h:
            val = h[k]
            if val not in (None, ""):
                return val
    return default


def fnum(h, *keys):
    val = hget(h, *keys)
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def parse_dateobs(h):
    """DATE-OBS is UTC by convention. Return a naive UTC datetime or None."""
    v = hget(h, "DATE-OBS", "DATE_OBS")
    if not v:
        return None
    s = str(v).strip().rstrip("Z")
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)   # trim sub-microsecond digits (ASI writes 7)
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def night_of(dt, offset_hours):
    """Local noon-to-noon observing night as an ISO date string."""
    local = dt + timedelta(hours=offset_hours)
    return (local - timedelta(hours=12)).date().isoformat()


def _ext(path):
    e = os.path.splitext(path)[1].lower()
    return e if e else ".fits"


def fmt_int(x):
    return "na" if x is None else str(int(round(x)))


def fmt_exp(x):
    if x is None:
        return "na"
    x = float(x)
    return str(int(x)) if x.is_integer() else ("%g" % x)


# ---------------------------------------------------------------------------
# Structural check: is this a whole, single-image FITS file? Pure byte
# inspection, no astropy, so it is cheap enough to run on every file before
# anything is moved. It exists because a copy taken while the capture device
# was rewriting the frame (the 2026-10-03 NGC7720 tear, see rca/) passes a
# header read but carries pixel rows from two layouts plus a stray trailing
# block; astropy's update mode then fails on the trailing block, and a header
# patch would only hide the broken image.
# ---------------------------------------------------------------------------
FITS_BLOCK = 2880
_HDR_SCAN_BLOCKS = 128          # give up looking for END after 360 KB of header
_WCS_KEYS = ("CTYPE1", "CTYPE2", "CRVAL1", "CRVAL2", "CRPIX1", "CRPIX2")


def fits_structure(path):
    """Return (status, info) for the FITS file at `path`.

    status:
      ok          END card found and the size is header + padded data, or a
                  real extension (XTENSION) follows the primary data
      no-end      no END card within the first _HDR_SCAN_BLOCKS blocks
      bad-header  BITPIX/NAXIS cards missing or unparsable
      short       smaller than header + padded data: still being written, or cut
      trailing    bytes after the padded data that are neither an extension
                  nor blank padding: a torn copy
    info: size, hdr_bytes, data_bytes, expected, extra (where known).
    """
    size = os.path.getsize(path)
    info = {"size": size, "hdr_bytes": None}
    cards = {}
    hdr_bytes = None
    with open(path, "rb") as fh:
        for blk in range(_HDR_SCAN_BLOCKS):
            block = fh.read(FITS_BLOCK)
            if len(block) < FITS_BLOCK:
                break
            for i in range(0, FITS_BLOCK, 80):
                card = block[i:i + 80]
                if card[:8] == b"END     ":
                    hdr_bytes = (blk + 1) * FITS_BLOCK
                    break
                if card[8:10] == b"= ":
                    key = card[:8].strip().decode("ascii", "replace")
                    cards[key] = card[10:].split(b"/", 1)[0].strip().decode("ascii", "replace")
            if hdr_bytes is not None:
                break
        if hdr_bytes is None:
            return ("no-end", info)
        info["hdr_bytes"] = hdr_bytes
        try:
            bitpix = abs(int(cards["BITPIX"]))
            naxis = int(cards["NAXIS"])
            npix = 1
            for ax in range(1, naxis + 1):
                npix *= int(cards[f"NAXIS{ax}"])
            data_bytes = (bitpix // 8) * npix if naxis > 0 else 0
        except (KeyError, ValueError):
            return ("bad-header", info)
        padded = ((data_bytes + FITS_BLOCK - 1) // FITS_BLOCK) * FITS_BLOCK
        expected = hdr_bytes + padded
        info.update(data_bytes=data_bytes, expected=expected, extra=size - expected)
        if size < expected:
            return ("short", info)
        if size == expected:
            return ("ok", info)
        fh.seek(expected)
        tail = fh.read(64 * 1024)
        if tail.startswith(b"XTENSION"):
            return ("ok", info)             # multi-HDU file; astropy owns those
        if not tail.strip(b"\0") or not tail.strip(b" "):
            return ("ok", info)             # blank padding after the data: harmless
        return ("trailing", info)


def describe_structure(status, info):
    if status == "short":
        return (f"short file: {info['size']} of {info['expected']} bytes "
                f"({info['expected'] - info['size']} missing)")
    if status == "trailing":
        return (f"torn copy: {info['extra']} bytes after the image data; "
                "re-copy from the capture device")
    if status == "no-end":
        return "no END card in the header"
    if status == "bad-header":
        return "BITPIX/NAXIS cards missing or unreadable"
    return "ok"


def has_wcs(h):
    return all(k in h for k in _WCS_KEYS)


# ---------------------------------------------------------------------------
# Resolvers: header -> template variables
# ---------------------------------------------------------------------------
def frame_type(h):
    t = str(hget(h, "IMAGETYP", "FRAME", "IMGTYPE", default="") or "").lower()
    if "master" in t or "integration" in t or "stack" in t:
        return "process"
    if "flat" in t and "dark" in t:      # "flatdark" / "dark flat" / "DARKFLAT"
        return "flatdark"
    if "bias" in t or "zero" in t:
        return "bias"
    if "dark" in t:
        return "dark"
    if "flat" in t:
        return "flat"
    return "light"                        # includes empty / "Light" / "Light Frame"


def norm_camera(h, cfg):
    cam = str(hget(h, "INSTRUME", "CAMERA", default="") or "")
    for k, v in cfg["camera_aliases"].items():
        if k.lower() in cam.lower():
            return v
    # Base model only ("ZWO ASI6200MC Pro" -> "ASI6200"); the archive keys
    # calibration on the model, and color/mono is detected via BAYERPAT.
    m = re.search(r"ASI\s?\d{3,4}", cam)
    if m:
        return m.group(0).replace(" ", "")
    return re.sub(r"[^\w+-]", "", cam) or "UNKNOWN"


class _StarbaseRigs:
    """Cached reader of StarBase's `v_rig_resolve` view: (camera model, focal
    length) -> rig name.

    Optional integration. When cfg['starbase']['enabled'] and StarBase is
    reachable, its rig definitions are the source of truth for the equipment
    folder name. Every failure (StarBase absent, no grant, connection or query
    error) is swallowed so the caller falls back to the local `rigs` table and
    then the bare camera model. The view is read at most once per `refresh_s`;
    a stale cache is preferred over a hard failure.
    """

    def __init__(self):
        self._rows = None      # list[(camera_model, focal_min, focal_max, rig_name)]
        self._loaded = 0.0     # time.monotonic() of the last successful load
        self._failed = None    # time.monotonic() of the last failed load, if any

    def rig(self, camera, focal, cfg):
        sb = cfg.get("starbase", {}) or {}
        if not sb.get("enabled") or focal is None or not camera:
            return None
        rows = self._rows_cached(sb)
        if not rows:
            return None
        # Exact canonical-model match + focal within the rig's window, mirroring
        # StarBase's own (camera_id, focal range) resolution.
        for cam, lo, hi, name in rows:
            if cam and cam.lower() == camera.lower() and lo <= focal <= hi:
                return name
        return None

    def _rows_cached(self, sb):
        ttl = float(sb.get("refresh_s", 300))
        now = time.monotonic()
        if self._rows is not None and (now - self._loaded) < ttl:
            return self._rows
        if self._failed is not None and (now - self._failed) < ttl:
            return self._rows               # already warned; retry after the window
        try:
            self._rows = self._load(sb.get("db", {}))
            self._loaded = now
            self._failed = None
        except Exception as e:
            # Keep any prior cache; if we never loaded, the caller falls back.
            self._failed = now
            warn(f"StarBase rig lookup unavailable (retry in {int(ttl)}s): {e}")
        return self._rows

    @staticmethod
    def _load(db):
        import pymysql
        pw = os.environ.get(db.get("password_env", "NWDB_PASSWORD"), "")
        conn = pymysql.connect(
            host=db.get("host", "127.0.0.1"), port=int(db.get("port", 3306)),
            user=db.get("user", "nightwatcher"), password=pw,
            database=db.get("name", "starbase"), autocommit=True,
            connect_timeout=int(db.get("connect_timeout", 5)))
        try:
            view = db.get("view", "v_rig_resolve")
            with conn.cursor() as cur:
                cur.execute("SELECT camera_model, focal_min_mm, focal_max_mm, "
                            f"rig_name FROM {view}")
                return [(r[0], float(r[1]), float(r[2]), r[3])
                        for r in cur.fetchall()]
        finally:
            conn.close()


_STARBASE_RIGS = _StarbaseRigs()


def rig_of(camera, focal, cfg):
    # StarBase is the source of truth for rig names when the integration is on
    # and reachable (cfg['starbase']); otherwise fall back to the local `rigs`
    # table, then to the bare camera model.
    name = _STARBASE_RIGS.rig(camera, focal, cfg)
    if name:
        return name
    for r in cfg["rigs"]:
        want = r.get("camera")
        if want and want.lower() not in camera.lower():
            continue
        lo, hi = r["focal_mm"]
        if focal is not None and lo <= focal <= hi:
            return r["name"]
    return camera or "UNKNOWN"


def _compact_catalog(obj, cats):
    """Normalize a catalog designation to compact directory form, e.g.
    'NGC 6992' -> 'NGC6992', 'M 31' -> 'M31'. Only recognized catalog prefixes
    (cats, upper-cased) are compacted; free-form target names are returned
    unchanged. This stops a capture app that writes 'NGC 6992' from forking a
    separate folder from one that writes 'NGC6992'."""
    m = re.match(r"^([A-Za-z]+)\s+(\d+[A-Za-z]?)$", obj.strip())
    if m and m.group(1).upper() in cats:
        return m.group(1).upper() + m.group(2)
    return obj


def norm_object(h, cfg):
    obj = str(hget(h, "OBJECT", default="") or "").strip()
    if not obj:
        return ""
    ocfg = cfg["resolve"]["object"]
    obj = _compact_catalog(obj, {c.upper() for c in ocfg.get("compact_catalogs", [])})
    if ocfg.get("messier_alias"):
        key = obj.upper().replace(" ", "")
        if key in cfg["messier"]:
            return cfg["messier"][key]
    return re.sub(r"[^\w+-]", "_", obj)


def _parse_coord(val):
    """Decimal degrees from a numeric or sexagesimal FITS coordinate value."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    try:
        return float(s)
    except ValueError:
        pass
    m = re.match(r"([+-]?)\s*(\d+)[\s:]+(\d+)[\s:]+([\d.]+)", s)   # '+32 18 11.30'
    if not m:
        return None
    sign = -1.0 if m.group(1) == "-" else 1.0
    return sign * (float(m.group(2)) + float(m.group(3)) / 60 + float(m.group(4)) / 3600)


def _frame_coords(h):
    """(lat, lon) in decimal degrees, preferring the unambiguous OBSGEO cards.

    TheSkyX writes SITELONG sexagesimal with a positive sign for a west longitude
    (a trap), but also writes OBSGEO-L as a correct signed decimal, so OBSGEO wins.
    """
    lat = _parse_coord(hget(h, "OBSGEO-B"))
    lon = _parse_coord(hget(h, "OBSGEO-L"))
    if lat is None:
        lat = _parse_coord(hget(h, "SITELAT", "LAT-OBS", "OBJCTLAT"))
    if lon is None:
        lon = _parse_coord(hget(h, "SITELONG", "LONG-OBS", "OBJCTLONG"))
    return lat, lon


def _site_of(h, cfg):
    sites = cfg.get("sqm", {}).get("sites", [])
    lat, lon = _frame_coords(h)
    if lat is not None and lon is not None:
        for s in sites:
            tol = s.get("tol_deg", 0.05)
            if abs(lat - s["lat"]) <= tol and abs(lon - s["lon"]) <= tol:
                return s["name"]
        return ""
    # no usable coords: fall back to an observatory-name card (NINA writes OBSERVAT)
    name = str(hget(h, "OBSERVAT", "SITENAME", default="") or "").strip()
    for s in sites:
        if name and name.lower() == s["name"].lower():
            return s["name"]
    return ""


def resolve(path, h, cfg):
    dt = parse_dateobs(h)
    camera = norm_camera(h, cfg)
    focal = fnum(h, "FOCALLEN")
    exp = fnum(h, "EXPTIME", "EXPOSURE")
    off = cfg["resolve"]["night"]["utc_offset_hours"]

    raw_filter = str(hget(h, "FILTER", default="") or "").strip()

    return {
        "type": frame_type(h),
        "object": norm_object(h, cfg),
        "camera": camera,
        "rig": rig_of(camera, focal, cfg),
        "filter": raw_filter or cfg["resolve"]["filter"]["default"],
        "night": night_of(dt, off) if dt else "",
        "utc": dt.strftime("%Y-%m-%dT%H%M%S") if dt else "",
        "exp": fmt_exp(exp),
        "gain": fmt_int(fnum(h, *cfg["resolve"]["gain_keywords"])),
        "offset": fmt_int(fnum(h, "OFFSET", "BLKLEVEL")),
        "bin": fmt_int(fnum(h, "XBINNING", "BINNING")),
        "temp": fmt_int(fnum(h, "CCD-TEMP", "SET-TEMP")),
        "site": _site_of(h, cfg),
        "ext": _ext(path),
        "_exp_num": exp,
        "_no_date": dt is None,
        "_filter_defaulted": not raw_filter,
        "_dt_utc": dt,
    }


def _aux_source(low):
    for key in ("@focus", "autofocus", "focus", "slew", "preview", "live",
                "failed", "platesolve"):
        if key in low:
            return key.lstrip("@")
    return "aux"


def classify(path, v, cfg):
    """Return (kind, extra). kind is a route key, or review/quarantine/process."""
    if v.get("_no_date"):
        return ("quarantine", "no-dateobs")
    t = v["type"]
    if t == "process":
        return ("process", None)
    low = path.lower()
    if t == "light":
        for tok in cfg["exclude_lights"]["path_tokens"]:
            if tok.lower() in low:
                return ("review", _aux_source(low))
        exp = v["_exp_num"]
        if exp is not None and exp < cfg["exclude_lights"]["min_exposure_s"]:
            return ("review", "short")
        if not v["object"]:
            return ("quarantine", "no-object")
        return ("light", None)
    if t in ("dark", "flat", "bias", "flatdark"):
        return (t, None)
    return ("quarantine", "unknown-type")


# ---------------------------------------------------------------------------
# Naming: template variables -> destination path and filename
# ---------------------------------------------------------------------------
def _clean(seg):
    seg = re.sub(r"_{2,}", "_", seg)      # collapse gaps left by empty tokens
    return seg.strip("_ ")


def _render_segments(template, v):
    root = None
    parts = []
    for seg in template.split("/"):
        s = _clean(seg.format_map(defaultdict(str, v)))
        if s:
            parts.append(s)
    return parts


def _folder_for(kind, extra, v, cfg):
    if kind in cfg["routes"]:
        tmpl = cfg["routes"][kind]["path"]
        vv = v
    else:
        tmpl = cfg["buckets"].get(kind, kind)
        vv = {**v, "source": extra or "misc", "reason": extra or "misc"}
    return os.path.join(cfg["destination"]["root"], *_render_segments(tmpl, vv))


def _filename_for(kind, v, src, cfg):
    if kind in cfg["routes"]:
        name = _clean(cfg["routes"][kind]["filename"].format_map(defaultdict(str, v)))
    else:
        name = os.path.splitext(os.path.basename(src))[0]   # keep original for buckets
    ext = cfg["destination"]["normalize_ext"] or v["ext"]
    return name + ext


def next_seq(folder):
    """Highest existing per-folder sequence + 1 (assumes ...Z_NNNN_ layout)."""
    if not os.path.isdir(folder):
        return 1
    mx = 0
    for name in os.listdir(folder):
        m = re.search(r"Z_(\d{3,})_", name)
        if m:
            mx = max(mx, int(m.group(1)))
    return mx + 1


def place_live(src, v, kind, extra, cfg):
    folder = _folder_for(kind, extra, v, cfg)
    if kind in cfg["routes"]:
        width = cfg["resolve"]["sequence"]["width"]
        v = {**v, "seq": str(next_seq(folder)).zfill(width)}
    return os.path.join(folder, _filename_for(kind, v, src, cfg))


# ---------------------------------------------------------------------------
# File movement (atomic rename; cross-device copy+verify fallback)
# ---------------------------------------------------------------------------
def move(src, dst, on_conflict="sequence"):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        if on_conflict == "overwrite":
            os.remove(dst)
        elif on_conflict == "sequence":
            dst = _dedupe(dst)
        else:
            return ("skip", dst)
    try:
        os.rename(src, dst)
        return ("rename", dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
    tmp = dst + ".part"
    shutil.copy2(src, tmp)
    if os.path.getsize(tmp) != os.path.getsize(src):
        os.remove(tmp)
        raise IOError(f"size mismatch copying {src}")
    os.replace(tmp, dst)
    os.remove(src)
    return ("copy", dst)


def _dedupe(dst):
    base, ext = os.path.splitext(dst)
    i = 2
    while os.path.exists(f"{base}~{i}{ext}"):
        i += 1
    return f"{base}~{i}{ext}"


# ---------------------------------------------------------------------------
# nwdb access: SQM lookup (read) + ingest log and extension registry (write)
# ---------------------------------------------------------------------------
INGEST_LOG_DDL = """
CREATE TABLE IF NOT EXISTS ingest_log (
    id         BIGINT       NOT NULL AUTO_INCREMENT,
    ts_utc     DATETIME     NOT NULL,
    frame_utc  DATETIME     NULL,
    kind       VARCHAR(16)  NOT NULL,
    `object`   VARCHAR(64)  NULL,
    rig        VARCHAR(64)  NULL,
    `filter`   VARCHAR(32)  NULL,
    sqm        DECIMAL(6,3) NULL,
    dest       VARCHAR(512) NOT NULL,
    status     VARCHAR(16)  NOT NULL,
    detail     VARCHAR(255) NULL,
    created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_ingest_ts (ts_utc)
) ENGINE=InnoDB
"""

EXTENSIONS_DDL = """
CREATE TABLE IF NOT EXISTS extensions (
    name           VARCHAR(32)  NOT NULL,
    label          VARCHAR(64)  NOT NULL,
    version        VARCHAR(32)  NULL,
    data_table     VARCHAR(64)  NULL,
    host           VARCHAR(128) NULL,
    pid            INT          NULL,
    status         VARCHAR(16)  NOT NULL DEFAULT 'active',
    last_heartbeat DATETIME     NOT NULL,
    started_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (name)
) ENGINE=InnoDB
"""


class Nwdb:
    """Lazy, reused connection to the NightWatcher database (PyMySQL)."""

    def __init__(self, cfg):
        self.c = cfg.get("nwdb", {})
        self.conn = None

    def _connect(self):
        import pymysql
        pw = os.environ.get(self.c.get("password_env", "NWDB_PASSWORD"), "")
        self.conn = pymysql.connect(
            host=self.c.get("host", "127.0.0.1"), port=int(self.c.get("port", 3306)),
            user=self.c.get("user", "nightwatcher"), password=pw,
            database=self.c.get("name", "nightwatcher"), autocommit=True,
            connect_timeout=5)

    def nearest_reading(self, sensor, dt, gap_min):
        """(ts_utc, mag) for the reading nearest dt within +/- gap_min, else None.

        Raises after a second failed attempt, so the caller can tell "no
        reading" from "could not ask"; the two are reported differently."""
        lo, hi = dt - timedelta(minutes=gap_min), dt + timedelta(minutes=gap_min)
        for attempt in (1, 2):
            try:
                if self.conn is None:
                    self._connect()
                with self.conn.cursor() as cur:
                    cur.execute(
                        "SELECT ts_utc, mag_arcsec2 FROM readings "
                        "WHERE sensor_id=%s AND ts_utc BETWEEN %s AND %s "
                        "AND quality<>'saturated' "
                        "ORDER BY ABS(TIMESTAMPDIFF(SECOND, ts_utc, %s)) LIMIT 1",
                        (sensor, lo, hi, dt))
                    return cur.fetchone()
            except Exception:
                self.conn = None
                if attempt == 2:
                    raise
        return None

    def _exec(self, sql, params=()):
        for attempt in (1, 2):
            try:
                if self.conn is None:
                    self._connect()
                with self.conn.cursor() as cur:
                    cur.execute(sql, params)
                return True
            except Exception as e:
                self.conn = None
                if attempt == 2:
                    warn(f"nwdb write failed: {e}")
        return False

    def ensure_schema(self):
        self._exec(INGEST_LOG_DDL)
        self._exec(EXTENSIONS_DDL)

    def log_ingest(self, r):
        self._exec(
            "INSERT INTO ingest_log "
            "(ts_utc, frame_utc, kind, `object`, rig, `filter`, sqm, dest, status, detail) "
            "VALUES (UTC_TIMESTAMP(), %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (r.get("frame_utc"), r.get("kind"), r.get("object"), r.get("rig"),
             r.get("filter"), r.get("sqm"), r.get("dest"), r.get("status"),
             r.get("detail")))

    def log_event(self, source, level, event, detail, device_id=""):
        """Append a row to the daemon's shared `events` table (shows in the Events tab)."""
        self._exec(
            "INSERT INTO events (ts_utc, device_id, source, level, event, detail) "
            "VALUES (UTC_TIMESTAMP(), %s, %s, %s, %s, %s)",
            (device_id or None, source, level, event, detail or None))

    def register(self, ext):
        self._exec(
            "INSERT INTO extensions "
            "(name, label, version, data_table, host, pid, last_heartbeat, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, UTC_TIMESTAMP(), 'active') "
            "ON DUPLICATE KEY UPDATE label=VALUES(label), version=VALUES(version), "
            "data_table=VALUES(data_table), host=VALUES(host), pid=VALUES(pid), "
            "last_heartbeat=UTC_TIMESTAMP(), status='active'",
            (ext.get("name"), ext.get("label"), ext.get("version"),
             ext.get("data_table"), ext.get("host"), ext.get("pid")))

    def heartbeat(self, name):
        self._exec("UPDATE extensions SET last_heartbeat=UTC_TIMESTAMP() WHERE name=%s", (name,))

    def deregister(self, name):
        self._exec("UPDATE extensions SET status='stopped' WHERE name=%s", (name,))

    def close(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None


def lookup_sqm(v, cfg, db):
    """(cards, reason) for the SQM reading nearest this light frame.

    cards is a dict of header cards, or None when there is nothing to stamp;
    reason then says why in a few words (it is shown on the transfer line and
    counted in the run summary)."""
    site, dt = v.get("site"), v.get("_dt_utc")
    if db is None:
        return None, "no database"
    if dt is None:
        return None, "no DATE-OBS"
    if not site:
        return None, "frame is not at a configured site"
    scfg = next((s for s in cfg["sqm"].get("sites", []) if s["name"] == site), None)
    if not scfg or not scfg.get("sensor"):
        return None, f"site {site} has no sensor configured"
    gap = cfg["sqm"].get("max_gap_minutes", 15)
    row = db.nearest_reading(scfg["sensor"], dt, gap)
    if not row:
        return None, f"no {scfg['sensor']} reading within {gap} min"
    ts, mag = row[0], float(row[1])
    kw = cfg["sqm"].get("keyword", "SQM")
    cards = {kw: (round(mag, 3), "sky brightness mag/arcsec^2 (nwingest)")}
    if cfg["sqm"].get("provenance", True):
        cards["SQMSRC"] = (scfg["sensor"], "SQM sensor id")
        cards["SQMTIME"] = (ts.strftime("%Y-%m-%dT%H:%M:%S"), "SQM reading time (UTC)")
        cards["SQMDT"] = (int(abs((dt - ts).total_seconds())), "sec between reading and DATE-OBS")
    return cards, None


# ---------------------------------------------------------------------------
# Header finalization: FILTER default + SQM stamp. The two are decided
# independently (a failed SQM lookup never costs the frame its FILTER), then
# written in one pass by nwingest's own writer rather than astropy's update
# mode. Update mode walks every HDU before flushing, which fails on a torn
# copy's trailing block, and its resize path ends with a chmod that raises
# EPERM over NFS after the new file is already in place (the "Operation not
# permitted" false failures on the NINA rig).
# ---------------------------------------------------------------------------
def plan_header_edits(kind, v, cfg, db):
    """Cards nwingest adds to a filed frame, and the gaps it could not fill.

    Returns (edits, gaps): edits maps keyword -> (value, comment); gaps is a
    list of (keyword, reason) for cards that were wanted but unavailable."""
    edits, gaps = {}, []
    if (kind in ("light", "flat") and v.get("_filter_defaulted")
            and cfg["resolve"]["filter"].get("write_header", True)):
        edits["FILTER"] = (v["filter"], "filled by nwingest (header had none)")
    if kind == "light" and cfg["sqm"].get("enabled"):
        kw = cfg["sqm"].get("keyword", "SQM")
        try:
            cards, why = lookup_sqm(v, cfg, db)
        except Exception as e:
            cards, why = None, f"lookup failed: {e}"
        if cards:
            edits.update(cards)
        else:
            gaps.append((kw, why or "unavailable"))
    return edits, gaps


def write_header_cards(path, edits, fits):
    """Add or replace primary-header cards in a single-image FITS file.

    In place when the new header fits the blocks the file already reserves for
    it (the END card is kept in the last reserved block, so the data offset
    never moves). When the header outgrows that space the file is rewritten
    through a temp file in the same directory and os.replace, with mode and
    ownership copied on a best-effort basis. Files with extensions fall back
    to astropy's update mode. Raises on failure; a failed grow leaves the
    original untouched."""
    if not edits:
        return
    status, info = fits_structure(path)
    if status != "ok":
        raise OSError(f"refusing to write header: {describe_structure(status, info)}")
    hdr = fits.getheader(path)
    for k, (val, comment) in edits.items():
        hdr[k] = (val, comment)
    reserved = info["hdr_bytes"]
    body = hdr.tostring(endcard=False, padding=False).encode("ascii")
    end_card = b"END" + b" " * 77
    if len(body) + len(end_card) <= reserved:
        new = body + b" " * (reserved - len(end_card) - len(body)) + end_card
        with open(path, "r+b") as fh:
            fh.write(new)
            fh.flush()
            os.fsync(fh.fileno())
        return
    if info["size"] != info["expected"]:
        # extensions present: let astropy rewrite the whole thing
        with fits.open(path, mode="update") as hdul:
            for k, (val, comment) in edits.items():
                hdul[0].header[k] = (val, comment)
        return
    new = hdr.tostring().encode("ascii")            # padded to a block multiple
    tmp = f"{path}.nwingest-tmp"
    try:
        st = os.stat(path)
        with open(path, "rb") as src, open(tmp, "wb") as dst:
            dst.write(new)
            src.seek(reserved)
            shutil.copyfileobj(src, dst, 1 << 20)
            dst.flush()
            os.fsync(dst.fileno())
        if os.path.getsize(tmp) != len(new) + (info["expected"] - reserved):
            raise OSError("size mismatch after header rewrite")
        try:
            os.chmod(tmp, st.st_mode & 0o7777)
            os.chown(tmp, st.st_uid, st.st_gid)
        except OSError:
            pass                                    # NFS/ACL may refuse; not fatal
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def finalize_header(path, kind, v, cfg, fits, db):
    """Write nwingest's cards into a filed frame.

    Returns (written, problems): written is the dict of cards now in the
    header; problems is a list of (keyword-or-step, reason) strings for the
    caller to log, record and count. Never raises."""
    edits, problems = plan_header_edits(kind, v, cfg, db)
    if not edits:
        return {}, problems
    for attempt in (1, 2):
        try:
            write_header_cards(path, edits, fits)
            return edits, problems
        except Exception as e:
            if attempt == 1:
                time.sleep(1.0)                     # one retry for a transient NFS hiccup
                continue
            problems.append(("header write", f"{e} (lost: {', '.join(edits)})"))
            warn(f"header write failed on {os.path.basename(path)}: {e}")
    return {}, problems


# ---------------------------------------------------------------------------
# External hooks: run configured programs on each filed frame (requirement 5)
# ---------------------------------------------------------------------------
_bg_hooks = []      # (Popen, name) for background hooks, reaped opportunistically


def _reap_hooks():
    global _bg_hooks
    _bg_hooks = [(p, n) for p, n in _bg_hooks if p.poll() is None]


def _hook_matches(when, v):
    return all(str(v.get(k)) == str(val) for k, val in (when or {}).items())


def _run_one_hook(hk, ctx):
    name = hk.get("name", "hook")
    use_shell = bool(hk.get("shell"))
    # argv by default: split the template, then fill each token, so a substituted
    # value with spaces stays one argument and the shell never sees it.
    cmd = hk["run"].format_map(ctx) if use_shell \
        else [tok.format_map(ctx) for tok in shlex.split(hk["run"])]
    if hk.get("background"):
        p = subprocess.Popen(cmd, shell=use_shell, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        _bg_hooks.append((p, name))
        log(f"    hook '{name}' started in background (pid {p.pid})")
        return
    r = subprocess.run(cmd, shell=use_shell, capture_output=True, text=True,
                       timeout=hk.get("timeout_s", 120))
    if r.returncode == 0:
        log(f"    hook '{name}' ok")
    else:
        err = (r.stderr or r.stdout or "").strip().replace("\n", " ")[:200]
        warn(f"hook '{name}' exited {r.returncode}: {err}")


def run_hooks(dest, v, cfg):
    """Run each enabled, matching hook on a filed frame. A hook never breaks
    ingestion: failures, timeouts, and missing programs are logged and skipped."""
    hooks = cfg.get("hooks", [])
    if not hooks:
        return
    _reap_hooks()
    ctx = defaultdict(str, {k: val for k, val in v.items() if not k.startswith("_")})
    ctx["dest"] = dest
    for hk in hooks:
        if not hk.get("enabled") or not _hook_matches(hk.get("when"), v):
            continue
        try:
            _run_one_hook(hk, ctx)
        except subprocess.TimeoutExpired:
            warn(f"hook '{hk.get('name', 'hook')}' timed out")
        except Exception as e:
            warn(f"hook '{hk.get('name', 'hook')}' error: {e}")


# ---------------------------------------------------------------------------
# Ingest log + extension registry (groundwork for the web UI Ingest tab)
# ---------------------------------------------------------------------------
def record(entry, db):
    """Write one ingest_log row (no-op without a DB connection)."""
    if db is not None:
        db.log_ingest(entry)


def make_db(cfg):
    """A DB handle if any feature needs nwdb (SQM stamp or extension registry)."""
    if cfg["sqm"].get("enabled") or cfg.get("extension", {}).get("register"):
        return Nwdb(cfg)
    return None


def register_extension(db, cfg):
    ext = cfg.get("extension", {})
    if db is None or not ext.get("register"):
        return
    db.register({"name": ext.get("name", "ingest"), "label": ext.get("label", "Ingest"),
                 "version": __version__, "data_table": "ingest_log",
                 "host": socket.gethostname(), "pid": os.getpid()})
    log(f"registered extension '{ext.get('name', 'ingest')}' (Ingest tab active while running)")


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------
def _matches(name, patterns):
    low = name.lower()
    return any(fnmatch.fnmatch(low, p.lower()) for p in patterns)


def scan(indir, cfg, require_stable=True):
    w = cfg["watch"]
    now = time.time()
    found = []
    for dirpath, _dirs, names in os.walk(indir):
        if any(g in dirpath for g in w["ignore"]):
            continue
        for n in names:
            if any(g in n for g in w["ignore"]):
                continue
            if not _matches(n, w["patterns"]):
                continue
            fp = os.path.join(dirpath, n)
            try:
                st = os.stat(fp)
            except FileNotFoundError:
                continue
            if require_stable and (now - st.st_mtime) < w["stable_seconds"]:
                continue                   # still being written or copied
            found.append(fp)
    return sorted(found)


def analyze(path, cfg, fits):
    """Classify one file: (v, kind, extra). kind is a route key, one of
    review / quarantine / process, or 'defer' (leave the file where it is this
    cycle). The structural check runs before the header read so a torn or
    unfinished copy is never moved into the archive on the strength of a
    header that happens to parse."""
    base = {"ext": _ext(path), "_no_date": True}
    try:
        status, info = fits_structure(path)
    except OSError as e:
        base["_structure_note"] = str(e)
        return (base, "defer", "unreadable")      # vanished mid-scan; next cycle decides
    base["_structure"] = status
    base["_structure_note"] = describe_structure(status, info)
    if status in ("no-end", "bad-header"):
        return (base, "quarantine", "unreadable")
    if status == "trailing":
        return (base, "quarantine", "torn")
    if status == "short":
        try:
            age = time.time() - os.stat(path).st_mtime
        except OSError:
            age = 0.0
        if age < cfg["watch"].get("truncated_after_s", 600):
            return (base, "defer", "short")
        return (base, "quarantine", "truncated")
    try:
        h = fits.getheader(path)
    except Exception as e:
        base["_structure_note"] = f"header unreadable: {e}"
        return (base, "quarantine", "unreadable")
    v = resolve(path, h, cfg)
    v["_structure"] = status
    kind, extra = classify(path, v, cfg)
    return (v, kind, extra)


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------
def do_plan(cfg, indir):
    """Read-only: show what would be done, with batch per-folder sequencing."""
    fits = _need_astropy()
    files = scan(indir, cfg, require_stable=False)
    if not files:
        log(f"plan: no FITS under {indir}")
        return 0

    groups = defaultdict(list)             # folder -> [(src, v, kind)]
    passthrough = []                       # (src, dest, kind)
    counts = defaultdict(int)
    for f in files:
        v, kind, extra = analyze(f, cfg, fits)
        counts[kind] += 1
        if kind == "defer":
            passthrough.append((f, f"(left in place: {v.get('_structure_note')})", kind))
            continue
        if kind in cfg["routes"]:
            groups[_folder_for(kind, extra, v, cfg)].append((f, v, kind))
        else:
            folder = _folder_for(kind, extra, v, cfg)
            passthrough.append((f, os.path.join(folder, _filename_for(kind, v, f, cfg)), kind))

    plans = []
    width = cfg["resolve"]["sequence"]["width"]
    for folder, lst in groups.items():
        lst.sort(key=lambda x: x[1]["utc"])
        for i, (src, v, kind) in enumerate(lst, 1):
            vv = {**v, "seq": str(i).zfill(width)}
            plans.append((src, os.path.join(folder, _filename_for(kind, vv, src, cfg)), kind))
    plans.extend(passthrough)

    for src, dest, kind in sorted(plans, key=lambda x: x[2]):
        rel = dest if kind == "defer" else os.path.relpath(dest, cfg["destination"]["root"])
        print(f"  {kind:10} {os.path.basename(src)}\n             -> {rel}")
    log("plan summary: " + ", ".join(f"{k}={counts[k]}" for k in sorted(counts)))
    return 0


# ---------------------------------------------------------------------------
# Run statistics: what happened to every file, summarized per cycle and per run
# ---------------------------------------------------------------------------
def new_stats():
    return {"processed": 0, "filed": 0, "review": 0, "process": 0, "deferred": 0,
            "errors": 0, "write_failed": 0,
            "quarantine": defaultdict(int),      # reason -> count
            "gaps": defaultdict(int)}            # keyword -> count of frames filed without it


def merge_stats(total, part):
    for k, val in part.items():
        if isinstance(val, dict):
            for kk, n in val.items():
                total[k][kk] += n
        else:
            total[k] += val


def summary_line(st):
    q = ", ".join(f"{k}={n}" for k, n in sorted(st["quarantine"].items()))
    g = ", ".join(f"{k}={n}" for k, n in sorted(st["gaps"].items()))
    return (f"filed={st['filed']} review={st['review']} "
            f"quarantined={sum(st['quarantine'].values())}{f' ({q})' if q else ''} "
            f"deferred={st['deferred']} errors={st['errors']} | "
            f"filed without: {g if g else 'nothing'} | header writes failed={st['write_failed']}")


def _quiet_gap(reason):
    """A gap that is expected rather than a failure (no warning line for it)."""
    return reason.startswith("frame is not at a configured site")


def process_dir(cfg, fits, db, stats=None):
    """One pass over incoming/. Returns the number of files moved; per-file
    outcomes are accumulated into `stats` (see new_stats) when given."""
    st = stats if stats is not None else new_stats()
    files = scan(cfg["watch"]["incoming"], cfg, require_stable=True)
    if not files:
        return 0
    root = cfg["destination"]["root"]
    kw = cfg["sqm"].get("keyword", "SQM")
    n = 0
    for f in files:
        name = os.path.basename(f)
        try:
            v, kind, extra = analyze(f, cfg, fits)
            if kind == "defer":
                st["deferred"] += 1
                log(f"  defer      {name}: {v.get('_structure_note')}; left in incoming")
                continue
            dest = place_live(f, v, kind, extra, cfg)
            action, dest = move(f, dest, cfg["destination"]["on_conflict"])
            written, problems = {}, []
            if kind in cfg["routes"]:          # science frames only: stamp, then hooks
                written, problems = finalize_header(dest, kind, v, cfg, fits, db)
                run_hooks(dest, v, cfg)
            sqm_val = float(written[kw][0]) if kw in written else None
            reldest = os.path.relpath(dest, root)
            # `action` (rename/copy/skip) is how the file was moved; the recorded
            # status is the disposition -- a filed frame reads "filed", not "rename".
            status = "filed" if action in ("rename", "copy") else action
            detail = None
            if kind == "quarantine":
                detail = v.get("_structure_note") or extra
            elif problems:
                detail = "; ".join(f"{k}: {why}" for k, why in problems)
            record({"frame_utc": v.get("_dt_utc"), "kind": kind,
                    "object": v.get("object") or None, "rig": v.get("rig") or None,
                    "filter": (v.get("filter") if kind in ("light", "flat") else None),
                    "sqm": sqm_val, "dest": reldest, "status": status,
                    "detail": (detail or "")[:255] or None}, db)
            note = f"  SQM={sqm_val}" if sqm_val else ""
            if problems:
                note += "  [" + "; ".join(f"{k}: {why}" for k, why in problems) + "]"
            log(f"  {kind:10} {name} -> {reldest} [{action}]{note}")
            if kind == "quarantine":
                st["quarantine"][extra or "misc"] += 1
                warn(f"quarantined ({extra}) {name}: {detail}")
            elif kind in cfg["routes"]:
                st["filed"] += 1
            elif kind in st:
                st[kind] += 1
            for k, why in problems:
                if k == "header write":
                    st["write_failed"] += 1
                else:
                    st["gaps"][k] += 1
                    if not _quiet_gap(why):
                        warn(f"{os.path.basename(dest)}: filed without {k}: {why}")
            if db is not None and cfg.get("events", {}).get("enabled", True):
                lvl = "warning" if (kind == "quarantine" or problems) else "info"
                sensor = next((s.get("sensor", "") for s in cfg["sqm"].get("sites", [])
                               if s.get("name") == v.get("site")), "")
                db.log_event("ingest", lvl, "transfer", reldest + note, sensor)
            n += 1
        except Exception as e:
            st["errors"] += 1
            log(f"  ERROR {name}: {e}", "error")
            record({"kind": "error", "dest": name,
                    "status": "error", "detail": str(e)[:200]}, db)
            if db is not None and cfg.get("events", {}).get("enabled", True):
                db.log_event("ingest", "error", "transfer", f"{name}: {str(e)[:180]}")
    st["processed"] += n
    return n


def do_once(cfg):
    fits = _need_astropy()
    db = make_db(cfg)
    if db:
        db.ensure_schema()
    st = new_stats()
    try:
        n = process_dir(cfg, fits, db, st)
    finally:
        if db:
            db.close()
    log(f"once: processed {n} file(s); {summary_line(st)}")
    return 1 if (st["errors"] or st["write_failed"]) else 0


def do_watch(cfg):
    fits = _need_astropy()
    db = make_db(cfg)
    if db:
        db.ensure_schema()
    register_extension(db, cfg)
    ext = cfg.get("extension", {})
    name = ext.get("name", "ingest")
    interval = cfg["watch"]["poll_seconds"]
    log(f"watching {cfg['watch']['incoming']} every {interval}s "
        f"(stable={cfg['watch']['stable_seconds']}s)")
    total = new_stats()
    try:
        while True:
            cycle = new_stats()
            n = process_dir(cfg, fits, db, cycle)
            merge_stats(total, cycle)
            if n:
                log(f"cycle: {summary_line(cycle)}")
                log(f"totals since start: {summary_line(total)}")
            if db and ext.get("register"):
                db.heartbeat(name)
            time.sleep(interval)
    except KeyboardInterrupt:
        log("watch: stopped")
    finally:
        log(f"watch totals: {summary_line(total)}")
        if db and ext.get("register"):
            db.deregister(name)
        if db:
            db.close()
    return 0


def do_backfill(cfg, indir, dry_run=False, hooks=False, recurse=True,
                quarantine_torn=False):
    """Add nwingest's missing header cards to frames already in the archive.

    Per FITS file under `indir`: add FILTER (lights and flats without one) and
    the SQM cards (lights at a configured site with a reading in range) when
    absent. Files are never moved or renamed, and a file that already has
    everything is left alone, so a second run changes nothing. Torn, short or
    END-less files are listed for re-copy and never touched. A missing WCS is
    reported, not produced: nwingest has no solver, the capture device or a
    hook supplies it (--hooks reruns the configured hooks on each file that
    was updated or is still missing something). With quarantine_torn the
    torn, short or END-less files are moved (never deleted) to
    quarantine/<reason>/ under the archive root, keeping their names, so a
    fresh copy can take their place. With dry_run nothing is written or moved
    anywhere: no header, no database rows, no hooks."""
    fits = _need_astropy()
    db = make_db(cfg)
    if db and not dry_run:
        db.ensure_schema()
    indir = os.path.abspath(indir)
    files = scan(indir, cfg, require_stable=False)
    if not recurse:
        files = [f for f in files if os.path.dirname(f) == indir]
    if not files:
        log(f"backfill: no FITS under {indir}")
        return 0
    root = cfg["destination"]["root"]
    kw = cfg["sqm"].get("keyword", "SQM")
    verb = "would add" if dry_run else "added"
    st = defaultdict(int)
    recopy = []
    log(f"backfill{' (dry run, nothing will be written)' if dry_run else ''}: "
        f"{len(files)} file(s) under {indir}")
    try:
        for f in files:
            rel = os.path.relpath(f, indir)
            st["scanned"] += 1
            try:
                status, info = fits_structure(f)
                if status != "ok":
                    st["needs_recopy"] += 1
                    why = describe_structure(status, info)
                    reason = {"trailing": "torn", "short": "truncated"}.get(status, "unreadable")
                    if quarantine_torn and not dry_run:
                        qdest = os.path.join(_folder_for("quarantine", reason, {}, cfg),
                                             os.path.basename(f))
                        _action, qdest = move(f, qdest, cfg["destination"]["on_conflict"])
                        qrel = os.path.relpath(qdest, root)
                        record({"kind": "quarantine", "dest": qrel, "status": "backfill",
                                "detail": why[:255]}, db)
                        if db is not None and cfg.get("events", {}).get("enabled", True):
                            db.log_event("ingest", "warning", "backfill", f"{qrel}: {why}")
                        warn(f"MOVED     {rel} -> {qrel}: {why}")
                        st["quarantined"] += 1
                    else:
                        recopy.append(rel)
                        tail = f" (would move to quarantine/{reason}/)" if quarantine_torn else ""
                        warn(f"RE-COPY   {rel}: {why}{tail}")
                    continue
                h = fits.getheader(f)
                v = resolve(f, h, cfg)
                kind = v["type"]
                if kind not in cfg["routes"]:
                    st["skipped"] += 1
                    continue
                edits, gaps = {}, []
                if (kind in ("light", "flat") and v.get("_filter_defaulted")
                        and cfg["resolve"]["filter"].get("write_header", True)):
                    edits["FILTER"] = (v["filter"], "filled by nwingest (header had none)")
                if kind == "light" and cfg["sqm"].get("enabled") and kw not in h:
                    try:
                        cards, why = lookup_sqm(v, cfg, db)
                    except Exception as e:
                        cards, why = None, f"lookup failed: {e}"
                    if cards:
                        edits.update(cards)
                    else:
                        gaps.append((kw, why or "unavailable"))
                        st["sqm_unavailable"] += 1
                if kind == "light" and not has_wcs(h):
                    gaps.append(("WCS", "absent; nwingest does not solve (re-copy from "
                                        "the capture device, or run a solver hook)"))
                    st["wcs_missing"] += 1
                if not edits and not gaps:
                    st["complete"] += 1
                    continue
                if edits and not dry_run:
                    write_header_cards(f, edits, fits)
                    sqm_val = float(edits[kw][0]) if kw in edits else None
                    dest = os.path.relpath(f, root) if f.startswith(root + os.sep) else f
                    record({"frame_utc": v.get("_dt_utc"), "kind": kind,
                            "object": v.get("object") or None, "rig": v.get("rig") or None,
                            "filter": (v.get("filter") if kind in ("light", "flat") else None),
                            "sqm": sqm_val, "dest": dest, "status": "backfill",
                            "detail": ("added " + ", ".join(edits))[:255]}, db)
                    if db is not None and cfg.get("events", {}).get("enabled", True):
                        db.log_event("ingest", "info", "backfill",
                                     f"{dest}: added {', '.join(edits)}")
                if edits:
                    st["would_update" if dry_run else "updated"] += 1
                note = ""
                if gaps:
                    note = "  [missing: " + "; ".join(f"{k}: {why}" for k, why in gaps) + "]"
                log(f"  {verb:9} {rel}: {', '.join(edits) if edits else '-'}{note}")
                if hooks and not dry_run:
                    run_hooks(f, v, cfg)
            except Exception as e:
                st["errors"] += 1
                log(f"  ERROR {rel}: {e}", "error")
    finally:
        if db:
            db.close()
    upd = "would_update" if dry_run else "updated"
    log(f"backfill summary: scanned={st['scanned']} complete={st['complete']} "
        f"{upd}={st[upd]} needs_recopy={st['needs_recopy']} quarantined={st['quarantined']} "
        f"wcs_missing={st['wcs_missing']} sqm_unavailable={st['sqm_unavailable']} "
        f"not_science={st['skipped']} errors={st['errors']}")
    if recopy:
        log("files needing re-copy from the capture device (not modified):")
        for r in recopy:
            log(f"    {r}")
    return 1 if st["errors"] else 0


def main():
    ap = argparse.ArgumentParser(
        prog="nwingest",
        description="Config-driven FITS ingest: watch, classify, rename, file.")
    ap.add_argument("-c", "--config",
                    default=os.environ.get("NWINGEST_CONFIG", "/etc/nwingest/nwingest.yaml"),
                    help="path to the YAML config (default: /etc/nwingest/nwingest.yaml)")
    sub = ap.add_subparsers(dest="mode", required=True)
    p_plan = sub.add_parser("plan", help="scan a directory and print planned moves (read-only)")
    p_plan.add_argument("dir", nargs="?", default=None, help="directory to scan (default: watch.incoming)")
    sub.add_parser("once", help="process the incoming directory once, then exit")
    sub.add_parser("watch", help="poll the incoming directory forever")
    p_bf = sub.add_parser("backfill", help="add missing nwingest header cards (FILTER, SQM) "
                                          "to frames already in the archive; never moves files")
    p_bf.add_argument("dir", help="directory to scan (recursively)")
    p_bf.add_argument("--dry-run", action="store_true",
                      help="list what would change and write nothing (no header, DB, hooks)")
    p_bf.add_argument("--hooks", action="store_true",
                      help="also rerun the configured hooks on each file that was updated "
                           "or is still missing something (e.g. a solver hook for WCS)")
    p_bf.add_argument("--quarantine-torn", action="store_true",
                      help="move torn, truncated or END-less files out of the archive into "
                           "quarantine/<reason>/ (keeping their names) so a fresh copy from "
                           "the capture device can take their place; never deletes")
    p_bf.add_argument("--no-recurse", action="store_true", help="only the directory itself")
    args = ap.parse_args()

    cfg = load_config(args.config)
    configure_logging(cfg)
    if args.mode == "plan":
        return do_plan(cfg, args.dir or cfg["watch"]["incoming"])
    if args.mode == "once":
        return do_once(cfg)
    if args.mode == "watch":
        return do_watch(cfg)
    if args.mode == "backfill":
        return do_backfill(cfg, args.dir, dry_run=args.dry_run, hooks=args.hooks,
                           recurse=not args.no_recurse, quarantine_torn=args.quarantine_torn)
    return 1


if __name__ == "__main__":
    sys.exit(main())
