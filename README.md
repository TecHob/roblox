# Roblox Map Architect v0.9.5 — URBANO

## ARQUIVOS A SUBIR — desta vez são DOIS

```
server/app/main.py          →  /home/andre/roblox/mapa/app/main.py
frontend/public/index.html  →  (Netlify / Live Server)
```

O `terrain_engine.py` não mudou. O `index.html` precisa ir porque faltavam
dois controles no frontend (ver seção 0).

---

## 0. O que veio das fotos no Studio

**Cerca no ar.** Muro, portão, cerca viva e caixa de correio ficavam numa
altura fixa em relação ao piso da casa. Como a casa assenta no ponto mais
alto da pegada e a divisa costuma estar mais baixa, tudo isso ficava
pendurado. Agora cada peça é assentada pela altura do terreno sob ela, com
base enterrada de sobra para divisa inclinada.

**Piscina com água falsa.** Era uma placa de vidro sobre um bloco maciço.
Agora a bacia é oca (fundo, quatro paredes, borda em moldura) e o vão é
preenchido com **água de Terrain de verdade** via `FillBlock`.

Este é o primeiro `FillBlock` do gerador desde a v0.7.0, então vale explicar
a diferença: lá o `flattenArea` chamava `FillBlock` de 4 em 4 studs para cada
edifício e cada segmento de rua — milhares de chamadas sem yield, e o Studio
travava. Aqui é **uma chamada por piscina**, vinte no mapa inteiro, com yield
depois de cada uma. A regra de não fazer terraplanagem pelo Lua continua
valendo.

**Cinza malhado nas ruas.** Não era textura: duas vias cruzando deixavam o
topo do leito na mesma cota e as superfícies disputavam o plano. Cada via
recebe agora um deslocamento vertical mínimo, diferente da vizinha.

**Cidade num canto só.** A nota da zona urbana dá +95 por proximidade da
costa, então ela caía na **borda** da ilha e a grelha só podia crescer para
dentro. Agora, escolhida a direção, o centro escorrega para o meio do trecho
construível nos dois eixos. No mapa de teste isso levou o centro de (248, 28)
para (92, 88): a avenida foi de 648 para 960 studs e os lotes de 59 para 128.

**Mato demais.** A limpeza só pegava a beira do leito e sobrava mata alta
entre um quarteirão e outro. Agora some tudo dentro da faixa loteável de
qualquer via — 412 árvores no mapa de teste. Fora dela a vegetação fica, e é
o que separa cidade de campo.

**Nível de detalhe.** Com 128 testadas e casas de 400 a 1.100 peças, o
orçamento acabava com 58 casas e 70 lotes vazios — cidade grande e vazia é
pior que cidade pequena e cheia. As 25 casas do centro mantêm o detalhe
cheio; da periferia para fora entra uma versão econômica (~250 peças) com a
mesma composição de módulos e menos subdivisão. Resultado: **80 casas** em vez
de 58, dentro do mesmo orçamento.

O preset Cidade também baixou a água de 30% para 22% (81% do mapa vira terra)
e a densidade de árvores de 0.006 para 0.004.

Números finais do preset Cidade, medidos sobre o heightmap real:

| | v0.9.5 | v0.9.7 |
|---|---|---|
| avenidas | 3 | 4 |
| avenida principal | 648 studs | 960 studs |
| lotes | 59 | 128 |
| casas | 50 | 80 |
| edifícios | 52 | 82 |
| peças | 40.024 | 50.089 |

As 80 casas passaram na auditoria de fachada.

---

## 0.0 A malha virou grelha de verdade

Até a v0.9.4 a malha era **uma avenida** com transversais curtas. Num mapa de
1024 studs a cidade ocupava um canto e o resto ficava vazio — daí a cara de
zona rural. Agora são avenidas paralelas com o mesmo passo das quadras, e as
transversais atravessam todas elas, fechando quarteirões.

Junto veio um efeito que precisou de segundo ajuste: com 59 testadas e o teto
antigo de 38 casas, o bairro ficou **mais** espalhado que antes — mais rua,
mesma quantidade de casa. O teto e o orçamento de peças passaram a acompanhar
o tamanho da malha.

Mapa plano, preset Cidade, lotes compactos, medido sobre o heightmap real:

| | v0.9.4 | v0.9.5 |
|---|---|---|
| avenidas | 1 | 3 |
| lotes | 42 | 59 |
| casas | 38 | 50 |
| edifícios | 40 | 52 |
| peças | 28.444 | 40.024 |

