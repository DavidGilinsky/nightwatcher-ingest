#!/bin/sh
# ============================================================================
#  Build nightwatcher-ingest_<version>_all.deb (pure-Python, Architecture: all).
#  No root needed; dpkg-deb --root-owner-group stamps root:root ownership.
#  Usage: sh packaging/build-deb.sh   (from anywhere)
# ============================================================================
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$here"

PKG=nightwatcher-ingest
VER=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' nwingest.py | head -1)
[ -n "$VER" ] || { echo "cannot read __version__ from nwingest.py" >&2; exit 1; }

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT INT TERM
chmod 0755 "$stage"   # mktemp makes it 0700; the package root (./) must be 0755

# --- payload -----------------------------------------------------------------
install -Dm0755 nwingest.py              "$stage/usr/bin/nwingest"
# Debian policy prefers an explicit interpreter over /usr/bin/env.
sed -i '1s|^#!.*|#!/usr/bin/python3|' "$stage/usr/bin/nwingest"
install -Dm0644 nwingest.example.yaml    "$stage/etc/nwingest/nwingest.yaml"
install -Dm0640 packaging/nwingest.env   "$stage/etc/nwingest/nwingest.env"
install -Dm0644 systemd/nwingest.service "$stage/lib/systemd/system/nwingest.service"
install -Dm0644 README.md                "$stage/usr/share/doc/$PKG/README.md"
install -Dm0644 packaging/copyright       "$stage/usr/share/doc/$PKG/copyright"

# --- control area ------------------------------------------------------------
install -Dm0644 packaging/control   "$stage/DEBIAN/control"
sed -i "s/^Version:.*/Version: $VER/" "$stage/DEBIAN/control"
install -Dm0644 packaging/conffiles "$stage/DEBIAN/conffiles"
install -Dm0755 packaging/postinst  "$stage/DEBIAN/postinst"
install -Dm0755 packaging/prerm     "$stage/DEBIAN/prerm"
install -Dm0755 packaging/postrm    "$stage/DEBIAN/postrm"

# --- build -------------------------------------------------------------------
out="${PKG}_${VER}_all.deb"
dpkg-deb --root-owner-group --build "$stage" "$out"
echo "built $out"
