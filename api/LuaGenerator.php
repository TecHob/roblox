<?php
/**
 * LuaGenerator — Gera scripts Lua para Roblox a partir de configuração JSON
 */
class LuaGenerator
{
    private array $config;

    // Tamanhos pré-definidos
    private const SIZES = [
        'tiny'   => [64,  256],
        'small'  => [128, 512],
        'medium' => [256, 1024],
        'large'  => [512, 2048],
        'epic'   => [1024, 4096],
    ];

    public function __construct(array $config)
    {
        $this->config = $config;
    }

    private function val(string $key, mixed $default = null): mixed
    {
        return $this->config[$key] ?? $default;
    }

    private function size(): array
    {
        return self::SIZES[$this->val('size', 'medium')] ?? self::SIZES['medium'];
    }

    /**
     * Gera o script Lua completo
     */
    public function generate(): string
    {
        $size = $this->size();
        $voxels = $size[0];
        $studs  = $size[1];
        $preset = $this->val('preset', 'custom');

        $lua = <<<LUA
--[[ ═══════════════════════════════════════════
    ROBLOX MAP ARCHITECT — Script Auto-Gerado
    Preset: {$preset}
    Tamanho: {$studs}×{$studs} studs ({$voxels}×{$voxels} voxels)
    Seed: {$this->val('seed', 42)}
    Gerado em: {$this->timestamp()}
═══════════════════════════════════════════ ]]

-- ═══ CONFIGURAÇÃO (edite aqui) ═══
local CONFIG = {
    MAP_SIZE_X      = {$voxels},
    MAP_SIZE_Z      = {$voxels},
    MAX_HEIGHT      = 64,
    SEED            = {$this->val('seed', 42)},
    TERRAIN_SCALE   = {$this->val('scale', 0.008)},
    OCTAVES         = {$this->val('octaves', 6)},
    LACUNARITY      = {$this->val('lacunarity', 2.0)},
    GAIN            = {$this->val('gain', 0.5)},
    WATER_LEVEL     = {$this->waterLevelVoxels()},
    AMPLITUDE       = {$this->val('amplitude', 0.7)},
    MOUNTAIN_THRESH = {$this->val('mountainThreshold', 0.6)},
    MOUNTAIN_AMP    = {$this->mountainAmpVoxels()},
    CAVES_ENABLED   = {$this->luaBool('cavesEnabled')},
    CAVE_THRESHOLD  = {$this->val('caveThreshold', 0.65)},
    ISLAND_MODE     = {$this->luaBool('islandMode')},
    VOLCANO         = {$this->luaBool('volcano')},
    RIVERS          = {$this->luaBool('rivers')},
    PLACE_TREES     = {$this->luaBool('placeTrees')},
    TREE_DENSITY    = {$this->val('treeDensity', 0.02)},
    PLACE_ROCKS     = {$this->luaBool('placeRocks')},
    ROCK_DENSITY    = {$this->val('rockDensity', 0.005)},
    CHUNK_SIZE      = 32,
}

-- ═══ SERVIÇOS ═══
local Workspace = game:GetService("Workspace")
local Terrain = Workspace.Terrain

LUA;

        // Adicionar funções de noise
        $lua .= $this->noiseSection();

        // Adicionar sistema de biomas
        $lua .= $this->biomeSection();

        // Adicionar seleção de material
        $lua .= $this->materialSection();

        // Adicionar cálculo de altura
        $lua .= $this->heightSection();

        // Calibração automática do nível da água (garante água mesmo em seeds "secas")
        $lua .= $this->waterCalibrationSection();

        // Main generation loop
        $lua .= $this->mainLoop();

        // Rios
        if ($this->val('rivers', false)) {
            $lua .= $this->riverSection();
        }

        // Assets
        if ($this->val('placeTrees', false)) {
            $lua .= $this->assetSection();
        }

        // Spawn (reposiciona jogador em cima do terreno gerado)
        $lua .= $this->spawnSection();

        // Lighting
        $lua .= $this->lightingSection();

        // Footer
        $lua .= $this->footer();

        return $lua;
    }

    // ─── Helpers ───

    private function timestamp(): string
    {
        return date('Y-m-d H:i:s');
    }

