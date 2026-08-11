# SUPER PROMPT — ROBLOX MAP ARCHITECT v0.9.0 → v1.0

> Cole este documento inteiro na primeira mensagem de uma conversa nova.
> Anexe também o ZIP `roblox_v090.zip`.

---

## PROMPT INÍCIO

Você é o desenvolvedor principal do ROBLOX MAP ARCHITECT, um gerador procedural
de mapas para Roblox Studio que já está em produção. Leia todo este documento
antes de escrever qualquer código — ele contém erros já cometidos que não devem
se repetir.

---

# 1. INFRAESTRUTURA

| componente | onde | detalhe |
|---|---|---|
| Frontend | Netlify | `precisao-inova-r-mapa.netlify.app` |
| Frontend local | VS Code Live Server | `127.0.0.1:5500/public/index.html` |
| API Python | VPS Ubuntu | `mapa-ia.hortaconecta.com.br` · porta **5014** |
| API PHP | Hostinger | `precisao.agr.br/apis/roblox/mapa/` |
| Repo | GitHub | `TecHob/roblox` |

**Servidor Python:**
```
/home/andre/roblox/mapa/
├── app/
│   ├── main.py            ← API + geração do Lua (~2900 linhas)
│   ├── terrain_engine.py  ← heightmap, erosão, colormap (709 linhas)
│   └── tools/validate_luau.py
└── output/                ← PNGs e .lua gerados
```

Usuário SSH: `andre`. Virtualenv: **`.venv`** (com ponto). Serviço systemd:
`roblox-heightmap` (já com `enable`, sobe sozinho no boot).

**Ciclo de atualização do backend:**
```bash
cd /home/andre/roblox/mapa
sudo systemctl stop roblox-heightmap
cp app/main.py app/main.py.bkp-vXXX
# enviar o arquivo novo
python3 -m py_compile app/main.py
find app -type d -name "__pycache__" -exec rm -rf {} +
sudo systemctl start roblox-heightmap
sleep 3
curl http://localhost:5014/health
```

---

# 2. REGRAS DE TRABALHO (o usuário pediu explicitamente)

1. **Sempre dizer no início da resposta QUAL arquivo subir.** Normalmente só
   `server/app/main.py`; às vezes também `terrain_engine.py` e/ou `index.html`.
2. O frontend é **um arquivo só**: `public/index.html`, com CSS e JS inline.
3. **Não fazer `git push`** — o trabalho é local até o usuário aprovar.
4. PowerShell do Windows **não aceita `&&`** — um comando por linha.
5. Entregar sempre um ZIP pronto, o usuário substitui os arquivos.
6. **Validar o Lua gerado antes de entregar** (ver seção 4).

---

# 3. ESTADO ATUAL (v0.9.0 URBANO)

## Funciona bem

- Heightmap 16-bit com erosão hidráulica (NumPy + Numba)
- Colormap com as cores **exatas** dos materiais do Roblox
- **Platô costeiro** (`apply_coastal_shelf`) — área construível de 18% p/ 25%
- Árvores, pedras, spawn com plataforma sólida
- **Casa por composição de módulos**: planta I/L/U × 1-2 pavimentos ×
  4 telhados (duas águas / quatro águas / uma água / mansarda com
  água-furtada) × 4 anexos × 4 acabamentos × 3 esquadrias × 5 quintais =
  **7.200 combinações** de geometria antes de cor e proporção. Medido:
  399 a 1097 peças por casa, nenhuma repetida em oito seeds vizinhas
- **Interior mobiliado**: sala, cozinha, jantar; quarto e escada no 2º pavto
- **Urbanismo por loteamento**: zona urbana → avenida → transversais →
  testada dividida em lotes → uma casa por lote com a FACHADA para a rua.
  Muro, portão, caminho, caixa de correio, cerca viva por lote
- **Mobiliário urbano**: postes com braço, bancos, floreiras, lixeiras,
  faixa de pedestre, placa de rua, ponto de ônibus
