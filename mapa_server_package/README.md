# Roblox Map Architect — Heightmap API

Serviço Python separado do backend PHP atual. Porta padrão: `5014`.

## Instalação

Copie o conteúdo desta pasta para:

```text
/home/andre/roblox/mapa
```

Depois execute:

```bash
cd /home/andre/roblox/mapa
chmod +x install.sh test_api.sh
./install.sh
./test_api.sh
```

## Endpoints

- `GET /health`
- `POST /generate`
- `GET /files/{arquivo}`
- documentação automática: `GET /docs`

## Exemplo

```bash
curl -X POST http://127.0.0.1:5014/generate \
  -H 'Content-Type: application/json' \
  -d '{
    "seed": 42,
    "resolution": 256,
    "amplitude": 0.55,
    "water_level": 0.20,
    "island_mode": false,
    "erosion": "medium",
    "response": "json"
  }'
```

Os arquivos ficam em `/home/andre/roblox/mapa/output`.

## Comandos úteis

```bash
sudo systemctl status roblox-heightmap
sudo journalctl -u roblox-heightmap -f
sudo systemctl restart roblox-heightmap
```