    private function luaBool(string $key): string
    {
        return $this->val($key, false) ? 'true' : 'false';
    }

    private function waterLevelVoxels(): int
    {
        return (int) floor(($this->val('waterLevel', 0.3)) * 64);
    }

    private function mountainAmpVoxels(): int
    {
        return (int) floor(($this->val('mountainAmplitude', 0.4)) * 40);
    }

    // ─── Seções do Script ───

    private function noiseSection(): string
    {
        return <<<'LUA'

-- ═══ NOISE ═══
local function fbm(x, z, cfg)
    local total, amp, freq, maxV = 0, 1, 1, 0
    for i = 1, cfg.OCTAVES do
        total = total + math.noise(x * freq * cfg.TERRAIN_SCALE, z * freq * cfg.TERRAIN_SCALE, cfg.SEED + i * 137) * amp
        maxV = maxV + amp
        amp = amp * cfg.GAIN
        freq = freq * cfg.LACUNARITY
    end
    return total / maxV
end

local function ridgedFbm(x, z, cfg)
    local total, amp, freq, maxV = 0, 1, 1, 0
    for i = 1, 4 do
        local n = math.noise(x * freq * cfg.TERRAIN_SCALE * 0.5, z * freq * cfg.TERRAIN_SCALE * 0.5, cfg.SEED + i * 251 + 5000)
        n = 1 - math.abs(n * 2)
        n = n * n
        total = total + n * amp
        maxV = maxV + amp
        amp = amp * cfg.GAIN
        freq = freq * cfg.LACUNARITY
    end
    return total / maxV
end

LUA;
    }

    private function biomeSection(): string
    {
        $preset = $this->val('preset', 'custom');

        $biomeLogic = match ($preset) {
            'desert'   => '    return "DESERT"',
            'arctic'   => '    local temp = math.noise(x * 0.003, z * 0.003, cfg.SEED + 500) + 0.5
    if temp > 0.5 then return "TUNDRA" else return "ARCTIC" end',
            'volcanic' => '    return "VOLCANIC"',
            'tropical' => '    local moist = math.noise(x * 0.004, z * 0.004, cfg.SEED + 1000) + 0.5
    if moist > 0.5 then return "JUNGLE" else return "PLAINS" end',
            default    => '    local temp = math.noise(x * 0.003, z * 0.003, cfg.SEED + 500) + 0.5
    local moist = math.noise(x * 0.004, z * 0.004, cfg.SEED + 1000) + 0.5
    if temp > 0.7 then
        if moist > 0.5 then return "JUNGLE" else return "DESERT" end
    elseif temp > 0.4 then
        if moist > 0.6 then return "SWAMP"
        elseif moist > 0.3 then return "FOREST"
        else return "PLAINS" end
    else
        if moist > 0.5 then return "TUNDRA" else return "ARCTIC" end
    end',
        };

        return <<<LUA

-- ═══ BIOMAS ═══
local function getBiome(x, z, cfg)
{$biomeLogic}
end

LUA;
    }

    private function materialSection(): string
    {
        $preset = $this->val('preset', 'custom');

        $materialLogic = match ($preset) {
            'volcanic' => '    if nh > 0.6 then return Enum.Material.CrackedLava end
    if nh > 0.4 then return Enum.Material.Basalt end
    return Enum.Material.Rock',
            'desert'   => '    if nh > 0.5 then return Enum.Material.Sandstone end
    return Enum.Material.Sand',
            'arctic'   => '    if biome == "ARCTIC" then
        if slope > 0.3 then return Enum.Material.Glacier end
        return Enum.Material.Ice
    end
    return Enum.Material.Snow',
            default    => '    if biome == "JUNGLE" then return Enum.Material.LeafyGrass
    elseif biome == "DESERT" then return Enum.Material.Sand
    elseif biome == "SWAMP" then return Enum.Material.Mud
    elseif biome == "TUNDRA" then return Enum.Material.Snow
    elseif biome == "ARCTIC" then return Enum.Material.Ice
    else return Enum.Material.Grass end',
        };

        return <<<LUA

-- ═══ MATERIAIS ═══
local function getMaterial(y, maxY, slope, biome, underwater)
    if underwater then
        if y <= 2 then return Enum.Material.Rock end
        return Enum.Material.Sand
    end
    local nh = y / maxY
    if nh > 0.85 then return Enum.Material.Snow end
    if nh > 0.75 then
        if slope > 0.6 then return Enum.Material.Rock end
        return Enum.Material.Snow
    end
    if slope > 0.7 then return Enum.Material.Rock end
    if slope > 0.5 then return Enum.Material.Slate end
{$materialLogic}
end

LUA;
    }