- Hotel ~655 peças, restaurante ~289, praça, pier
- Asset IDs do Creator Store e kit próprio em `ServerStorage/MapArchitectKit`
- Script partido em dois opcional: `setup_world_nocity` (25 KB) +
  `setup_city` (97 KB), para quando a Command Bar reclamar dos 118 KB
- Bancada `test_buildings` usa a MESMA biblioteca do mapa (oito casas
  lado a lado com seeds diferentes)

## Pendências conhecidas

- Script completo em **118 KB** (o par nocity+city resolve)
- Hotel e restaurante ainda maciços por dentro
- Zonas ainda não separadas em residencial / comercial / turística
- Estradas podem cruzar faixas estreitas de água
- `index.html` não tem botão para o par nocity+city (saem no ZIP do pacote)

# 4. FERRAMENTA OBRIGATÓRIA: validate_luau.py

Está em `app/tools/validate_luau.py`. Compila o Lua gerado com `luac5.4`,
traduzindo antes as construções que o Luau aceita e o Lua padrão não:

| Luau | traduzido para |
|---|---|
| `x += y` | `x = x + y` |
| `for k,v in t do` | `for k,v in pairs(t) do` |
| `continue` | comentado |

```bash
python3 app/tools/validate_luau.py $(ls -t output/setup_world_*.lua | head -1)
```

**Esta ferramenta já encontrou um bug que travava o Studio** e que passou
despercebido por dias:

```lua
local segmentLength = ((4.8, 5.2, 4.4))[segment] * scale
```

Sintaxe Python vazando para dentro do Lua. Em Lua tabela é `{}`, não `()`.
Sempre rode o validador antes de entregar.

---

# 5. ERROS JÁ COMETIDOS — NÃO REPETIR

## 5.1 `+=` e `*=` são VÁLIDOS em Luau

Um assistente anterior removeu todos achando que eram inválidos. **Luau aceita
operadores compostos.** Só o Lua 5.4 padrão não aceita — por isso o validador
traduz antes de checar.

## 5.2 O que travava o Studio era `FillBlock`, não o tamanho

A v0.7.0 travava com 58 KB. Culpa do `flattenArea()` chamando `FillBlock` de 4
em 4 studs para cada edifício e cada segmento de estrada — centenas de
modificações de voxel sem yield.

**A versão atual tem 72 KB e ZERO `FillBlock`, e funciona.** Não modifique
terreno pelo Lua. Se precisar de terreno plano, faça no heightmap
(`apply_coastal_shelf` no `terrain_engine.py`).

## 5.3 Colormap: usar RGB EXATO dos materiais

O Terrain Editor escolhe o material de **cor mais próxima**. Cores "bonitas"
mapeiam errado:

| cor antiga | virava | deveria |
|---|---|---|
| (232,214,165) | **Salt** (branco) | Sand |
| (16,118,178) | **Slate** | Water |
| (150,168,142) | **Pavement** | Rock |

Era o "tom de neve" que o usuário reportou. Use sempre:
```
Grass (106,127,63)  LeafyGrass (115,132,74)  Sand (143,126,95)
Rock (102,108,111)  Water (12,84,92)         Ground (102,92,59)
Snow (195,199,218)  Ice (129,194,224)        Basalt (30,30,37)
```

## 5.4 Ordem no pipeline do terreno importa

`apply_coastal_shelf` precisa vir **depois** de `calibrate_water_area`. Antes
dela, a calibração desloca tudo em ~0.18 e o platô não tem efeito nenhum.

## 5.5 Assentar no ponto MAIS ALTO, não no mais baixo

Assentar no mais baixo + fundação funda enterrava o prédio inteiro. O correto é
assentar no ponto mais alto e deixar a fundação preencher o vão abaixo.

## 5.6 Amostrar o terreno cobrindo TODA a pegada

O raio de amostragem era 32, mas a piscina do hotel ficava a 58 studs. Tudo
além do raio ficava pendurado no ar. Hoje são 16 pontos em raio 50.

## 5.7 SmoothPlastic em superfícies grandes

Qualquer material texturizado (Brick, Slate, Asphalt, WoodPlanks) mostra o
tiling da textura em face grande e vira xadrez. `SmoothPlastic` é o único sem
textura. Use materiais texturizados só em peças pequenas.

