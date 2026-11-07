#!/usr/bin/env bash
# The old name of care.sh (#66), for a schedule set up with it: the same script, the same arguments.
exec "$(dirname "$0")/care.sh" "$@"