    private function heightSection(): string
    {
        $islandCode = $this->val('islandMode', false) ? '
    if cfg.ISLAND_MODE then
        local nx = (x - cfg.MAP_SIZE_X / 2) / (cfg.MAP_SIZE_X / 2)
        local nz = (z - cfg.MAP_SIZE_Z / 2) / (cfg.MAP_SIZE_Z / 2)
        local dist = math.sqrt(nx * nx + nz * nz)
        h = h * math.max(0, 1 - dist ^ 1.5)
    end' : '';

        $volcanoCode = $this->val('volcano', false) ? '
    if cfg.VOLCANO then
        local vx = (x - cfg.MAP_SIZE_X / 2) / cfg.MAP_SIZE_X
        local vz = (z - cfg.MAP_SIZE_Z / 2) / cfg.MAP_SIZE_Z
        local vDist = math.sqrt(vx * vx + vz * vz)
        if vDist < 0.2 then h = h + (1 - vDist / 0.2) * 20 end
        if vDist < 0.08 then h = h * 0.4 end
    end' : '';

        return <<<LUA

-- ═══ ALTURA ═══
local function getHeight(x, z, cfg)
    local h = (fbm(x, z, cfg) + 1) * 0.5 * cfg.AMPLITUDE * cfg.MAX_HEIGHT
    local ridge = ridgedFbm(x, z, cfg)
    if ridge > cfg.MOUNTAIN_THRESH then
        h = h + (ridge - cfg.MOUNTAIN_THRESH) / (1 - cfg.MOUNTAIN_THRESH) * cfg.MOUNTAIN_AMP
    end{$islandCode}{$volcanoCode}
    return math.clamp(h, 0, cfg.MAX_HEIGHT)
end

local function getSlope(x, z, cfg)
    local h = getHeight(x, z, cfg)
    return math.sqrt((getHeight(x+1, z, cfg) - h)^2 + (getHeight(x, z+1, cfg) - h)^2) / 4
end

-- ═══ RIOS (parâmetros determinísticos — usados por rios E por assets) ═══
local function riverParams(cfg, r)
    local rSeed = cfg.SEED + r * 333
    local startZ = cfg.MAP_SIZE_Z * (0.2 + (math.noise(rSeed * 0.013, 0, 1) + 1) * 0.5 * 0.6)
    local rWidth = 2 + (math.noise(rSeed * 0.017, 2, 3) + 1) * 0.5 * 3
    return rSeed, startZ, rWidth
end

local function isRiverAt(cfg, x, z)
    if not cfg.RIVERS then return false end
    for r = 1, 3 do
        local rSeed, startZ, rWidth = riverParams(cfg, r)
        local off = math.noise(x * 0.02, 0, rSeed) * 25
        local cz = startZ + off
        if math.abs(z - cz) < rWidth then
            return true
        end
    end
    return false
end

-- ═══ ÁREA DE LAGO (média da vizinhança — evita poças isoladas de 1 coluna) ═══
local function isLakeArea(cfg, x, z)
    local sum, samples = 0, 0
    for dx = -4, 4, 4 do
        for dz = -4, 4, 4 do
            local sx, sz = x + dx, z + dz
            if sx >= 0 and sx < cfg.MAP_SIZE_X and sz >= 0 and sz < cfg.MAP_SIZE_Z then
                sum = sum + getHeight(sx, sz, cfg)
                samples = samples + 1
            end
        end
    end
    if samples == 0 then return false end
    return (sum / samples) < cfg.WATER_LEVEL
end

LUA;
    }

