#!/usr/bin/env bash
# Start the Linux server for the HaramMute extension (see README.md).
cd "$(dirname "$(readlink -f "$0")")"
exec .venv/bin/python harammute_linux.py "$@"
