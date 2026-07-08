#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/home/andre/roblox/mapa"
VENV="$APP_DIR/.venv"
SERVICE_FILE="/etc/systemd/system/roblox-heightmap.service"

cd "$APP_DIR"
python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip wheel
"$VENV/bin/pip" install -r requirements.txt

# Pré-compila o Numba para evitar demora na primeira requisição.
"$VENV/bin/python" - <<'PY'
import sys
sys.path.insert(0, '/home/andre/roblox/mapa/app')
from terrain_engine import TerrainConfig, ErosionConfig, generate_base, hydraulic_erosion
cfg = TerrainConfig(seed=1, resolution=64)
h = generate_base(cfg)
hydraulic_erosion(h, ErosionConfig(droplets=10, max_steps=2), 1)
print('Numba pré-compilado com sucesso')
PY

sudo tee "$SERVICE_FILE" >/dev/null <<'UNIT'
[Unit]
Description=Roblox Map Architect Heightmap API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=andre
Group=andre
WorkingDirectory=/home/andre/roblox/mapa
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/andre/roblox/mapa/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 5014 --workers 1
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now roblox-heightmap
sudo systemctl --no-pager --full status roblox-heightmap

echo
echo "Teste local: curl http://127.0.0.1:5014/health"