    private function waterCalibrationSection(): string
    {
        return <<<'LUA'

-- ═══ CALIBRAÇÃO AUTOMÁTICA DO NÍVEL DA ÁGUA ═══
-- Garante uma proporção mínima de água visível, independente de quão "seca"
-- a combinação seed/preset/tamanho de mapa tenha saído (a distribuição real
-- do ruído FBM não é uniforme, então um WATER_LEVEL fixo pode ficar sempre
-- acima de toda a altura gerada em mapas pequenos ou seeds específicas)
do
    local samples = {}
    local sampleCount = 400
    for i = 1, sampleCount do
        local sx = math.random(0, CONFIG.MAP_SIZE_X - 1)
        local sz = math.random(0, CONFIG.MAP_SIZE_Z - 1)
        table.insert(samples, getHeight(sx, sz, CONFIG))
    end
    table.sort(samples)
    local targetPercentile = 0.18
    local idx = math.max(1, math.floor(sampleCount * targetPercentile))
    local calibratedWater = math.floor(samples[idx])
    if calibratedWater > CONFIG.WATER_LEVEL then
        print("💧 Nível da água recalibrado:", CONFIG.WATER_LEVEL, "->", calibratedWater)
        CONFIG.WATER_LEVEL = calibratedWater
    end
end

LUA;
    }

    private function mainLoop(): string
    {
        $caveCheck = $this->val('cavesEnabled', false)
            ? 'local isCave = math.noise(x*0.05, y*0.05, z*0.05 + CONFIG.SEED + 9999) > CONFIG.CAVE_THRESHOLD
                if not isCave or y == 0 then'
            : 'do';

        return <<<LUA

-- ═══ GERAÇÃO PRINCIPAL ═══
print("🗺️ [MAP GEN] Seed:", CONFIG.SEED, "Size:", CONFIG.MAP_SIZE_X * 4 .. "x" .. CONFIG.MAP_SIZE_Z * 4)
local startTime = tick()
Terrain:Clear()

Terrain.WaterColor = Color3.fromRGB(12, 84, 92)
Terrain.WaterReflectance = 0.8
Terrain.WaterTransparency = 0.3
Terrain.WaterWaveSize = 0.2
Terrain.WaterWaveSpeed = 10
-- Nota: Terrain.Decoration e Terrain.GrassLength são "Not Scriptable" na API do Roblox
-- (só podem ser ajustadas manualmente na janela Properties do Studio, não via script)

local total = CONFIG.MAP_SIZE_X * CONFIG.MAP_SIZE_Z
local done = 0

for x = 0, CONFIG.MAP_SIZE_X - 1 do
    for z = 0, CONFIG.MAP_SIZE_Z - 1 do
        local h = getHeight(x, z, CONFIG)
        local surfH = math.floor(h)
        local slope = getSlope(x, z, CONFIG)
        local biome = getBiome(x, z, CONFIG)
        local lake = surfH < CONFIG.WATER_LEVEL and isLakeArea(CONFIG, x, z)

        for y = 0, math.max(surfH, CONFIG.WATER_LEVEL) do
            local pos = Vector3.new(
                (x - CONFIG.MAP_SIZE_X/2) * 4, y * 4, (z - CONFIG.MAP_SIZE_Z/2) * 4
            )
            if y <= surfH then
                {$caveCheck}
                    local uw = y < CONFIG.WATER_LEVEL and lake
                    local mat = getMaterial(y, CONFIG.MAX_HEIGHT, slope, biome, uw)
                    if y < surfH - 3 then mat = Enum.Material.Rock
                    elseif y < surfH - 1 and mat ~= Enum.Material.Rock then mat = Enum.Material.Ground end
                    Terrain:FillBlock(CFrame.new(pos), Vector3.new(4,4,4), mat)
                end
            elseif y <= CONFIG.WATER_LEVEL and lake then
                Terrain:FillBlock(CFrame.new(pos), Vector3.new(4,4,4), Enum.Material.Water)
            end
        end
        done = done + 1
    end
    if x % 8 == 0 then
        task.wait()
        print(string.format("⏳ %.0f%%", done / total * 100))
    end
end

LUA;
    }