Auditoria nas 50 casas: todas com a porta mais perto da rua que o centro,
nenhuma a menos de 33 studs de qualquer leito, vizinhas a 28 studs no mínimo,
hotel a 79 studs do vizinho mais próximo.

**Atenção ao tamanho:** 40 mil peças é bastante para colar de uma vez. Com o
preset Cidade no máximo, use o par `setup_world_nocity` + `setup_city`.

### O limite de 200 locais

Ao fechar esta versão o `validate_luau.py` reprovou o `setup_world`:

```
too many local variables (limit is 200) in main function
```

O bloco da cidade cresceu a ponto de estourar o limite de locais por função,
que é duro no Lua e no Luau. O script **não compilaria no Studio**, e não é
erro de digitação nenhum — nenhuma leitura do código pegaria isso. A cidade
passou a rodar dentro de `construirCidade()`, o que devolve os locais para um
escopo novo e deixa o chunk principal com um só.

Vale como regra para as próximas versões: bloco grande vai dentro de função,
não solto no chunk.

---

## 0.0 O preview procedural mostrava o mapa inteiro submerso

Com o preset Cidade (amplitude 0.16, água 30%) o preview aparecia 100% azul,
sem relação nenhuma com o mapa que seria gerado.

O servidor **normaliza e calibra** o heightmap antes de aplicar o nível
d'água — é por isso que o resultado informa "água pedida 30.0%, resultante
29.869%". O preview não fazia nem uma coisa nem outra: calculava
`h *= amplitude` e comparava direto com `waterLevel`. Com amplitude 0.16 e
água em 30%, todo o terreno ficava abaixo da linha. O erro atingia justamente
os presets urbanos, que são de amplitude baixa.

Normalizar min-max sozinho não resolveu (no modo ilha a queda das bordas joga
muita área para o zero: sobravam 63% de água com 30% pedidos). Agora a
calibração é por quantil, como no servidor. Medido em 200×200 amostras:

| preset | pedido | preview antes | preview agora |
|---|---|---|---|
| Cidade | 30% | 100% | 28.4% |
| Subúrbio | 28% | 100% | 26.5% |
| Resort | 25% | ~100% | 23.4% |
| Medieval | 25% | 6.7% | 25.0% |
| Ártico | 20% | — | 20.1% |
| Deserto | 5% | — | 5.3% |

A correção vale para o preview 2D, o 3D procedural e o overlay urbano, que
compartilhavam a mesma conta duplicada em três lugares — agora é uma função só.

---

## 0.0 Preview 3D do relevo real, e o que faltava no frontend

**O preview do modo erosão agora tem 2D/3D.** O 3D monta a malha a partir do
PNG que o servidor devolveu, pintado com o colormap, com a lâmina d'água na
mesma cota que o script Lua usa. É o mesmo relevo que vai entrar no Studio,
não uma reconstrução.

**O overlay urbano estava desenhando no lugar errado.** Ele calculava as
posições com Perlin **do navegador** — não tinha relação nenhuma com o
heightmap erodido que o servidor gerou e que o Lua realmente lê. Por isso os
ícones apareciam sempre naquela grade regular no meio do mapa. Agora o PNG é
lido de verdade (canvas com `crossOrigin`), e se o CORS falhar ele avisa e
cai na aproximação antiga em vez de mentir em silêncio.

**Rótulo de versão.** O cabeçalho mostrava "Urban Planner v0.7.0.1 · PHP
v1.0". Agora mostra a versão do HTML e consulta o `/health` para exibir a
versão da API ao lado — em verde se baterem, laranja se não. Quando um dos
dois arquivos não for substituído, dá para ver na hora.

---

## 0.1 Duas coisas que faltavam no frontend

**O "Platô costeiro" não tinha controle nenhum.** Ele só existia dentro do
preset Resort de Luxo (0.85); em qualquer outro preset ficava em 0. É o
parâmetro que achata a faixa logo acima da água — o que decide quanta área
sobra para lotear. Sem ele a cidade sai com poucas casas por mais plano que o
resto pareça. Agora tem slider próprio na seção TERRENO.

**O `syncUI()` só atualizava dois sliders.** Amplitude e nível d'água eram
refrescados ao trocar de preset; Octaves, Escala, Limiar e Amplitude de
montanha, densidades e erosão ficavam mostrando o valor anterior. O `config`
estava certo e o payload ia certo — **a tela é que mentia**. Pior: bastava
encostar num desses sliders para o valor errado da tela virar o valor de
verdade. Agora todos são sincronizados.

Junto disso, as faixas dos sliders de montanha não alcançavam o que o preset
urbano precisa: Limiar ia só até 0.90 (preciso 0.97) e Amplitude começava em
0.10 (preciso 0.00). Faixas corrigidas para 0.3–1.0 e 0–1.

