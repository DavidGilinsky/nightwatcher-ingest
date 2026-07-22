# nightwatcher-ingest

A small, config-driven watcher that files raw FITS frames. It watches an
incoming directory, reads each new frame's header, renames it to a standard,
and moves it into an organized archive tree. Optionally it stamps each frame
with a sky-brightness reading from [NightWatcher2](https://github.com/DavidGilinsky),
runs external programs on the result, and shows its activity in the
NightWatcher2 web UI.

It is header-driven on purpose. Three capture apps (NINA, TheSkyX, an ASIair)
produce three different folder layouts and filenames, but they all write a sane
FITS header, so the header is the only thing trusted. Nothing about the naming
scheme is hardcoded; it all lives in one YAML file.

It works as a plain FITS organizer with no SQM and no NightWatcher at all. The
sky-brightness stamping and the web UI tab are optional extras that light up
when you configure them.

## What it does per frame

```
incoming/  ->  read header  ->  classify  ->  rename  ->  file into the tree  ->  hooks
```

- **Classify** the frame type (light, dark, flat, bias, flatdark) from the header.
- **Resolve** the target, rig, filter, night, exposure, gain, offset, binning,
  and temperature into template variables.
- **Rename** using a configurable filename template.
- **File** into a configurable directory structure.
- Light frames that are really focus, slew, or preview shots go to `review/`;
  frames with a broken or missing header go to `quarantine/`. Nothing is deleted.

Example result:

```
lights/M57/Askar185-ASI6200/2026-05-22/CLEAR/M57_2026-05-23T102229Z_0055_Askar185-ASI6200_CLEAR_300s_g100_o50_bin1_0C.fits
```

## Install

```sh
pip install astropy pyyaml          # add PyMySQL too if you enable the SQM stamp
```

Python 3.9+. `astropy` does the FITS work; `pyyaml` reads the config.

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
   groups it correctly.
5. **Hooks** (below).
6. **SQM stamping and the web UI tab** (below).

Templates use `{variable}` placeholders. Available variables:
`{object} {type} {rig} {camera} {night} {filter} {utc} {seq} {exp} {gain}
{offset} {bin} {temp} {site}`. Empty ones collapse cleanly, so a missing filter
never leaves a `__` in the name.

## Run

```sh
nwingest --config /etc/nwingest/nwingest.yaml plan [DIR]   # read-only: show what would happen
nwingest --config /etc/nwingest/nwingest.yaml once         # process incoming once, then exit
nwingest --config /etc/nwingest/nwingest.yaml watch        # poll incoming forever
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

`{dest}` is the final path; every resolve variable is available too. Hooks are
independent and individually toggled, so you add a plate solver, a notifier, or
a trigger for your stacking pipeline without touching the code.

## SQM stamping (optional)

With `sqm.enabled`, each frame is stamped with the sky-brightness reading
nearest its exposure time, pulled from the NightWatcher database, but only when
the frame's own coordinates match one of your configured sites. A rig taken to a
dark site does not get tagged with the observatory's numbers.

## Web UI (optional)

With `extension.register`, the watcher announces itself to NightWatcher2 and
heartbeats while it runs, so an **Ingest** tab appears in the NightWatcher2 web
UI showing recent activity. Stop the watcher and the tab goes away. NightWatcher2
itself stays a clean standalone SQM tool; this is an optional extension it lights
up only when the tool is present.

## Status

The config-driven classify/rename/file core, the read-only `plan` mode, the
CLEAR filter default, and the **SQM stamp** (site-matched, nearest reading from
the NightWatcher database, writing `SQM`/`SQMSRC`/`SQMTIME`/`SQMDT`) are working.
The hook runner, the ingest log, and the web UI registration are stubbed and next.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
