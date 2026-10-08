#!/usr/bin/env python3
# ============================================================================
#  Author   : David Gilinsky
#  File     : tests/test_nwingest.py
#  Purpose  : Tests for nwingest: structural validation of incoming frames
#             (torn and short copies), independent FILTER/SQM header steps,
#             the own header writer, run summaries, and the backfill mode.
#  Created  : 2026-10-04
#  Modified : 2026-10-07
#  Version  : 0.3.0
#  License  : GPL-3.0-or-later
# ============================================================================
"""Run with:  python3 -m pytest -q tests/   or   python3 -m unittest -v tests.test_nwingest

The frames are tiny (6 x 8 pixels) but byte-for-byte the same layout as a
camera's output: a primary header, padded int16 data, nothing else. The torn
case appends the 2880-byte block a mid-rewrite copy carries (pixel-like bytes
then NULs), which is what broke the 2026-10-03 NGC7720 session (see rca/)."""

import glob
import hashlib
import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np                                   # noqa: E402
from astropy.io import fits                          # noqa: E402

import nwingest                                      # noqa: E402

SITE = {"name": "TestSite", "lat": 32.30314, "lon": -110.98589, "tol_deg": 0.05,
        "sensor": "SQM1"}
TORN_TAIL = bytes([0x90, 0x98, 0x8f, 0xf8]) * 240 + b"\0" * 1920     # 2880 bytes
SHAPE = (6, 8)


def write_frame(path, imagetyp="Light", exptime=120.0, with_filter=False,
                trailing=b"", truncate=0, extra_cards=None, old=True):
    data = (np.arange(SHAPE[0] * SHAPE[1]).reshape(SHAPE) * 37 % 60000).astype(np.uint16)
    hdu = fits.PrimaryHDU(data)
    h = hdu.header
    h["IMAGETYP"] = imagetyp
    h["EXPTIME"] = exptime
    h["DATE-OBS"] = "2026-10-04T02:21:06.688963"
    h["INSTRUME"] = "ZWO ASI4400MC Pro"
    h["FOCALLEN"] = 520
    h["GAIN"] = 136
    h["OFFSET"] = 15
    h["XBINNING"] = 1
    h["CCD-TEMP"] = -0.1
    h["OBJECT"] = "NGC 7720"
    h["SITELAT"] = 32.3032
    h["SITELONG"] = -110.986
    if with_filter:
        h["FILTER"] = with_filter if isinstance(with_filter, str) else "CLEAR"
    for k, val in (extra_cards or {}).items():
        h[k] = val
    hdu.writeto(path, overwrite=True)
    if trailing:
        with open(path, "ab") as fh:
            fh.write(trailing)
    if truncate:
        os.truncate(path, os.path.getsize(path) - truncate)
    if old:                                   # make it "stable" for scan()
        t = time.time() - 120
        os.utime(path, (t, t))
    return path


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


class FakeDb:
    def __init__(self, reading=None, raise_on_lookup=False):
        self.reading = reading
        self.raise_on_lookup = raise_on_lookup
        self.ingest = []
        self.events = []

    def nearest_reading(self, sensor, dt, gap):
        if self.raise_on_lookup:
            raise RuntimeError("db down")
        return self.reading

    def log_ingest(self, r):
        self.ingest.append(dict(r))

    def log_event(self, source, level, event, detail, device_id=""):
        self.events.append((level, event, detail))

    def ensure_schema(self):
        pass

    def close(self):
        pass


def make_cfg(tmp):
    cfg = deepcopy(nwingest.DEFAULTS)
    cfg["watch"]["incoming"] = os.path.join(tmp, "incoming")
    cfg["destination"]["root"] = tmp
    cfg["sqm"] = {"enabled": True, "keyword": "SQM", "provenance": True,
                  "max_gap_minutes": 15, "sites": [SITE]}
    cfg["hooks"] = []
    os.makedirs(cfg["watch"]["incoming"], exist_ok=True)
    return cfg


def run_once(cfg, db):
    st = nwingest.new_stats()
    out = io.StringIO()
    with redirect_stdout(out):
        n = nwingest.process_dir(cfg, fits, db, st)
    return n, st, out.getvalue()


READING = (datetime(2026, 10, 4, 2, 23, 0), 19.18)


class StructureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nwtest-")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_whole_file_is_ok(self):
        p = write_frame(os.path.join(self.tmp, "a.fits"))
        status, info = nwingest.fits_structure(p)
        self.assertEqual(status, "ok")
        self.assertEqual(info["size"], info["expected"])
        self.assertEqual(info["hdr_bytes"], 2880)

    def test_trailing_block_is_torn(self):
        p = write_frame(os.path.join(self.tmp, "a.fits"), trailing=TORN_TAIL)
        status, info = nwingest.fits_structure(p)
        self.assertEqual(status, "trailing")
        self.assertEqual(info["extra"], 2880)

    def test_blank_trailing_padding_is_tolerated(self):
        p = write_frame(os.path.join(self.tmp, "a.fits"), trailing=b"\0" * 2880)
        self.assertEqual(nwingest.fits_structure(p)[0], "ok")

    def test_short_file(self):
        p = write_frame(os.path.join(self.tmp, "a.fits"), truncate=100)
        self.assertEqual(nwingest.fits_structure(p)[0], "short")

    def test_missing_end_card(self):
        p = write_frame(os.path.join(self.tmp, "a.fits"))
        raw = bytearray(open(p, "rb").read())
        i = raw.find(b"END     ")
        raw[i:i + 80] = b" " * 80
        open(p, "wb").write(raw)
        self.assertEqual(nwingest.fits_structure(p)[0], "no-end")


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nwtest-")
        self.cfg = make_cfg(self.tmp)
        self.inc = self.cfg["watch"]["incoming"]

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def filed(self):
        return glob.glob(os.path.join(self.tmp, "lights", "**", "*.fits"), recursive=True)

    def test_torn_copy_is_quarantined_not_filed(self):
        src = write_frame(os.path.join(self.inc, "Light_torn_0007.fit"), trailing=TORN_TAIL)
        db = FakeDb(reading=READING)
        n, st, out = run_once(self.cfg, db)
        self.assertEqual(n, 1)
        self.assertFalse(os.path.exists(src))
        self.assertEqual(self.filed(), [])
        q = glob.glob(os.path.join(self.tmp, "quarantine", "torn", "*.fits"))
        self.assertEqual(len(q), 1)
        self.assertEqual(sha(q[0]), sha(q[0]))               # untouched: still torn
        self.assertEqual(nwingest.fits_structure(q[0])[0], "trailing")
        self.assertEqual(st["quarantine"]["torn"], 1)
        self.assertEqual(st["filed"], 0)
        self.assertIn("warn: quarantined (torn)", out)
        self.assertIn("re-copy", out)
        self.assertEqual(db.ingest[0]["kind"], "quarantine")
        self.assertIn("torn copy", db.ingest[0]["detail"])
        self.assertEqual(db.events[0][0], "warning")
        self.assertIn("torn", nwingest.summary_line(st))

    def test_short_file_is_left_in_incoming_then_quarantined_when_stale(self):
        src = write_frame(os.path.join(self.inc, "Light_short.fit"), truncate=200)
        db = FakeDb(reading=READING)
        n, st, out = run_once(self.cfg, db)
        self.assertEqual(n, 0)
        self.assertTrue(os.path.exists(src))                  # not moved
        self.assertEqual(st["deferred"], 1)
        self.assertIn("defer", out)
        self.cfg["watch"]["truncated_after_s"] = 0            # now it is stale
        n, st, out = run_once(self.cfg, db)
        self.assertEqual(n, 1)
        self.assertFalse(os.path.exists(src))
        self.assertEqual(st["quarantine"]["truncated"], 1)

    def test_filter_is_written_when_sqm_lookup_raises(self):
        write_frame(os.path.join(self.inc, "Light_a.fit"))
        db = FakeDb(raise_on_lookup=True)
        n, st, out = run_once(self.cfg, db)
        self.assertEqual(n, 1)
        f = self.filed()
        self.assertEqual(len(f), 1)
        h = fits.getheader(f[0])
        self.assertEqual(h["FILTER"], "CLEAR")
        self.assertNotIn("SQM", h)
        self.assertEqual(st["filed"], 1)
        self.assertEqual(st["gaps"]["SQM"], 1)
        self.assertEqual(st["write_failed"], 0)
        self.assertIn("filed without SQM: lookup failed", out)
        self.assertIn("SQM: lookup failed", db.ingest[0]["detail"])
        self.assertEqual(db.ingest[0]["status"], "filed")
        self.assertEqual(db.events[0][0], "warning")
        self.assertIn("SQM=1", nwingest.summary_line(st))

    def test_filter_and_sqm_written_on_the_good_path(self):
        write_frame(os.path.join(self.inc, "Light_a.fit"))
        db = FakeDb(reading=READING)
        n, st, out = run_once(self.cfg, db)
        f = self.filed()
        h = fits.getheader(f[0])
        self.assertEqual(h["FILTER"], "CLEAR")
        self.assertAlmostEqual(h["SQM"], 19.18)
        self.assertEqual(h["SQMSRC"], "SQM1")
        self.assertEqual(dict(st["gaps"]), {})
        self.assertIn("SQM=19.18", out)
        self.assertAlmostEqual(db.ingest[0]["sqm"], 19.18)
        self.assertEqual(db.events[0][0], "info")
        self.assertEqual(nwingest.fits_structure(f[0])[0], "ok")

    def test_no_reading_is_a_visible_gap_not_a_failure(self):
        write_frame(os.path.join(self.inc, "Light_a.fit"))
        db = FakeDb(reading=None)
        n, st, out = run_once(self.cfg, db)
        h = fits.getheader(self.filed()[0])
        self.assertEqual(h["FILTER"], "CLEAR")
        self.assertNotIn("SQM", h)
        self.assertEqual(st["gaps"]["SQM"], 1)
        self.assertIn("no SQM1 reading within 15 min", out)


class HeaderWriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nwtest-")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_in_place_when_the_header_has_room(self):
        p = write_frame(os.path.join(self.tmp, "a.fits"))
        before = os.path.getsize(p)
        data0 = fits.getdata(p).copy()
        nwingest.write_header_cards(p, {"FILTER": ("CLEAR", "c"), "SQM": (19.18, "s")}, fits)
        self.assertEqual(os.path.getsize(p), before)
        h = fits.getheader(p)
        self.assertEqual(h["FILTER"], "CLEAR")
        self.assertTrue(np.array_equal(fits.getdata(p), data0))
        self.assertEqual(nwingest.fits_structure(p)[0], "ok")

    def test_grow_rewrites_through_a_temp_file(self):
        p = write_frame(os.path.join(self.tmp, "a.fits"))
        data0 = fits.getdata(p).copy()
        edits = {f"KEY{i:03d}": (i, "pad") for i in range(40)}     # 21 + 40 cards: 2 blocks
        nwingest.write_header_cards(p, edits, fits)
        h = fits.getheader(p)
        self.assertEqual(h["KEY039"], 39)
        self.assertTrue(np.array_equal(fits.getdata(p), data0))
        status, info = nwingest.fits_structure(p)
        self.assertEqual(status, "ok")
        self.assertEqual(info["hdr_bytes"], 5760)
        self.assertEqual(glob.glob(os.path.join(self.tmp, "*tmp*")), [])

    def test_refuses_a_torn_file(self):
        p = write_frame(os.path.join(self.tmp, "a.fits"), trailing=TORN_TAIL)
        s0 = sha(p)
        with self.assertRaises(OSError):
            nwingest.write_header_cards(p, {"FILTER": ("CLEAR", "c")}, fits)
        self.assertEqual(sha(p), s0)


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nwtest-")
        self.cfg = make_cfg(self.tmp)
        self.arch = os.path.join(self.tmp, "lights", "NGC7720", "ASI4400", "2026-10-03", "CLEAR")
        os.makedirs(self.arch)
        wcs = {"CTYPE1": "RA---TAN-SIP", "CTYPE2": "DEC--TAN-SIP", "CRVAL1": 354.9,
               "CRVAL2": 27.5, "CRPIX1": 2759.0, "CRPIX2": 3086.0}
        self.raw = write_frame(os.path.join(self.arch, "raw_0007.fits"))
        self.done = write_frame(os.path.join(self.arch, "done_0008.fits"), with_filter=True,
                                extra_cards={"SQM": 19.18, **wcs})
        self.torn = write_frame(os.path.join(self.arch, "torn_0012.fits"), trailing=TORN_TAIL)
        self.dark = write_frame(os.path.join(self.arch, "dark.fits"), imagetyp="Dark")
        self.before = {p: sha(p) for p in (self.raw, self.done, self.torn, self.dark)}

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def backfill(self, db, **kw):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = nwingest.do_backfill(self.cfg, self.arch, **kw)
        return rc, out.getvalue()

    def test_dry_run_changes_nothing_and_lists_the_plan(self):
        db = FakeDb(reading=READING)
        with unittest.mock.patch.object(nwingest, "make_db", lambda cfg: db):
            rc, out = self.backfill(db, dry_run=True)
        self.assertEqual(rc, 0)
        for p, s in self.before.items():
            self.assertEqual(sha(p), s, p)
        self.assertEqual(db.ingest, [])
        self.assertEqual(db.events, [])
        self.assertIn("would add raw_0007.fits: FILTER, SQM, SQMSRC, SQMTIME, SQMDT", out)
        self.assertIn("WCS: absent", out)
        self.assertIn("RE-COPY   torn_0012.fits: torn copy", out)
        self.assertNotIn("done_0008", out.split("backfill summary")[0].replace("would add", ""))
        self.assertIn("would_update=1 needs_recopy=1 quarantined=0 wcs_missing=1", out)

    def test_apply_then_rerun_is_idempotent(self):
        db = FakeDb(reading=READING)
        with unittest.mock.patch.object(nwingest, "make_db", lambda cfg: db):
            rc, out = self.backfill(db)
            self.assertEqual(rc, 0)
            h = fits.getheader(self.raw)
            self.assertEqual(h["FILTER"], "CLEAR")
            self.assertAlmostEqual(h["SQM"], 19.18)
            self.assertEqual(nwingest.fits_structure(self.raw)[0], "ok")
            for p in (self.done, self.torn, self.dark):
                self.assertEqual(sha(p), self.before[p], p)
            self.assertEqual(len(db.ingest), 1)
            self.assertEqual(db.ingest[0]["status"], "backfill")
            self.assertIn("added FILTER, SQM", db.ingest[0]["detail"])
            self.assertIn("updated=1 needs_recopy=1", out)
            after = {p: sha(p) for p in self.before}
            rc, out = self.backfill(db)
            self.assertEqual(rc, 0)
            self.assertEqual({p: sha(p) for p in self.before}, after)
            self.assertEqual(len(db.ingest), 1)                     # no new rows
            self.assertIn("updated=0", out)


    def test_quarantine_torn_moves_only_the_torn_file(self):
        db = FakeDb(reading=READING)
        with unittest.mock.patch.object(nwingest, "make_db", lambda cfg: db):
            rc, out = self.backfill(db, dry_run=True, quarantine_torn=True)
            self.assertTrue(os.path.exists(self.torn))             # dry run moves nothing
            self.assertIn("would move to quarantine/torn/", out)
            rc, out = self.backfill(db, quarantine_torn=True)
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.torn))
        moved = os.path.join(self.tmp, "quarantine", "torn", "torn_0012.fits")
        self.assertTrue(os.path.exists(moved))
        self.assertEqual(sha(moved), self.before[self.torn])        # moved, not altered
        self.assertTrue(os.path.exists(self.done) and os.path.exists(self.dark))
        self.assertIn("MOVED     torn_0012.fits -> quarantine/torn/torn_0012.fits", out)
        self.assertIn("quarantined=1", out)
        kinds = [r["kind"] for r in db.ingest]
        self.assertIn("quarantine", kinds)