**Os presets "Cidade" e "Subúrbio" nunca ligaram o gerador de edifícios.**
Eles setavam `urbanEnabled` (o urbanismo do preview Perlin antigo) mas não
`placeBuildings`, nem estilo urbano, nem os parâmetros de rua e lote. Clicar
em Cidade não produzia cidade. Os dois foram reescritos como configurações
urbanas completas:

- **🏙️ Cidade** — mapa plano, montanhas desligadas, platô 1.00, grid,
  lotes compactos, densidade 100%, natureza 0%, raio 480
- **🏘️ Subúrbio** — relevo suave, platô 0.95, traçado orgânico, lotes
  médios, densidade 80%, natureza 20%, raio 420

---

## 1. Casa por composição de módulos

Era esse o gargalo: as três variantes de casa diferiam **só na cor**, então
cinco casas no mapa eram cinco cópias. Agora a casa é montada a partir de
módulos sorteados pela seed do lote:

| eixo | opções |
|---|---|
| planta | I · L · U |
| pavimentos | 1 · 2 |
| telhado | duas águas · quatro águas (quadril) · uma água · mansarda com água-furtada |
| anexo | garagem com portão de enrolar e entrada de carro · varanda lateral · alpendre · nenhum |
| acabamento | lambril · tijolo aparente com fiadas defasadas · reboco com cimalha e cantoneiras · embasamento em pedra |
| esquadria | com postigo · lisa · em arco (aduelas) |
| quintal | piscina com espreguiçadeiras · jardim · deck com guarda-sol · horta com canteiros · nenhum |

São **7.200 combinações de geometria** antes de contar cor, largura e
profundidade — largura e profundidade também são sorteadas.

Medido na bancada, oito casas com seeds vizinhas: 399, 427, 468, 636, 795,
807, 858 e 1097 peças. Nenhuma repetida.

Também entrou o **interior mobiliado**, que estava na lista de pendências
desde a v0.8.5: sofá, mesa de centro, estante, bancada de cozinha com
armários e pia, mesa de jantar com quatro cadeiras. Casa de dois pavimentos
ganha quarto (cama, criado-mudo, guarda-roupa, escrivaninha) e escada interna.

---

## 2. Urbanismo invertido: ruas primeiro, lotes depois

Antes as construções eram largadas em pontos soltos e só depois ligadas por
estradas. Agora:

1. acha a zona urbana (nota por planura, altitude e proximidade da costa)
2. traça a avenida na direção em que o terreno construível se estende mais
3. traça as transversais saindo dela
4. divide a testada das vias em lotes
5. uma casa por lote, com a **fachada voltada para a rua**

Cada lote ganha muro frontal, portão com pilaretes, caminho até a porta
acompanhando o desnível, caixa de correio e cerca viva nas divisas.

**Nada disso toca no terreno.** Zero `FillBlock`, zero `flattenArea`.

Mobiliário urbano: postes com braço alternando de lado, bancos, floreiras,
lixeiras, faixa de pedestre e placa em cada cruzamento, ponto de ônibus.

Números medidos rodando o Lua sobre o **heightmap real** de um mapa plano
(amplitude 0.16, montanhas desligadas, platô costeiro 1.0, raio urbano 480):

| lote | lotes | casas | edifícios | peças |
|---|---|---|---|---|
| compact | 42 | 32 | 34 | 25.049 |
| medium | 29 | 24 | 26 | 19.062 |
| large | 23 | 19 | 21 | 15.333 |

Avenida de 648 studs nos três casos, com 9 transversais. Em terreno de morro
cai para 4 casas em 10 lotes e em terreno muito acidentado para 3 em 3 — o
critério afrouxa, mas não inventa lote onde não cabe.

---

## 3. Quatro bugs corrigidos

### 3.1 A escada nascia para dentro da casa

```lua
escadaAoSolo(m, degrauCasa.Position, degrauCasa.LookVector, ...)
```

Em Roblox `LookVector` é **-Z**. A frente da casa (porta, alpendre) está em
**+Z local**. A escada era construída no sentido oposto: atravessava o
alpendre e entrava na casa. Valia também para o restaurante. Agora usa
`-pe.LookVector`.

Foi por isso que a escada continuou estranha mesmo depois da v0.8.7 abrir o
vão no guarda-corpo — abrir o vão estava certo, mas a escada saía pelo lado
errado.

### 3.2 Os controles de rua e lote nunca funcionaram

