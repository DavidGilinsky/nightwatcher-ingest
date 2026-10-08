# nightwatcher-ingest

A small, config-driven watcher that files raw FITS frames. It watches an
incoming directory, reads each new frame's header, renames it to a standard,
and moves it into an organized archive tree. Optionally it stamps each frame
with a sky-brightness reading from [NightWatcher2](https://github.com/DavidGilinsky),
runs external programs on the result, and shows its activity in the
NightWatcher2 web UI.

It is header-driven on purpose. Three capture apps (NINA, TheSkyX, an ASIair)
produce three different folder layouts and filenames, but they all write a sane
FITS header, so the header is the only thing trusted. The one deliberate
exception is a filter token in the ASIair's filename for a manual filter drawer,
which the ASIair cannot record anywhere else (see *Manual filter drawer*).
Nothing about the naming scheme is hardcoded; it all lives in one YAML file.

It works as a plain FITS organizer with no SQM and no NightWatcher at all. The
sky-brightness stamping and the web UI tab are optional extras that light up
when you configure them.

## What it does per frame

```
incoming/  ->  check structure  ->  read header  ->  classify  ->  rename  ->  file into the tree  ->  stamp header  ->  hooks
```

- **Check structure** before anything moves: is the file whole (header, END
  card, exactly the padded image data)? A short file is left in `incoming/` to
  finish; one with stray bytes after the image is a torn copy and goes to
  `quarantine/torn/`. A torn copy's header reads fine, but its pixel rows come
  from two layouts of the same frame, so filing it would only hide the damage.
- **Classify** the frame type (light, dark, flat, bias, flatdark) from the header.
- **Resolve** the target, rig, filter, night, exposure, gain, offset, binning,
  and temperature into template variables.
- **Rename** using a configurable filename template.
- **File** into a configurable directory structure.
- **Stamp** the header: `FILTER` when the frame carries none, and the SQM cards
  when enabled. The two are decided independently, so a failed SQM lookup never
  costs a frame its `FILTER`. nwingest writes the header itself (in place when it
  fits, otherwise through a temp file and rename), so a frame is never left
  half-updated.
- Light frames that are really focus, slew, or preview shots go to `review/`;
  frames with a broken or missing header go to `quarantine/`. Nothing is deleted.
- Every step that could not be completed is logged as a `warn:` line naming the
  file and the step, carried in the ingest log's `detail` column and the events
  row, and counted in a summary line at the end of each cycle (`watch`) or run
  (`once`): filed, quarantined by reason, deferred, frames filed without a
  keyword, header writes that failed.

Example result:

```
lights/M57/Askar185-ASI6200/2026-05-22/CLEAR/M57_2026-05-23T102229Z_0055_Askar185-ASI6200_CLEAR_300s_g100_o50_bin1_0C.fits
```

## Install

**Debian/Ubuntu (recommended)** — build and install the package:

```sh
make deb
sudo apt install ./nightwatcher-ingest_0.1.0_all.deb
```

The install **prompts** for the watch directory, whether to enable the SQM
stamp and the web-UI Ingest tab, the NightWatcher database password, and an
optional group to grant the service account — and applies them to
`/etc/nwingest/`. Re-run those prompts any time with:

```sh
sudo dpkg-reconfigure nightwatcher-ingest
```

It installs `nwingest` to `/usr/bin`, a `nwingest` systemd service, the config
under `/etc/nwingest/`, and pulls in `python3-astropy`, `python3-yaml`, and
`python3-pymysql`, creating an unprivileged `nwingest` service account. Give
that account read on the watch directory and write on the archive tree (name a
group at the prompt, or set ownership / an ACL), point the capture apps at the
watch directory, then `sudo systemctl start nwingest`.

**From source / other platforms:**

```sh
pip install astropy pyyaml          # add PyMySQL too if you enable the SQM stamp
```

Python 3.9+. `astropy` does the FITS work; `pyyaml` reads the config; `PyMySQL`
is only needed for the SQM stamp and the extension registry.

## Configure

Copy the example and edit it:

```sh
cp nwingest.example.yaml /etc/nwingest/nwingest.yaml
```

The config controls everything (see the comments in
[`nwingest.example.yaml`](nwingest.example.yaml)):

1. **Where it watches** and how it decides a file is finished being written.
2. **The directory structure**, as a `path` template per frame type.
3. **The filename**, as a `filename` template per frame type.
4. **How header values become variables** (rig by focal length, gain from
   `GAIN` or `GAINRAW`, local noon-to-noon nights, Messier aliases, and `CLEAR`
   as the default filter when a frame carries none). A light or flat whose
   header has no `FILTER` also gets `FILTER=CLEAR` written into it, so WBPP
   groups it correctly. Every filed science frame also gets a `SRCFILE` card
   with the capture app's original filename.
5. **A manual filter drawer** (`resolve.filter.from_filename`, below).
6. **Hooks** (below).
7. **SQM stamping and the web UI tab** (below).

Templates use `{variable}` placeholders. Available variables:
`{object} {type} {rig} {camera} {night} {filter} {utc} {seq} {exp} {gain}
{offset} {bin} {temp} {site}`. Empty ones collapse cleanly, so a missing filter
never leaves a `__` in the name.

## Run

```sh
nwingest --config /etc/nwingest/nwingest.yaml plan [DIR]   # read-only: show what would happen
nwingest --config /etc/nwingest/nwingest.yaml once         # process incoming once, then exit
nwingest --config /etc/nwingest/nwingest.yaml watch        # poll incoming forever
nwingest --config /etc/nwingest/nwingest.yaml backfill DIR [--dry-run] [--hooks]
```

Start with `plan`. It moves nothing, it just prints the old name and where each
frame would land, so you can see the scheme applied to real files before you
trust it.

For production, run `watch` as a service. A unit file is in
[`systemd/nwingest.service`](systemd/nwingest.service):

```sh
sudo systemctl enable --now nwingest
journalctl -fu nwingest
```

Over NFS it polls rather than using inotify, because an NFS client cannot see
writes made by other hosts. It only touches a file once it has been size-stable
for a few seconds, which covers both an app writing directly and a network copy
landing.

## Manual filter drawer: the filter from the capture filename

An ASIAir with a filter drawer instead of an EFW has no way to put the filter
in the header. It does let you type a custom name that it inserts into every
filename, between the temperature field and the sequence number, on lights and
flats alike:

```
Light_IC 1805_60.0s_Bin1_4400MC_gain136_20261007-174102_242deg_0.0C_F_ALP_T_5nm_0001.fit
Flat_1.0s_Bin1_4400MC_gain136_20261007-174812_242deg_0.0C_F_ALP_T_5nm_0001.fit
```

Type `F_<name>` there (the ASIAir's field accepts letters, digits and
underscores) and turn on `resolve.filter.from_filename`:

```yaml
resolve:
  filter:
    from_filename:
      enabled: true
      pattern: '_-?\d+(?:\.\d+)?C_F_(?P<filter>.+?)_\d{4}\.fits?$'
      aliases:
        ALP_T_5nm: ALP-T-5nm     # raw token (any case) -> FILTER value and folder name
```

The rules, in order: a `FILTER` card in the header always wins, so an EFW rig
is unaffected; otherwise the token is used; otherwise the frame is `CLEAR` as
before. A token with an alias becomes the alias (keep underscores out of the
result, since nwingest's own filenames are underscore-delimited); one without is
used as typed. If the header and the token disagree, the header value is kept
and a warning names the file. The result drives the `{filter}` folder and
filename and is written as `FILTER` with a comment saying it came from the
filename, so WBPP matches the flats. `backfill` can re-derive `FILTER` for a
frame that lacks one from its `SRCFILE` card.

### At the telescope

1. Load the drawer, then type the token in the ASIair app's custom file-name
   field: `F_` plus the name you want to see, for example `F_ALP_T_5nm`. The
   field takes letters, digits and underscores only; a hyphen is refused.
2. Shoot lights and flats as usual. The ASIair appends the token to every frame
   it saves while the field is set; AirWatcher copies the names through
   unchanged.
3. Clear the field when the drawer comes out. The ASIair keeps it set until you
   do, and frames shot without the drawer would be filed under that filter.

What lands, for the example above with the alias `ALP_T_5nm: ALP-T-5nm`:

```
lights/IC1805/WO-UC-108-ASI4400/2026-10-07/ALP-T-5nm/IC1805_2026-10-08T010829Z_0001_WO-UC-108-ASI4400_ALP-T-5nm_30s_g136_o15_bin1_0C.fits
calibration/flat/WO-UC-108-ASI4400/ALP-T-5nm/2026-10-07/Flat_2026-10-08T010109Z_0001_WO-UC-108-ASI4400_ALP-T-5nm_g136_o15_bin1.fits
```

with these cards in each header:

```
FILTER  = 'ALP-T-5nm'          / from capture filename token (nwingest)
SRCFILE = 'Light_IC 1805_30.0s_Bin1_4400MC_gain136_20261007-180900_242deg_0.0C_F_ALP_T_5nm_0001.fit' / original filename as captured
```

Two things the token does not change: a light shorter than
`exclude_lights.min_exposure_s` still goes to `review/short`, and a dark or bias
ignores the token, since the filter is irrelevant to them. To add a filter, add
an alias (or just type a new token; it is used as typed). Changing an alias
later affects new frames only; frames already filed keep their folder and
`FILTER` card.

## Backfill: repairing frames that were filed incomplete

`backfill DIR` walks an archive directory and adds the cards nwingest would have
written at ingest but did not: `FILTER` on lights and flats that have none, and
the SQM cards on lights taken at a configured site with a reading in range. It
never moves or renames anything, skips files that already have everything, and
so changes nothing on a second run. Start with `--dry-run`, which lists each
file and what would be added and writes nothing at all (no header, no database
row, no hook):

```sh
nwingest backfill /astronomy/astro-imaging/lights/NGC7720/WO-UC-108-ASI4400/2026-10-03 --dry-run
nwingest backfill /astronomy/astro-imaging/lights/NGC7720/WO-UC-108-ASI4400/2026-10-03
```

Two things it reports but does not fix. A file that is torn, short, or has no
END card is listed under "needs re-copy" and left alone; the only repair is a
fresh copy from the capture device. `--quarantine-torn` moves such files out of
the archive into `quarantine/<reason>/` under the archive root (names kept,
nothing deleted) so the fresh copies can be ingested in their place. A light without a WCS is flagged, because
nwingest has no plate solver: the WCS on an ASIAir frame comes from the ASIAir
itself, and otherwise from a solver hook. `--hooks` reruns the configured hooks
on each file the backfill touched or still found incomplete, which is how a
`solve-field` hook can fill the WCS in.

Each backfilled frame is also written to `ingest_log` with status `backfill`
and the cards that were added.

## Hooks (external programs)

Anything you want done to a frame after it is filed is a hook. Each is a named
command template with match conditions:

```yaml
hooks:
  - name: plate-solve-lights
    when: { type: light }
    run:  "solve-field --overwrite --no-plots {dest}"
    background: true
    timeout_s: 300
    enabled: true
```

`{dest}` is the final path; every resolve variable (`{object}`, `{filter}`,
`{rig}`, ...) is available too. Details:

- **Argv, not a shell.** The command is split into arguments and each is filled
  in, so a substituted value never reaches a shell. Set `shell: true` on a hook
  if you need pipes or redirects.
- **Foreground vs background.** A foreground hook blocks until it finishes or
  hits `timeout_s` (default 120 s); a `background: true` hook runs detached and
  does not hold up the next frame.
- **Isolated.** A hook that fails, times out, or is missing is logged and
  skipped; it never breaks ingestion or the other hooks.
- **Scope.** Hooks run on filed science frames (lights and calibration), not on
  `review/` or `quarantine/` files.

Hooks are independent and individually toggled, so you add a plate solver, a
notifier, or a stacking trigger without touching the code.

## SQM stamping (optional)

With `sqm.enabled`, each frame is stamped with the sky-brightness reading
nearest its exposure time, pulled from the NightWatcher database, but only when
the frame's own coordinates match one of your configured sites. A rig taken to a
dark site does not get tagged with the observatory's numbers.

## Web UI (optional)

With `extension.register`, the watcher registers with NightWatcher2 and heartbeats
while it runs, so an **Ingest** tab appears in the web UI showing the transfer
history from `ingest_log` (each filed frame: time, target, rig, filter, SQM,
destination, status). Stop the watcher and the tab goes away.

With `events.enabled` (default on), each transfer is also written to NightWatcher's
shared `events` table, so it shows up in the **Events** tab next to the daemon's
own events — a one-line audit per frame. NightWatcher2 itself stays a clean
standalone SQM tool; this is an optional extension it lights up only when present.

## Tests

```sh
python3 -m pytest -q tests/
```

The suite builds tiny FITS frames with the real byte layout, including the torn
copy that broke the 2026-10-03 NGC7720 session, and covers the structural
check, the independent FILTER/SQM steps, the header writer, and the backfill
mode's dry run and idempotence.

## Status

Working end-to-end and deployed. That covers classify/rename/file, the read-only
`plan` mode, the **structural check** (torn and short copies never reach the
archive), the CLEAR filter default, the **SQM stamp** (site-matched, nearest
reading from the NightWatcher database, writing `SQM`/`SQMSRC`/`SQMTIME`/`SQMDT`), the
**hook runner**, the **backfill** mode, per-cycle **summaries**, the **ingest log**
(each filed frame recorded in `ingest_log`), and
the **extension registry** — the watcher registers and heartbeats in NightWatcher while
it runs, so the daemon's `/api/v1/extensions` endpoints light up a live **Ingest** tab
in the [NightWatcher2](https://github.com/DavidGilinsky/NightWatcher2) web UI, and each
transfer is also written to NightWatcher's shared `events` table. The Debian package
(above) installs it with a debconf setup that `dpkg-reconfigure` can re-run.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
