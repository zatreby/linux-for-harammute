#!/usr/bin/env bash
# Start the HaramMute local server for Linux (see README.md).
cd "$(dirname "$(readlink -f "$0")")"
exec .venv/bin/python harammute_linux.py "$@"