O `index.html` envia `road_style`, `lot_size`, `preserve_nature`,
`road_width` e `urban_radius` desde a v0.8.x, mas o `GenerateRequest` não
declarava esses campos e o pydantic os **descartava em silêncio**. Mexer nos
sliders não mudava nada. Agora estão declarados e em uso.

### 3.3 O sorteio das casas estava colapsado

O `frac(sin(x))` clássico não aguenta seed grande: com seeds na casa dos
milhares o argumento do seno passa de 1e8, o double perde os bits baixos e as
escolhas ficam correlacionadas. Oito casas seguidas caíam em **quatro**
combinações, alternando duas a duas.

Trocado por hash inteiro (quadrático módulo primo). Toda a aritmética cabe
exata em double (65521² = 4.3e9, bem abaixo de 2^53), então o resultado é
idêntico no Studio e em conferência offline. Distribuição medida em 20 mil
seeds: uniforme nos sete eixos.

### 3.4 Mato crescendo dentro da rua

A grama do Terrain é decoração volumétrica: cresce **através** de qualquer
peça apoiada no chão, e engolia calçada, meio-fio e caminho. Pintar só a faixa
da rua exigiria mexer em voxel, que é justamente o que trava o Studio (erro
5.2). Solução: `Terrain.Decoration = false` quando há cidade, com a linha para
reverter impressa no Output.

### 3.5 Xadrez cinza no chão não era textura, era z-fighting

Leito de rua, calçada, piso da praça e caminho tinham a face de baixo
**exatamente** na superfície do terreno; as duas superfícies disputavam o
mesmo plano e a placa de vídeo alternava entre elas. Agora essas peças são
grossas com a base enterrada e o topo na altura de antes.

### 3.6 Casa mais larga que o próprio lote

Lote "Compacto" tinha 34 studs de testada e a casa vai até 30 de fachada: a
cerca viva ficava por dentro da parede e as vizinhas encostavam a 22 studs.
As testadas passaram para 38 / 46 / 56 studs e a largura da casa ganhou trava
pelo tamanho do lote. Distância mínima entre vizinhas agora: 28 studs.

### 3.7 Hotel, praça e restaurante amontoados

O afastamento do loteamento (34 studs) serve para casa, não para um hotel de
54 studs de fachada nem para uma praça de 36. Praça e restaurante nasciam
encostados no hotel. Agora cada equipamento reserva a vizinhança (hotel 72,
restaurante 46, praça 44) — mas com parcimônia: em mapa que só produziu 3
testadas a reserva parava, e casa passou a ter prioridade sobre os
equipamentos. Medido no mapa plano: hotel a 79 studs do vizinho mais próximo,
praça a 88.

### 3.8 Transversais espaçadas sem contar o quintal

Só apareceu ao testar num mapa realmente plano, onde a malha fica densa: o
passo entre transversais somava rua + calçada + recuo + meia casa, mas
**esquecia o quintal**, e o multiplicador do estilo `grid` ainda encurtava
mais. Saíam 18 transversais a cada 64 studs, e a rua de trás cortava a
piscina da casa da frente — 8 de 38 casas ficavam com rua dentro do lote.

Agora o passo é `2 × (meia rua + calçada + recuo + meia casa + 26) + rua`, e o
recuo exigido das *outras* vias (a de frente não entra, o próprio offset já
garante) passou de 9 para 26 studs. Auditoria depois da correção: 38 de 38
casas com a porta mais perto da rua que o centro, e nenhuma a menos de 33
studs de qualquer leito.

### 3.9 Teto de casas baixo demais

O mapa plano produziu 45 lotes e o limite antigo construía 18: 27 lotes vazios
justamente onde o terreno era melhor. O teto subiu para
`(4 + densidade×34) × (1 − natureza×0.55)`, com um **orçamento de 32 mil
peças** como trava real — contar casas não basta, porque cada uma custa de 400
a 1.100 peças conforme a combinação sorteada. O Output agora informa o total.

### 3.10 Critério fixo de desnível zerava a cidade

Um limite fixo dava "0 candidatas" em mapa acidentado e o bairro simplesmente
não aparecia. Agora a tolerância é **derivada** do melhor terreno disponível,
com valores separados para via, lote e hotel (a pegada do hotel é de 58 studs
por causa da piscina). Em mapa plano fica no mínimo e o bairro sai alinhado;
em mapa de montanha afrouxa o bastante para a cidade existir.

Junto: fundações passaram a 34 studs de profundidade. Como ficam enterradas,
sobrar altura não custa nada — faltar deixa a casa pendurada no ar de um lado.

---

## 4. Script maior: 72 KB → 118 KB