## 5.8 Não subir peça ao longo da NORMAL da superfície

O tronco da árvore usava `p + normal * altura`. Em encosta a normal é
inclinada, a base descolava do chão. Use vertical e enterre um pouco.

## 5.9 Cuidado com regras que sobrescrevem condições

A regra da palmeira substituía `treeAllowed` por completo e perdia a checagem
`p.Y > WATER_Y`, então nasciam palmeiras dentro do lago.

## 5.10 Verificar se um elemento não bloqueia outro

A escada da casa existia mas o guarda-corpo do alpendre passava na frente. Ao
adicionar um elemento, confira o que já ocupa aquele espaço.

## 5.11 Suavizar tem efeito colateral

A média móvel que tirou o ziguezague das estradas passou a levantá-las acima do
terreno em encostas. Sempre limite o desvio ao terreno real (`math.clamp`).

## 5.12 LookVector é -Z, e a frente da casa é +Z

`escadaAoSolo(m, cf.Position, cf.LookVector, ...)` construía a escada no
sentido oposto: ela atravessava o alpendre e entrava na casa. A frente
(porta, alpendre) fica em **+Z local**, que é `-LookVector`. Corrigido na
v0.9. Sempre que orientar algo pela fachada, lembrar do sinal.

Consequência prática: `yaw` para uma casa olhar para a rua é
`math.deg(math.atan2(dir.X, dir.Z))` com `dir` apontando da casa PARA a rua.

## 5.13 `frac(sin(x))` colapsa com seed grande

Com seeds na casa dos milhares o argumento do seno passa de 1e8, o double
perde os bits baixos e as escolhas ficam correlacionadas: oito casas seguidas
caíam em **quatro** combinações. Use hash inteiro (quadrático módulo primo,
65521) — cabe exato em double e dá o mesmo resultado no Studio e offline.

## 5.14 Limite fixo de desnível zera a cidade

Critério rígido dava "0 candidatas" em mapa acidentado e o bairro não
aparecia. Derive a tolerância do melhor terreno disponível, com valores
separados para via, lote e hotel (pegada de 58 studs por causa da piscina).

## 5.15 O frontend pode enviar campo que o backend descarta

`road_style`, `lot_size`, `preserve_nature`, `road_width` e `urban_radius`
eram enviados desde a v0.8.x mas não estavam no `GenerateRequest`: o pydantic
descartava em silêncio e os sliders não faziam nada. Ao adicionar controle no
`index.html`, conferir se o campo existe no modelo.

## 5.16 Fundação enterrada de sobra não custa nada

A construção assenta no ponto mais alto da pegada. A saia precisa alcançar o
ponto mais baixo, senão a casa fica pendurada de um lado. Hoje são 34 studs —
como fica enterrada, sobrar é grátis; faltar é bug visível.

## 5.17 Espaçamento de via tem que contar o QUINTAL

O passo entre transversais somava rua + calçada + recuo + meia casa e
esquecia o fundo do lote. Em mapa plano (malha densa) a rua de trás cortava a
piscina da casa da frente. Passo correto:
`2 × (meia rua + calçada + recuo + meia casa + fundo) + rua`, com fundo ≈ 26.

Corolário: bug de dimensionamento urbano só aparece em **terreno plano**, onde
a malha fica densa. Testar só em ilha montanhosa esconde a classe inteira.

## 5.18 Limite de 200 locais por função (Lua e Luau)

`too many local variables (limit is 200) in main function`. Bloco grande vai
DENTRO de uma função, nunca solto no chunk principal. Foi o `validate_luau.py`
que pegou; nenhuma leitura do código pegaria. Hoje a cidade inteira roda em
`construirCidade()`.

## 5.19 Uma avenida só não faz cidade

Malha de uma avenida com transversais curtas ocupa um canto do mapa e deixa o
resto vazio: cara de zona rural. Precisa de avenidas paralelas com o passo das
quadras. E ao aumentar a malha, aumentar junto o teto de casas — senão o
bairro fica MAIS espalhado que antes, com mais rua e a mesma quantidade de casa.

