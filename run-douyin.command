#!/bin/zsh
set -e

SCRIPT_DIR="${0:A:h}"
exec "$SCRIPT_DIR/run.command" --platform douyin "$@"
