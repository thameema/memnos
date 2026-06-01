#!/bin/bash
set -e

echo "Starting memnos..."
exec python -m memnos_api.main