## 5.20 Contar casas não limita o custo

Cada casa custa de 400 a 1.100 peças conforme a combinação sorteada. Um teto
por número de casas não segura o total — use orçamento de peças (hoje 32 mil).

## 5.21 Lote pode cair em cima de outra via

Lote da avenida na altura de uma transversal nasce no meio da rua. Meça a
distância ponto-a-**segmento** de todas as vias e desloque o lote ao longo da
testada em vez de descartá-lo.

---

# 6. PRÓXIMO OBJETIVO

A composição da casa e o loteamento (itens 1 e 2 da ordem antiga) estão
prontos e verificados. O que sobra da lista original:

1. **Interior do hotel e do restaurante** — a casa já é oca e mobiliada;
   esses dois continuam maciços por dentro
2. **Zonas**: residencial, comercial, turística. Hoje a avenida recebe hotel,
   praça, restaurante e ponto de ônibus e o resto é casa. Faltam quarteirão
   comercial com fachadas geminadas, estacionamento com vagas demarcadas,
   quadra
3. **Botões no `index.html`** para o par `nocity` + `city`
4. **Estradas cruzando água** — ponte quando o vão for curto

## 6.1 Como o urbanismo está montado hoje (`LUA_URBAN_PLAN`)

```
pegada(x,z,raio,maxDrop)   amostra 13 pontos, devolve o ponto MAIS ALTO
zona urbana                nota = -desnível -altitude +costa
avenida                    direção de maior alcance construível
transversais               a cada passoT, alcance mínimo 60 studs
lotes                      testada / LOT_W, offset do eixo, com deslocamento
                           em esquina; rejeita se ficar perto de outra via
ocupação                   hotel (pegada 58) → praça → restaurante → ponto
                           de ônibus → casas
reserva                    se buildingCount==0, volta ao modo avulso antigo
```

## 6.2 Como verificar sem abrir o Studio

Existe um mock da API do Roblox usado no desenvolvimento da v0.9 (`Vector3`,
`CFrame` com matriz de verdade, `Instance`, `Raycast` sobre terreno
sintético). Rodar o Lua gerado nele pegou três bugs que nenhuma validação de
sintaxe pegaria: o sorteio colapsado, a escada invertida e as casas em cima
da transversal. Se for mexer em geometria, vale reconstruir o mock.

O `validate_luau.py` continua obrigatório, mas ele só compila: um
`for x in t do ... end` numa linha só passa por ele e quebra na execução.

# 7. PARÂMETROS ATUAIS DO PRESET RESORT

```javascript
resort: {
  waterLevel: 0.25, amplitude: 0.35, octaves: 5, scale: 0.006,
  mountainThreshold: 0.75, mountainAmplitude: 0.18,
  erosion: 'light', coastalShelf: 0.85,
  islandMode: true, placeTrees: true, treeDensity: 0.014,
  placeRocks: true, rockDensity: 0.003, spawnMode: 'beach',
  placeBuildings: true, buildingPreset: 'resort', buildingDensity: 0.6,
  roadStyle: 'coastal', lotSize: 'large', preserveNature: 0.62,
  roadWidth: 12, urbanRadius: 280
}

Os cinco últimos passaram a ter efeito na v0.9 (antes o backend os
descartava). `roadStyle` muda o espaçamento das transversais e o alinhamento
dos lotes: `grid` mais denso, `organic` com recuo variável, `coastal` premia
a direção que acompanha a costa.
```

Importação no Studio: **Size 1024 / 256 / 1024 · Position 0 / 40 / 0**.
Heightmap e Colormap em campos separados (os nomes são parecidos, é fácil
trocar).

---

# 8. NOTA SOBRE O AMBIENTE DO USUÁRIO

O Roblox Studio dele dá `exception while signaling: The Parent property of
StarterPlayer is locked` **mesmo em Baseplate vazio e após reinstalar**. Não
vem do nosso código. Contorno confirmado: abrir um lugar salvo em vez de criar
um novo. Afeta só o Play; todo o pipeline de geração funciona em modo Edit.

Não gaste tempo investigando isso de novo.

---

## PROMPT FIM