    private function riverSection(): string
    {
        return <<<'LUA'

-- ═══ RIOS ═══
if CONFIG.RIVERS then
    local riverDepth = 3
    for x = 0, CONFIG.MAP_SIZE_X - 1 do
        for z = 0, CONFIG.MAP_SIZE_Z - 1 do
            if isRiverAt(CONFIG, x, z) then
                local h = getHeight(x, z, CONFIG)
                if h > CONFIG.WATER_LEVEL and h < CONFIG.MAX_HEIGHT * 0.55 then
                    local surfH = math.floor(h)
                    local floorY = surfH - riverDepth
                    -- Preenche água do fundo (piso sólido preservado abaixo) até a superfície
                    for y = floorY, surfH do
                        Terrain:FillBlock(
                            CFrame.new(Vector3.new((x-CONFIG.MAP_SIZE_X/2)*4, y*4, (z-CONFIG.MAP_SIZE_Z/2)*4)),
                            Vector3.new(4,4,4), Enum.Material.Water)
                    end
                end
            end
        end
        if x % 16 == 0 then task.wait() end
    end
    print("🌊 Rios gerados")
end

LUA;
    }

    private function assetSection(): string
    {
        $preset = $this->val('preset', 'custom');
        $canopyColor = ($preset === 'tropical' || $preset === 'custom')
            ? '"Forest green"'
            : '"Dark green"';

        return <<<LUA

-- ═══ ASSETS ═══
if CONFIG.PLACE_TREES then
    local assets = Instance.new("Folder")
    assets.Name = "MapAssets"
    assets.Parent = Workspace

    for x = 0, CONFIG.MAP_SIZE_X - 1, 2 do
        for z = 0, CONFIG.MAP_SIZE_Z - 1, 2 do
            local h = getHeight(x, z, CONFIG)
            if h > CONFIG.WATER_LEVEL + 2 and h < CONFIG.MAX_HEIGHT * 0.75 and not isRiverAt(CONFIG, x, z) then
                local biome = getBiome(x, z, CONFIG)
                local density = CONFIG.TREE_DENSITY
                if biome == "FOREST" or biome == "JUNGLE" then density = density * 2.5
                elseif biome == "DESERT" or biome == "ARCTIC" then density = density * 0.1 end

                -- Hash determinístico uniforme via bit32 (evita clusters de noise de baixa frequência)
                local hn = (x * 374761393 + z * 668265263 + (CONFIG.SEED + 7777) * 2147483) % 4294967296
                hn = bit32.bxor(hn, bit32.rshift(hn, 15))
                hn = (hn * 2246822519) % 4294967296
                hn = bit32.bxor(hn, bit32.rshift(hn, 13))
                local rnd = (hn % 2147483647) / 2147483647

                if rnd < math.min(density * 3, 0.55) then
                    local pos = Vector3.new((x-CONFIG.MAP_SIZE_X/2)*4, math.floor(h)*4+2, (z-CONFIG.MAP_SIZE_Z/2)*4)
                    local trunk = Instance.new("Part")
                    trunk.Size = Vector3.new(2, 8+math.random()*6, 2)
                    trunk.CFrame = CFrame.new(pos + Vector3.new(0, trunk.Size.Y/2, 0))
                    trunk.Material = Enum.Material.Wood
                    trunk.BrickColor = BrickColor.new("Reddish brown")
                    trunk.Anchored = true
                    trunk.Parent = assets

                    local canopy = Instance.new("Part")
                    canopy.Shape = Enum.PartType.Ball
                    canopy.Size = Vector3.new(8,8,8) + Vector3.new(1,1,1) * math.random() * 6
                    canopy.CFrame = CFrame.new(pos + Vector3.new(0, trunk.Size.Y + canopy.Size.Y/3, 0))
                    canopy.Material = Enum.Material.Grass
                    canopy.BrickColor = BrickColor.new({$canopyColor})
                    canopy.Anchored = true
                    canopy.Parent = assets
                end
            end
        end
        if x % 16 == 0 then task.wait() end
    end
    print("🌳 Assets colocados")
end

LUA;
    }