ASIAIR_LIGHT = "Light_IC 1805_60.0s_Bin1_4400MC_gain136_20261007-174102_242deg_0.0C_F_ALP_T_5nm_0001.fit"
ASIAIR_FLAT = "Flat_1.0s_Bin1_4400MC_gain136_20261007-174812_242deg_0.0C_F_ALP_T_5nm_0001.fit"
ASIAIR_PLAIN = "Light_IC 1805_60.0s_Bin1_4400MC_gain136_20261007-044639_241deg_-0.1C_0398.fit"


class FilterFromFilenameTests(unittest.TestCase):
    """The manual filter drawer: a token the ASIAir puts in the filename."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nwtest-")
        self.cfg = make_cfg(self.tmp)
        self.cfg["sqm"]["enabled"] = False
        self.cfg["resolve"]["filter"]["from_filename"]["enabled"] = True
        self.cfg["resolve"]["filter"]["from_filename"]["aliases"] = {"alp_t_5nm": "ALP-T-5nm"}
        self.inc = self.cfg["watch"]["incoming"]

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_rule_parses_the_asiair_layout(self):
        f = nwingest.filter_from_filename
        self.assertEqual(f(ASIAIR_LIGHT, self.cfg), "ALP-T-5nm")       # alias, any case
        self.assertEqual(f(ASIAIR_FLAT, self.cfg), "ALP-T-5nm")
        self.assertIsNone(f(ASIAIR_PLAIN, self.cfg))                   # no token
        self.assertEqual(f("Light_M31_60.0s_Bin1_4400MC_gain136_20261007-010203_0deg_0.0C_F_Ha_0012.fit", self.cfg),
                         "Ha")                                         # unaliased: as typed
        self.assertEqual(f("Light_M31_60.0s_Bin1_4400MC_gain136_20261007-010203_0deg_0.0C_f_sii_0012.fits", self.cfg),
                         "sii")                                        # prefix is case-insensitive
        self.assertIsNone(f("Light_M31_60.0s_Bin1_4400MC_gain136_20261007-010203_0deg_0.0C_note_0012.fit", self.cfg))
        self.cfg["resolve"]["filter"]["from_filename"]["enabled"] = False
        self.assertIsNone(f(ASIAIR_LIGHT, self.cfg))                   # off by default

    def test_light_with_token_is_filed_by_filter_with_cards(self):
        write_frame(os.path.join(self.inc, ASIAIR_LIGHT))
        db = FakeDb()
        n, st, out = run_once(self.cfg, db)
        self.assertEqual(n, 1)
        f = glob.glob(os.path.join(self.tmp, "lights", "*", "*", "*", "ALP-T-5nm", "*.fits"))
        self.assertEqual(len(f), 1)
        self.assertIn("_ALP-T-5nm_", os.path.basename(f[0]))
        h = fits.getheader(f[0])
        self.assertEqual(h["FILTER"], "ALP-T-5nm")
        self.assertIn("filename token", h.comments["FILTER"])
        self.assertEqual(h["SRCFILE"], ASIAIR_LIGHT)
        self.assertEqual(db.ingest[0]["filter"], "ALP-T-5nm")
        self.assertEqual(st["filed"], 1)

    def test_flat_with_token_is_filed_under_the_filter(self):
        write_frame(os.path.join(self.inc, ASIAIR_FLAT), imagetyp="Flat", exptime=1.0)
        n, st, out = run_once(self.cfg, FakeDb())
        f = glob.glob(os.path.join(self.tmp, "calibration", "flat", "*", "ALP-T-5nm", "*", "*.fits"))
        self.assertEqual(len(f), 1)
        self.assertEqual(fits.getheader(f[0])["FILTER"], "ALP-T-5nm")

    def test_no_token_stays_clear(self):
        write_frame(os.path.join(self.inc, ASIAIR_PLAIN))
        n, st, out = run_once(self.cfg, FakeDb())
        f = glob.glob(os.path.join(self.tmp, "lights", "*", "*", "*", "CLEAR", "*.fits"))
        self.assertEqual(len(f), 1)
        h = fits.getheader(f[0])
        self.assertEqual(h["FILTER"], "CLEAR")
        self.assertIn("header had none", h.comments["FILTER"])
        self.assertEqual(h["SRCFILE"], ASIAIR_PLAIN)

    def test_header_filter_wins_over_token_with_a_warning(self):
        write_frame(os.path.join(self.inc, ASIAIR_LIGHT), with_filter="Ha")
        n, st, out = run_once(self.cfg, FakeDb())
        f = glob.glob(os.path.join(self.tmp, "lights", "*", "*", "*", "Ha", "*.fits"))
        self.assertEqual(len(f), 1)
        h = fits.getheader(f[0])
        self.assertEqual(h["FILTER"], "Ha")
        self.assertEqual(h.comments["FILTER"], "")                      # the capture app's card, untouched
        self.assertIn("header FILTER='Ha' but the filename token says 'ALP-T-5nm'", out)

    def test_plan_shows_the_resolved_filter(self):
        write_frame(os.path.join(self.inc, ASIAIR_LIGHT))
        out = io.StringIO()
        with redirect_stdout(out):
            nwingest.do_plan(self.cfg, self.inc)
        self.assertIn("/ALP-T-5nm/", out.getvalue())
        self.assertTrue(os.path.exists(os.path.join(self.inc, ASIAIR_LIGHT)))   # plan moves nothing

    def test_backfill_rederives_filter_from_the_source_card(self):
        arch = os.path.join(self.tmp, "lights", "IC1805", "ASI4400", "2026-10-06", "CLEAR")
        os.makedirs(arch)
        p = write_frame(os.path.join(arch, "IC1805_filed_0001.fits"), extra_cards={"SRCFILE": ASIAIR_LIGHT})
        db = FakeDb()
        with unittest.mock.patch.object(nwingest, "make_db", lambda cfg: db):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = nwingest.do_backfill(self.cfg, arch)
        self.assertEqual(rc, 0)
        h = fits.getheader(p)
        self.assertEqual(h["FILTER"], "ALP-T-5nm")
        self.assertIn("filename token", h.comments["FILTER"])
        self.assertIn("added FILTER", db.ingest[0]["detail"])


import unittest.mock  # noqa: E402  (used by BackfillTests)

if __name__ == "__main__":
    unittest.main()
