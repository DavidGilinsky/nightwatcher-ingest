#!/usr/bin/env python3
# ============================================================================
#  Author   : David Gilinsky
#  File     : nwingest.py
#  Purpose  : Config-driven FITS ingest. Watch a directory, classify each
#             frame from its header, rename it to a standard, and file it into
#             an archive tree. Optional SQM stamping, external hooks, and
#             NightWatcher2 web UI registration.
#  Created  : 2026-07-21
#  Modified : 2026-07-21
#  Version  : 0.1.0
#  License  : GPL-3.0-or-later
# ============================================================================
"""nwingest: watch, classify, rename, and file FITS frames by configuration.

Modes:
  plan   scan a directory and print what would happen (read-only, moves nothing)
  once   process the incoming directory a single time, then exit
  watch  poll the incoming directory forever (run under systemd)

Everything is header-driven: the paths and filenames the capture apps produce
are never trusted, only the FITS header. See nwingest.example.yaml.
"""

from __future__ import annotations

import argparse
import errno
import fnmatch
import os
import re
import shutil
import sys
import time
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta

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
        "object": {"messier_alias": True},
        "filter": {"default": "CLEAR", "write_header": True},
        "gain_keywords": ["GAIN", "GAINRAW"],
        "sequence": {"width": 4},
    },
    "rigs": [],
    "camera_aliases": {},
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
    "sqm": {"enabled": False, "keyword": "SQM", "provenance": True,
            "max_gap_minutes": 15, "sites": [], "nwdb": {}},
    "hooks": [],
    "extension": {"register": False, "name": "ingest", "label": "Ingest",
                  "heartbeat_seconds": 20},
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
# Small logging helpers
# ---------------------------------------------------------------------------
def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print(f"[{_now_str()}] {msg}", flush=True)


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


def rig_of(camera, focal, cfg):
    for r in cfg["rigs"]:
        want = r.get("camera")
        if want and want.lower() not in camera.lower():
            continue
        lo, hi = r["focal_mm"]
        if focal is not None and lo <= focal <= hi:
            return r["name"]
    return camera or "UNKNOWN"


