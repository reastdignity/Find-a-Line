#!/bin/sh
# Find-a-Line in the browser (Linux): run ./start.sh (needs uv, see setup-for-regular-folk.md)
cd "$(dirname "$0")" || exit 1
exec uv run findaline.py web