    private function spawnSection(): string
    {
        return <<<'LUA'

-- ═══ SPAWN (evita jogador nascer enterrado no terreno) ═══
do
    local spawnH = getHeight(CONFIG.MAP_SIZE_X / 2, CONFIG.MAP_SIZE_Z / 2, CONFIG)
    local spawnY = (spawnH + 3) * 4

    local spawnPoint = Workspace:FindFirstChild("MapSpawn")
    if not spawnPoint then
        spawnPoint = Instance.new("SpawnLocation")
        spawnPoint.Name = "MapSpawn"
        spawnPoint.Size = Vector3.new(6, 1, 6)
        spawnPoint.Anchored = true
        spawnPoint.CanCollide = true
        spawnPoint.TopSurface = Enum.SurfaceType.Smooth
        spawnPoint.Parent = Workspace
    end
    spawnPoint.Position = Vector3.new(0, spawnY, 0)

    -- Desativa outros SpawnLocations antigos pra não conflitar
    for _, obj in ipairs(Workspace:GetChildren()) do
        if obj:IsA("SpawnLocation") and obj ~= spawnPoint then
            obj.Enabled = false
        end
    end

    -- Reposiciona jogadores que já estejam no jogo (evita ficar preso dentro do terreno)
    for _, plr in ipairs(game:GetService("Players"):GetPlayers()) do
        if plr.Character and plr.Character:FindFirstChild("HumanoidRootPart") then
            plr.Character:SetPrimaryPartCFrame(CFrame.new(0, spawnY + 3, 0))
        end
    end
end

LUA;
    }

    private function lightingSection(): string
    {
        $preset = $this->val('lighting', $this->val('preset', 'tropical'));

        return match ($preset) {
            'volcanic' => <<<'LUA'

-- ═══ ILUMINAÇÃO VULCÂNICA ═══
local Lighting = game:GetService("Lighting")
Lighting.Ambient = Color3.fromRGB(30, 15, 10)
Lighting.Brightness = 1.2
Lighting.ClockTime = 18
Lighting.OutdoorAmbient = Color3.fromRGB(80, 40, 20)
local bloom = Instance.new("BloomEffect")
bloom.Intensity = 0.5; bloom.Size = 24; bloom.Threshold = 0.8; bloom.Parent = Lighting
local cc = Instance.new("ColorCorrectionEffect")
cc.TintColor = Color3.fromRGB(255, 180, 120)
cc.Contrast = 0.1; cc.Saturation = 0.3; cc.Parent = Lighting

LUA,
            'arctic' => <<<'LUA'

-- ═══ ILUMINAÇÃO ÁRTICA ═══
local Lighting = game:GetService("Lighting")
Lighting.Ambient = Color3.fromRGB(80, 80, 100)
Lighting.Brightness = 1.8
Lighting.ClockTime = 10
Lighting.FogColor = Color3.fromRGB(200, 210, 230)
Lighting.FogEnd = 2000; Lighting.FogStart = 500

LUA,
            'desert' => <<<'LUA'

-- ═══ ILUMINAÇÃO DESERTO ═══
local Lighting = game:GetService("Lighting")
Lighting.Ambient = Color3.fromRGB(80, 70, 50)
Lighting.Brightness = 3
Lighting.ClockTime = 15
Lighting.OutdoorAmbient = Color3.fromRGB(140, 120, 80)
local atmo = Instance.new("Atmosphere")
atmo.Density = 0.2; atmo.Offset = 0.5; atmo.Haze = 3
atmo.Color = Color3.fromRGB(230, 210, 170); atmo.Parent = Lighting

LUA,
            default => <<<'LUA'

-- ═══ ILUMINAÇÃO ═══
local Lighting = game:GetService("Lighting")
Lighting.Ambient = Color3.fromRGB(50, 60, 50)
Lighting.Brightness = 2.5
Lighting.ClockTime = 14
Lighting.OutdoorAmbient = Color3.fromRGB(100, 120, 100)
local atmo = Instance.new("Atmosphere")
atmo.Density = 0.3; atmo.Offset = 0.25
atmo.Color = Color3.fromRGB(199, 210, 228)
atmo.Haze = 1; atmo.Glare = 0.2; atmo.Parent = Lighting

LUA,
        };
    }

    private function footer(): string
    {
        return <<<'LUA'

print(string.format("✅ [MAP GEN] Concluído em %.1fs!", tick() - startTime))
LUA;
    }
}