Se a Command Bar reclamar do tamanho, o pacote traz o mesmo resultado em duas
colagens (o corte é no ponto que já estava mapeado, `if PLACE_BUILDINGS`):

```
setup_world_nocity_<id>.lua    25 KB   terreno, água, decoração, spawn
setup_city_<id>.lua            97 KB   a cidade inteira
```

O `setup_city` se vira sozinho: redetecta o terreno e reaproveita as pastas da
parte 1. Pode rodar de novo quantas vezes quiser para refazer só a cidade,
sem reimportar heightmap. Os dois saem dentro do `map_package_<id>.zip`.

O `metadata` agora traz `script_kb` com o tamanho dos três scripts.

---

## 5. Bancada de teste reescrita

`test_buildings_<id>.lua` usa a **mesma** biblioteca do mapa. Até a v0.8.7 ela
tinha uma cópia congelada das construções da v0.7 e mostrava uma casa que não
existia mais — inútil justamente para o que ela serve.

Agora coloca oito casas lado a lado com seeds diferentes, mais hotel,
restaurante, praça e um trecho de avenida. É o jeito mais rápido de ver se a
composição está produzindo casas diferentes, sem gerar mapa nenhum.

---

## Atualização

```
cd /home/andre/roblox/mapa
sudo systemctl stop roblox-heightmap
cp app/main.py app/main.py.bkp-v087
```
Envie o arquivo, depois:
```
python3 -m py_compile app/main.py
find app -type d -name "__pycache__" -exec rm -rf {} +
sudo systemctl start roblox-heightmap
sleep 3
curl http://localhost:5014/health
```
Espera `"version": "0.9.0"`.

```
python3 app/tools/validate_luau.py $(ls -t output/setup_world_*.lua | head -1)
python3 app/tools/validate_luau.py $(ls -t output/setup_city_*.lua | head -1)
```
Esperado: ~2.490 linhas / 120 KB e ~1.985 linhas / 99 KB.

Importação no Studio continua **Size 1024 / 256 / 1024 · Position 0 / 40 / 0**.

---

## Como foi verificado

Além do `validate_luau.py`, escrevi um mock da API do Roblox (`Vector3`,
`CFrame` com matriz de verdade, `Instance`, `Raycast` sobre um terreno
sintético) e **executei** os scripts fora do Studio, em dois relevos e nos três
tamanhos de lote. Foi assim que apareceram o colapso do sorteio, a escada
invertida e as casas caindo em cima da transversal — nada disso é erro de
sintaxe, então nenhuma validação de compilação pegaria.

Depois disso fiz o mock ler o **heightmap real** produzido pela API, com o
mesmo mapeamento da importação no Studio (Size 1024/256/1024, Position
0/40/0). Foi assim que apareceram os dois problemas do item 3.4 e 3.5, que só
existem quando o terreno é plano o bastante para a malha ficar densa.

Auditoria geométrica confirmou, para toda casa gerada: a porta fica mais perto
do leito da rua que o centro do prédio (fachada correta), o centro fica a 33+
studs de qualquer segmento de rua, e as telhas sobem em Y recuando em Z na
mesma proporção — a interpolação que faltava na v0.7.9.

---

## Pendências

- Hotel e restaurante ainda maciços por dentro (a casa já é oca e mobiliada)
- Zonas ainda não separadas em residencial / comercial / turística
- Estradas ainda podem cruzar faixas estreitas de água
- `index.html` não tem botão para o par `nocity` + `city`; eles saem no ZIP do
  pacote. Se quiser os botões, é uma edição pequena no frontend.


---

## Receita de mapa plano urbano

Testada com o heightmap real. Desde a v0.9.1 basta clicar no preset
**🏙️ Cidade**, que já vem com tudo isto.

| controle | valor |
|---|---|
| Amplitude | 0.16 |
| Nível d'água | 30% |
| Octaves | 3 |
| Escala | 0.008 |
| Limiar de montanhas | 0.97 |
| Amplitude de montanhas | 0.00 |
| Erosão | Leve |
| Platô costeiro | 1.00 |
| Modo Ilha | ligado |
| **Gerar edifícios no mapa** | **marcado** |
| Estilo urbano | Cidade Costeira |
| Densidade urbana | 100% |
| Traçado das ruas | Grid elegante |
| Tamanho dos lotes | Compactos |
| Preservar natureza | 0% |
| **Platô costeiro** | **100%** |
| Largura das ruas | 12 studs |
| Raio urbano | 480 studs |

O relevo inteiro fica em ~30 studs de desnível, o que dá 70% do mapa em terra
e desnível de 3 studs na zona urbana.
