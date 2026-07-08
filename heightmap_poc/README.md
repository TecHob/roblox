# Roblox Map Architect — Prova de Conceito de Erosão

Esta prova de conceito replica a fórmula visual do `public/index.html`:

- Perlin determinístico baseado na seed;
- FBM com 6 oitavas;
- ridged noise para montanhas;
- amplitude;
- modo ilha e vulcão quando habilitados.

Depois, aplica erosão hidráulica por partículas em três intensidades. Não altera o frontend, PHP, banco ou `LuaGenerator.php`.

## Instalação

```bash
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
```

## Executar

```bash
python generate_heightmap.py --preset rpg --seed 42 --resolution 256
```

Presets disponíveis: `rpg`, `tropical` e `custom`.

## Saídas

- `*_original.png`: heightmap original em grayscale 16-bit;
- `*_erosion_light.png`: erosão leve;
- `*_erosion_medium.png`: erosão média;
- `*_erosion_strong.png`: erosão intensa;
- `*_preview.png`: visualização colorida apenas para comparação;
- `*_report.json`: tempo e métricas de cada geração.

Os arquivos grayscale são os candidatos para importação no Terrain Editor do Roblox Studio.

## Limite desta PoC

O preview JavaScript usa uma implementação própria de Perlin. O export Luau usa `math.noise`, portanto os dois motores atuais não são matematicamente idênticos. Esta PoC segue o preview do site, pois ele representa o resultado visual apresentado ao usuário.
