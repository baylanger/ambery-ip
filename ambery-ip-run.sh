#!/bin/sh
DIR="$(dirname "$0")"
. "$DIR/ambery-ip-defaults.sh"
exec python3 "$DIR/ambery-ip.py" "$@"
