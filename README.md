# Roblox Map Architect — Heightmap Beta v0.3.0

## Alterações

- Preview colorido por altitude, inclinação e preset/bioma.
- Metadados visíveis: seed, intensidade, água solicitada e água resultante.
- Botões para voltar/comparar com o preview Perlin.
- Guia de importação no Roblox Studio dentro do resultado.
- API pública configurada em `https://mapa-ia.hortaconecta.com.br`.
- Modo Perlin e fallback continuam intactos.

## Arquivos que precisam ser atualizados

### Servidor Python
Substitua somente:

- `/home/andre/roblox/mapa/app/main.py`
- `/home/andre/roblox/mapa/app/terrain_engine.py`

Depois remova cache e reinicie:

```bash
cd /home/andre/roblox/mapa
sudo systemctl stop roblox-heightmap
cp app/main.py app/main.py.bkp-v02
cp app/terrain_engine.py app/terrain_engine.py.bkp-v02
# envie/substitua os dois arquivos novos
find app -type d -name "__pycache__" -exec rm -rf {} +
sudo systemctl start roblox-heightmap
sleep 3
curl https://mapa-ia.hortaconecta.com.br/health
```

A resposta deve mostrar `version: 0.3.0`.

### Frontend
Substitua somente:

- `public/index.html`

Depois teste com Live Server e publique no Git/Netlify.

```bash
git add public/index.html
git commit -m "melhora preview e guia do heightmap beta"
git push
```
