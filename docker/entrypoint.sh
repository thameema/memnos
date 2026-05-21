#!/bin/bash
set -e

echo "Starting engram..."
exec python -m engram_api.main
