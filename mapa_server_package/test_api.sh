#!/usr/bin/env bash
set -euo pipefail
curl -sS http://127.0.0.1:5014/health | python3 -m json.tool
curl -sS -X POST http://127.0.0.1:5014/generate \
  -H 'Content-Type: application/json' \
  -d '{"seed":42,"resolution":256,"amplitude":0.55,"water_level":0.20,"erosion":"medium","response":"json"}' \
  | python3 -m json.tool
