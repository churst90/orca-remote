#!/usr/bin/env bash
# Build a .orca-ext archive from a package-extension directory.
#
# Usage:
#   ./build-orca-ext.sh <package-dir> [output.orca-ext]
#
# Example:
#   ./build-orca-ext.sh ocr ocr.orca-ext
#
# The output is a zip archive whose top-level entries are the
# package's manifest.toml and .py files. Orca's
# `--install-extension` accepts either layout (top-level files, or
# wrapped in a single subdirectory).

set -euo pipefail

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
    echo "usage: $0 <package-dir> [output.orca-ext]" >&2
    exit 2
fi

PKG="$1"

if [ ! -d "$PKG" ]; then
    echo "error: $PKG is not a directory" >&2
    exit 1
fi
if [ ! -f "$PKG/manifest.toml" ]; then
    echo "error: $PKG/manifest.toml is missing" >&2
    exit 1
fi

# Default output name comes from the manifest's [extension].name so
# `./build-orca-ext.sh .` does the right thing without forcing the
# caller to spell out the filename.
if [ "$#" -eq 2 ]; then
    OUT="$2"
else
    NAME="$(awk -F '"' '/^name *=/ {print $2; exit}' "$PKG/manifest.toml")"
    if [ -z "$NAME" ]; then
        echo "error: could not parse extension name from $PKG/manifest.toml" >&2
        exit 1
    fi
    OUT="$NAME.orca-ext"
fi

# Resolve OUT to an absolute path BEFORE we cd, so relative output
# paths land where the user expects.
OUT_ABS="$(realpath -m "$OUT")"

# Use a sorted file list so the archive is deterministic (same
# inputs -> same bytes -> same SHA256).
( cd "$PKG" && find . -type f \
    \! -name '*.pyc' \! -name '*.pyo' \
    \! -path './__pycache__/*' \! -path '*/.*' \
    | sort \
    | zip -X -@ "$OUT_ABS" ) > /dev/null

echo "wrote $OUT_ABS ($(stat -c %s "$OUT_ABS") bytes)"