def norm_object(h, cfg):
    obj = str(hget(h, "OBJECT", default="") or "").strip()
    if not obj:
        return ""
    if cfg["resolve"]["object"].get("messier_alias"):
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
# nwdb access (SQM lookup)
# ---------------------------------------------------------------------------
class Nwdb:
    """Lazy, reused connection to the NightWatcher database (PyMySQL)."""

    def __init__(self, cfg):
        self.c = cfg["sqm"].get("nwdb", {})
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
        """(ts_utc, mag) for the reading nearest dt within +/- gap_min, else None."""
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
            except Exception as e:
                self.conn = None
                if attempt == 2:
                    log(f"    warn: nwdb query failed: {e}")
        return None

    def close(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None


def lookup_sqm(v, cfg, db):
    """Header cards for the SQM reading nearest this light frame, or None."""
    site, dt = v.get("site"), v.get("_dt_utc")
    if not site or dt is None or db is None:
        return None
    scfg = next((s for s in cfg["sqm"].get("sites", []) if s["name"] == site), None)
    if not scfg or not scfg.get("sensor"):
        return None
    row = db.nearest_reading(scfg["sensor"], dt, cfg["sqm"].get("max_gap_minutes", 15))
    if not row:
        return None
    ts, mag = row[0], float(row[1])
    kw = cfg["sqm"].get("keyword", "SQM")
    cards = {kw: (round(mag, 3), "sky brightness mag/arcsec^2 (nwingest)")}
    if cfg["sqm"].get("provenance", True):
        cards["SQMSRC"] = (scfg["sensor"], "SQM sensor id")
        cards["SQMTIME"] = (ts.strftime("%Y-%m-%dT%H:%M:%S"), "SQM reading time (UTC)")
        cards["SQMDT"] = (int(abs((dt - ts).total_seconds())), "sec between reading and DATE-OBS")
    return cards


# ---------------------------------------------------------------------------
# Header finalization: FILTER default + SQM stamp, in a single file open
# ---------------------------------------------------------------------------
def finalize_header(path, kind, v, cfg, fits, db):
    edits = {}
    if (kind in ("light", "flat") and v.get("_filter_defaulted")
            and cfg["resolve"]["filter"].get("write_header", True)):
        edits["FILTER"] = (v["filter"], "filled by nwingest (header had none)")
    if kind == "light" and cfg["sqm"].get("enabled"):
        sqm = lookup_sqm(v, cfg, db)
        if sqm:
            edits.update(sqm)
    if not edits:                       # nothing to write: don't rewrite the file
        return None
    try:
        with fits.open(path, mode="update") as hdul:
            hdr = hdul[0].header
            for k, (val, comment) in edits.items():
                hdr[k] = (val, comment)
        return edits
    except Exception as e:
        log(f"    warn: header finalize failed on {os.path.basename(path)}: {e}")
        return None


# ---------------------------------------------------------------------------
# Stubs for the next build stage (hooks exec, DB log, extension registry)
# ---------------------------------------------------------------------------
def run_hooks(dest, v, cfg):
    """Requirement 5: run configured external programs on the filed file."""
    for hk in cfg.get("hooks", []):
        if not hk.get("enabled"):
            continue
        when = hk.get("when", {})
        if any(str(v.get(k)) != str(val) for k, val in when.items()):
            continue
        # TODO: subprocess.run(hk["run"].format_map(defaultdict(str, {**v, "dest": dest})),
        #       shell=True, timeout=hk.get("timeout_s"))  -- background if hk.get("background")
        log(f"    hook (TODO) would run: {hk['name']}")


def record(entry, cfg):
    """TODO: insert into nwdb ingest_log for the web UI Ingest tab."""
    return None


def register(cfg):
    """Requirement 6: TODO upsert an extensions row so the NightWatcher2 Ingest
    tab appears while this watcher is alive."""
    if cfg["extension"].get("register"):
        log("extension registration (TODO): would announce '%s' to nwdb"
            % cfg["extension"].get("name"))


def heartbeat(cfg):
    """TODO: touch the extensions row so the tab stays visible while running."""
    return None


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
    try:
        h = fits.getheader(path)
    except Exception:
        return ({"ext": _ext(path), "_no_date": True}, "quarantine", "unreadable")
    v = resolve(path, h, cfg)
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
        rel = os.path.relpath(dest, cfg["destination"]["root"])
        print(f"  {kind:10} {os.path.basename(src)}\n             -> {rel}")
    log("plan summary: " + ", ".join(f"{k}={counts[k]}" for k in sorted(counts)))
    return 0


def process_dir(cfg, fits):
    files = scan(cfg["watch"]["incoming"], cfg, require_stable=True)
    if not files:
        return 0
    db = Nwdb(cfg) if cfg["sqm"].get("enabled") else None
    n = 0
    try:
        for f in files:
            try:
                v, kind, extra = analyze(f, cfg, fits)
                dest = place_live(f, v, kind, extra, cfg)
                action, dest = move(f, dest, cfg["destination"]["on_conflict"])
                edits = finalize_header(dest, kind, v, cfg, fits, db)
                run_hooks(dest, v, cfg)
                record({"src": f, "dest": dest, "kind": kind,
                        "object": v.get("object"), "status": action}, cfg)
                sqm = edits.get(cfg["sqm"].get("keyword", "SQM")) if edits else None
                note = f"  SQM={sqm[0]}" if sqm else ""
                log(f"  {kind:10} {os.path.basename(f)} -> "
                    f"{os.path.relpath(dest, cfg['destination']['root'])} [{action}]{note}")
                n += 1
            except Exception as e:
                log(f"  ERROR {os.path.basename(f)}: {e}")
    finally:
        if db:
            db.close()
    return n


def do_once(cfg):
    fits = _need_astropy()
    n = process_dir(cfg, fits)
    log(f"once: processed {n} file(s)")
    return 0


def do_watch(cfg):
    fits = _need_astropy()
    register(cfg)
    interval = cfg["watch"]["poll_seconds"]
    log(f"watching {cfg['watch']['incoming']} every {interval}s "
        f"(stable={cfg['watch']['stable_seconds']}s)")
    try:
        while True:
            process_dir(cfg, fits)
            heartbeat(cfg)
            time.sleep(interval)
    except KeyboardInterrupt:
        log("watch: stopped")
    return 0


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
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.mode == "plan":
        return do_plan(cfg, args.dir or cfg["watch"]["incoming"])
    if args.mode == "once":
        return do_once(cfg)
    if args.mode == "watch":
        return do_watch(cfg)
    return 1


if __name__ == "__main__":
    sys.exit(main())
