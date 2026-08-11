from __future__ import annotations

import io
import json
import time
import uuid
import zipfile
from dataclasses import asdict
from pathlib import Path
from typing import Literal

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from PIL import Image

from app.terrain_engine import (
    ErosionConfig,
    TerrainConfig,
    generate_base,
    hydraulic_erosion,
    normalize_preserving_range,
    postprocess_terrain,
    save_png16,
    save_preview,
    save_colormap,
    stats,
)

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
OUTPUT_DIR = ROOT_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Roblox Map Architect Heightmap API", version="0.7.6")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://precisao-inova-r-mapa.netlify.app",
    ],
    allow_origin_regex=r"^http://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


class GenerateRequest(BaseModel):
    seed: int = 42
    resolution: int = Field(default=256, ge=64, le=1024)
    amplitude: float = Field(default=0.55, ge=0.05, le=1.0)
    water_level: float = Field(default=0.20, ge=0.0, le=1.0)
    octaves: int = Field(default=6, ge=1, le=10)
    lacunarity: float = Field(default=2.0, ge=1.1, le=4.0)
    gain: float = Field(default=0.5, ge=0.1, le=0.9)
    mountain_threshold: float = Field(default=0.60, ge=0.0, le=1.0)
    mountain_amplitude: float = Field(default=0.40, ge=0.0, le=1.5)
    island_mode: bool = False
    volcano: bool = False
    erosion: Literal["none", "light", "medium", "strong"] = "medium"
    preset: str = Field(default="custom", max_length=32)
    response: Literal["json", "png", "preview", "package"] = "json"
    import_vertical_size: int = Field(default=256, ge=64, le=1024)
    import_center_x: int = 0
    import_center_y: int = 0
    import_center_z: int = 0
    place_trees: bool = True
    place_rocks: bool = True
    tree_density: float = Field(default=0.02, ge=0.0, le=0.20)
    rock_density: float = Field(default=0.005, ge=0.0, le=0.10)
    decoration_seed: int = 7777
    tree_style: Literal["round", "pine", "palm", "mixed"] = "mixed"
    tree_scale_min: float = Field(default=0.75, ge=0.25, le=3.0)
    tree_scale_max: float = Field(default=1.35, ge=0.25, le=4.0)
    tree_clustered: bool = True
    tree_cluster_strength: float = Field(default=0.55, ge=0.0, le=1.0)
    tree_shore_clearance: float = Field(default=14.0, ge=0.0, le=80.0)
    tree_min_normal_y: float = Field(default=0.82, ge=0.3, le=1.0)
    rock_style: Literal["boulder", "cluster", "mixed"] = "mixed"
    rock_scale_min: float = Field(default=0.65, ge=0.25, le=3.0)
    rock_scale_max: float = Field(default=1.45, ge=0.25, le=4.0)
    rock_clustered: bool = True
    rock_cluster_strength: float = Field(default=0.60, ge=0.0, le=1.0)
    rock_shore_clearance: float = Field(default=3.0, ge=0.0, le=50.0)
    rock_min_normal_y: float = Field(default=0.58, ge=0.2, le=1.0)
    spawn_mode: Literal["auto", "center", "highest", "beach"] = "auto"
    place_buildings: bool = False
    building_preset: Literal["resort", "coastal_city", "mountain_village", "none"] = "none"
    building_density: float = Field(default=0.5, ge=0.1, le=1.0)
    building_seed: int = 31415


def erosion_for(level: str, resolution: int) -> ErosionConfig | None:
    if level == "none":
        return None
    table = {
        "light": ErosionConfig(
            droplets=max(4000, resolution * 25), erode_speed=0.18,
            deposit_speed=0.22, erosion_radius=2,
        ),
        "medium": ErosionConfig(
            droplets=max(9000, resolution * 55), erode_speed=0.30,
            deposit_speed=0.30, erosion_radius=3,
        ),
        "strong": ErosionConfig(
            droplets=max(16000, resolution * 90), erode_speed=0.42,
            deposit_speed=0.38, erosion_radius=3,
        ),
    }
    return table[level]


def build_setup_lua(req: GenerateRequest, job_id: str, mode: str = "full") -> str:
    map_x = req.resolution * 4
    map_z = req.resolution * 4
    include_water = mode == "full"
    clear_only = mode == "clear"
    header = {
        "full": "Pos-importacao: agua, decoracao e spawn",
        "decorations": "Regeneracao somente da decoracao e spawn",
        "clear": "Limpeza somente da decoracao gerada",
    }[mode]
    return f'''--[[ ROBLOX MAP ARCHITECT v0.5.1
{header}
Job: {job_id}
IMPORTANTE: rode pela Command Bar fora do modo Play.
]]
local Terrain = workspace.Terrain
local VOXEL = 4
local MAP_SIZE_X = {map_x}
local MAP_SIZE_Y = {req.import_vertical_size}
local MAP_SIZE_Z = {map_z}
local CENTER = Vector3.new({req.import_center_x}, {req.import_center_y}, {req.import_center_z})
local WATER_LEVEL = {req.water_level:.6f}
local INCLUDE_WATER = {str(include_water).lower()}
local CLEAR_ONLY = {str(clear_only).lower()}
local PLACE_TREES = {str(req.place_trees).lower()}
local PLACE_ROCKS = {str(req.place_rocks).lower()}
local TREE_DENSITY = {req.tree_density:.6f}
local ROCK_DENSITY = {req.rock_density:.6f}
local TERRAIN_SEED = {req.seed}
local DECORATION_SEED = {req.decoration_seed}
local TREE_STYLE = "{req.tree_style}"
local TREE_SCALE_MIN = {min(req.tree_scale_min, req.tree_scale_max):.4f}
local TREE_SCALE_MAX = {max(req.tree_scale_min, req.tree_scale_max):.4f}
local TREE_CLUSTERED = {str(req.tree_clustered).lower()}
local TREE_CLUSTER_STRENGTH = {req.tree_cluster_strength:.4f}
local TREE_SHORE_CLEARANCE = {req.tree_shore_clearance:.2f}
local TREE_MIN_NORMAL_Y = {req.tree_min_normal_y:.4f}
local ROCK_STYLE = "{req.rock_style}"
local ROCK_SCALE_MIN = {min(req.rock_scale_min, req.rock_scale_max):.4f}
local ROCK_SCALE_MAX = {max(req.rock_scale_min, req.rock_scale_max):.4f}
local ROCK_CLUSTERED = {str(req.rock_clustered).lower()}
local ROCK_CLUSTER_STRENGTH = {req.rock_cluster_strength:.4f}
local ROCK_SHORE_CLEARANCE = {req.rock_shore_clearance:.2f}
local ROCK_MIN_NORMAL_Y = {req.rock_min_normal_y:.4f}
local SPAWN_MODE = "{req.spawn_mode}"
local PLACE_BUILDINGS = {str(req.place_buildings).lower()}
local BUILDING_PRESET = "{req.building_preset}"
local BUILDING_DENSITY = {req.building_density:.4f}
local BUILDING_SEED = {req.building_seed}

local minX = CENTER.X - MAP_SIZE_X/2
local minZ = CENTER.Z - MAP_SIZE_Z/2
local maxX = CENTER.X + MAP_SIZE_X/2
local maxZ = CENTER.Z + MAP_SIZE_Z/2

local terrainOnlyParams = RaycastParams.new()
terrainOnlyParams.FilterType = Enum.RaycastFilterType.Include
terrainOnlyParams.FilterDescendantsInstances = {{Terrain}}
terrainOnlyParams.IgnoreWater = true

local detectedMinY = math.huge
local detectedMaxY = -math.huge
local detectedSamples = 0

-- Varre a area configurada procurando terreno
local function scanArea(x0, z0, x1, z1, scanTop, scanDepth)
    local mnY, mxY, n = math.huge, -math.huge, 0
    local stepX = math.max(16, math.floor((x1-x0) / 32))
    local stepZ = math.max(16, math.floor((z1-z0) / 32))
    for x = x0, x1, stepX do
        for z = z0, z1, stepZ do
            local result = workspace:Raycast(
                Vector3.new(x, scanTop, z),
                Vector3.new(0, -scanDepth, 0),
                terrainOnlyParams
            )
            if result and result.Instance == Terrain then
                mnY = math.min(mnY, result.Position.Y)
                mxY = math.max(mxY, result.Position.Y)
                n = n + 1
            end
        end
    end
    return mnY, mxY, n
end

detectedMinY, detectedMaxY, detectedSamples = scanArea(minX, minZ, maxX, maxZ, CENTER.Y + MAP_SIZE_Y * 4, MAP_SIZE_Y * 8)

-- Se nao achou nada, localiza o terreno automaticamente pelos limites reais
if detectedSamples == 0 then
    warn("[Map Architect] Terreno nao encontrado na area configurada. Procurando automaticamente...")
    local ok, extents = pcall(function() return Terrain.MaxExtents end)
    if ok and extents then
        local eMin, eMax = extents.Min, extents.Max
        local sizeX = eMax.X - eMin.X
        local sizeZ = eMax.Z - eMin.Z
        if sizeX > 8 and sizeZ > 8 and sizeX < 20000 and sizeZ < 20000 then
            local newMinX, newMaxX = eMin.X, eMax.X
            local newMinZ, newMaxZ = eMin.Z, eMax.Z
            local scanTop = eMax.Y + 200
            local scanDepth = (eMax.Y - eMin.Y) + 500
            local mnY, mxY, n = scanArea(newMinX, newMinZ, newMaxX, newMaxZ, scanTop, scanDepth)
            if n > 0 then
                minX, maxX, minZ, maxZ = newMinX, newMaxX, newMinZ, newMaxZ
                detectedMinY, detectedMaxY, detectedSamples = mnY, mxY, n
                CENTER = Vector3.new((minX+maxX)/2, CENTER.Y, (minZ+maxZ)/2)
                MAP_SIZE_X = maxX - minX
                MAP_SIZE_Z = maxZ - minZ
                print(string.format("[Map Architect] Terreno localizado automaticamente: %.0f x %.0f studs, centro (%.0f, %.0f).", MAP_SIZE_X, MAP_SIZE_Z, CENTER.X, CENTER.Z))
            end
        end
    end
end

if detectedSamples == 0 then
    error("[Map Architect] Nenhum terreno encontrado no lugar nenhum do Workspace.\\n" ..
          "Verifique se voce:\\n" ..
          "  1. Importou o heightmap PNG no Terrain Editor ANTES de rodar este script\\n" ..
          "  2. Usou Size X=" .. MAP_SIZE_X .. ", Y=" .. MAP_SIZE_Y .. ", Z=" .. MAP_SIZE_Z .. "\\n" ..
          "  3. Usou Position X=0, Y=0, Z=0\\n" ..
          "  4. Clicou no botao Import (nao apenas selecionou os arquivos)")
end
print(string.format("[Map Architect] Terreno detectado: %d amostras, altura de %.1f a %.1f.", detectedSamples, detectedMinY, detectedMaxY))
local detectedHeight = math.max(detectedMaxY - detectedMinY, VOXEL)
local WATER_Y = detectedMinY + detectedHeight * WATER_LEVEL
local minY = detectedMinY - VOXEL * 2

local oldGenerated = workspace:FindFirstChild("GeneratedMap")
if oldGenerated then oldGenerated:Destroy() end
for _, obj in workspace:GetDescendants() do
    if obj:IsA("SpawnLocation") then obj:Destroy() end
end
for _, obj in workspace:GetChildren() do
    if obj:IsA("BasePart") and obj.Name == "Baseplate" then obj:Destroy() end
end
if CLEAR_ONLY then
    print("[Map Architect] Decoracao e spawns removidos.")
    return
end

local function alignedRegion(a, b)
    return Region3.new(a, b):ExpandToGrid(VOXEL)
end
if INCLUDE_WATER then
    print(string.format("[Map Architect] Terreno minY=%.2f maxY=%.2f | agua Y=%.2f", detectedMinY, detectedMaxY, WATER_Y))
    local CHUNK = 128
    for x = minX, maxX - 1, CHUNK do
        for z = minZ, maxZ - 1, CHUNK do
            local x2, z2 = math.min(x + CHUNK, maxX), math.min(z + CHUNK, maxZ)
            local region = alignedRegion(Vector3.new(x, minY, z), Vector3.new(x2, WATER_Y, z2))
            local materials, occupancies = Terrain:ReadVoxels(region, VOXEL)
            local changed = false
            for ix = 1, #materials do
                for iy = 1, #materials[ix] do
                    for iz = 1, #materials[ix][iy] do
                        if materials[ix][iy][iz] == Enum.Material.Air or occupancies[ix][iy][iz] < 0.05 then
                            materials[ix][iy][iz] = Enum.Material.Water
                            occupancies[ix][iy][iz] = 1
                            changed = true
                        end
                    end
                end
            end
            if changed then Terrain:WriteVoxels(region, VOXEL, materials, occupancies) end
            task.wait()
        end
    end
end

local generated = Instance.new("Folder")
generated.Name = "GeneratedMap"
generated.Parent = workspace
local treesFolder = Instance.new("Folder"); treesFolder.Name = "Trees"; treesFolder.Parent = generated
local rocksFolder = Instance.new("Folder"); rocksFolder.Name = "Rocks"; rocksFolder.Parent = generated
local buildingsFolder = Instance.new("Folder"); buildingsFolder.Name = "Buildings"; buildingsFolder.Parent = generated
local infraFolder = Instance.new("Folder"); infraFolder.Name = "Infrastructure"; infraFolder.Parent = generated
local spawnFolder = Instance.new("Folder"); spawnFolder.Name = "Spawn"; spawnFolder.Parent = generated

local raycastParams = RaycastParams.new()
raycastParams.FilterType = Enum.RaycastFilterType.Exclude
raycastParams.FilterDescendantsInstances = {{generated}}
raycastParams.IgnoreWater = false

local TREE_MATERIALS = {{
    [Enum.Material.Grass] = true,
    [Enum.Material.LeafyGrass] = true,
    [Enum.Material.Ground] = true,
}}
local ROCK_MATERIALS = {{
    [Enum.Material.Rock] = true,
    [Enum.Material.Slate] = true,
    [Enum.Material.Ground] = true,
    [Enum.Material.Sand] = true,
    [Enum.Material.Sandstone] = true,
    [Enum.Material.Limestone] = true,
}}

local function hash01(x, z, salt)
    local n = math.sin((x * 12.9898 + z * 78.233 + DECORATION_SEED * 0.013 + salt) * 43758.5453)
    return n - math.floor(n)
end
local function surfaceAt(x, z)
    local result = workspace:Raycast(
        Vector3.new(x, CENTER.Y + MAP_SIZE_Y * 4, z),
        Vector3.new(0, -MAP_SIZE_Y * 8, 0),
        raycastParams
    )
    if not result or result.Instance ~= Terrain or result.Material == Enum.Material.Water then return nil end
    return result
end
local function nearWater(x, z, radius)
    if radius <= 0 then return false end
    for angle = 0, 315, 45 do
        local a = math.rad(angle)
        local result = workspace:Raycast(
            Vector3.new(x + math.cos(a)*radius, WATER_Y + 12, z + math.sin(a)*radius),
            Vector3.new(0, -24, 0),
            raycastParams
        )
        if result and result.Material == Enum.Material.Water then return true end
    end
    return false
end
local function clusterFactor(x, z, salt, strength)
    -- Agrupamento suave: evita manchas muito vazias ou concentrações exageradas.
    local broad = hash01(math.floor(x/96), math.floor(z/96), salt)
    local factor = 0.82 + broad * 0.48
    return 1 + (factor - 1) * strength
end
local function chooseStyle(requested, x, z, salt, choices)
    if requested ~= "mixed" then return requested end
    local index = 1 + math.floor(hash01(x,z,salt) * #choices)
    return choices[math.clamp(index, 1, #choices)]
end
local function createTree(style, p, normal, scale, x, z)
    local model = Instance.new("Model")
    model.Name = style:gsub("^%l", string.upper) .. "Tree"
    model.Parent = treesFolder

    local trunkHeight = (style == "palm" and 13.5 or style == "pine" and 12 or 10) * scale
    local trunk = Instance.new("Part")
    trunk.Name = "Trunk"
    trunk.Anchored = true
    trunk.CanCollide = true
    trunk.CanTouch = false
    trunk.Material = Enum.Material.Wood
    trunk.Color = style == "palm" and Color3.fromRGB(126,88,50) or Color3.fromRGB(103,72,45)
    trunk.Size = style == "palm" and Vector3.new(2.0, trunkHeight, 2.0) or Vector3.new(2.4, trunkHeight, 2.4)

    local yaw = math.rad(hash01(x,z,210)*360)
    local leanX = style == "palm" and math.rad((hash01(x,z,211)-0.5)*8) or 0
    local leanZ = style == "palm" and math.rad((hash01(x,z,212)-0.5)*8) or 0
    trunk.CFrame = CFrame.new(p + normal * (trunk.Size.Y/2)) * CFrame.Angles(leanX, yaw, leanZ)
    trunk.Parent = model

    if style == "pine" then
        for level = 1, 3 do
            local crown = Instance.new("Part")
            crown.Name = "Crown"
            crown.Anchored = true
            crown.CanCollide = false
            crown.CanTouch = false
            crown.Shape = Enum.PartType.Ball
            crown.Material = Enum.Material.LeafyGrass
            crown.Color = Color3.fromRGB(40,105 + level*4,55)
            local s = (12 - level*2) * scale
            crown.Size = Vector3.new(s, s*0.65, s)
            crown.Position = trunk.Position + Vector3.new(0, trunkHeight*0.22 + level*2.6*scale, 0)
            crown.Parent = model
        end
    elseif style == "palm" then
        local crownCenter = trunk.Position + trunk.CFrame.UpVector * (trunkHeight * 0.53)

        -- Copa volumosa: evita que a palmeira pareca um poste.
        local core = Instance.new("Part")
        core.Name = "PalmCrownCore"
        core.Anchored = true
        core.CanCollide = false
        core.CanTouch = false
        core.Shape = Enum.PartType.Ball
        core.Material = Enum.Material.LeafyGrass
        core.Color = Color3.fromRGB(48,126,58)
        core.Size = Vector3.new(5.8,4.2,5.8) * scale
        core.Position = crownCenter
        core.Parent = model

        for bulb = 1, 5 do
            local angle = math.rad((bulb - 1) * 72 + hash01(x,z,215) * 25)
            local bulbPart = Instance.new("Part")
            bulbPart.Name = "PalmCrown"
            bulbPart.Anchored = true
            bulbPart.CanCollide = false
            bulbPart.CanTouch = false
            bulbPart.Shape = Enum.PartType.Ball
            bulbPart.Material = Enum.Material.LeafyGrass
            bulbPart.Color = Color3.fromRGB(
                42 + math.floor(hash01(x,z,216+bulb)*14),
                118 + math.floor(hash01(x,z,222+bulb)*22),
                50 + math.floor(hash01(x,z,228+bulb)*14)
            )
            bulbPart.Size = Vector3.new(4.8,3.3,4.8) * scale
            bulbPart.Position = crownCenter + Vector3.new(math.cos(angle),0.15,math.sin(angle)) * (2.5*scale)
            bulbPart.Parent = model
        end

        for coconut = 1, 4 do
            local angle = math.rad((coconut - 1) * 90 + 25)
            local fruit = Instance.new("Part")
            fruit.Name = "Coconut"
            fruit.Anchored = true
            fruit.CanCollide = false
            fruit.CanTouch = false
            fruit.Shape = Enum.PartType.Ball
            fruit.Material = Enum.Material.Wood
            fruit.Color = Color3.fromRGB(92,62,38)
            fruit.Size = Vector3.new(1.25,1.4,1.25) * scale
            fruit.Position = crownCenter + Vector3.new(math.cos(angle),-1.35,math.sin(angle)) * (1.35*scale)
            fruit.Parent = model
        end

        local leafCount = 10
        for leaf = 1, leafCount do
            local baseAngle = math.rad((leaf - 1) * (360/leafCount) + hash01(x,z,240)*20)
            local direction = Vector3.new(math.cos(baseAngle),0,math.sin(baseAngle))
            local side = Vector3.new(-direction.Z,0,direction.X)

            for segment = 1, 3 do
                local segmentLength
                local segmentWidth
                local distance
                local drop
                local targetDrop
                if segment == 1 then
                    segmentLength = 4.8 * scale
                    segmentWidth = 2.8 * scale
                    distance = 2.6 * scale
                    drop = 0.15 * scale
                    targetDrop = 0.15 * scale
                elseif segment == 2 then
                    segmentLength = 5.2 * scale
                    segmentWidth = 2.5 * scale
                    distance = 6.6 * scale
                    drop = 1.35 * scale
                    targetDrop = 1.15 * scale
                else
                    segmentLength = 4.4 * scale
                    segmentWidth = 1.8 * scale
                    distance = 10.5 * scale
                    drop = 3.7 * scale
                    targetDrop = 2.6 * scale
                end

                local frond = Instance.new("Part")
                frond.Name = "PalmFrond"
                frond.Anchored = true
                frond.CanCollide = false
                frond.CanTouch = false
                frond.Material = Enum.Material.LeafyGrass
                frond.Size = Vector3.new(segmentWidth,0.38*scale,segmentLength)

                local sway = (hash01(x,z,250+leaf)-0.5)*1.1*scale
                local pos = crownCenter + direction*distance + side*sway - Vector3.new(0,drop,0)
                local target = pos + direction*4 - Vector3.new(0,targetDrop,0)
                frond.CFrame = CFrame.lookAt(pos,target)
                frond.Color = Color3.fromRGB(
                    38 + math.floor(hash01(x,z,260+leaf+segment)*16),
                    116 + math.floor(hash01(x,z,280+leaf+segment)*28),
                    47 + math.floor(hash01(x,z,300+leaf+segment)*15)
                )
                frond.Parent = model
            end
        end

        for leaf = 1, 5 do
            local angle = math.rad((leaf-1)*72+18)
            local direction = Vector3.new(math.cos(angle),0,math.sin(angle))
            local topFrond = Instance.new("Part")
            topFrond.Name = "PalmTopFrond"
            topFrond.Anchored = true
            topFrond.CanCollide = false
            topFrond.CanTouch = false
            topFrond.Material = Enum.Material.LeafyGrass
            topFrond.Color = Color3.fromRGB(54,142,65)
            topFrond.Size = Vector3.new(2.3,0.35,5.5)*scale
            local pos = crownCenter + direction*(3.0*scale) + Vector3.new(0,1.05*scale,0)
            topFrond.CFrame = CFrame.lookAt(pos,pos+direction*4+Vector3.new(0,0.5*scale,0))
            topFrond.Parent = model
        end
    else
        local crown = Instance.new("Part")
        crown.Name = "Crown"
        crown.Anchored = true
        crown.CanCollide = false
        crown.CanTouch = false
        crown.Shape = Enum.PartType.Ball
        crown.Material = Enum.Material.LeafyGrass
        crown.Color = Color3.fromRGB(55,120,55)
        crown.Size = Vector3.new(10,9,10) * scale
        crown.Position = trunk.Position + Vector3.new(0, trunkHeight*0.55, 0)
        crown.Parent = model
    end
end
local function createRock(style, p, normal, scale, x, z)
    local model = Instance.new("Model")
    model.Name = "RockFormation"
    model.Parent = rocksFolder
    local pieces = style == "boulder" and 1 or (2 + math.floor(hash01(x,z,300)*2))

    -- Rocha menor e mais achatada; escala final reduzida para não parecer uma bolota gigante.
    local finalScale = scale * 0.72
    for i = 1, pieces do
        local salt = 320 + i*11
        local rock = Instance.new("Part")
        rock.Name = "Rock"
        rock.Anchored = true
        rock.CanCollide = true
        rock.CanTouch = false
        rock.Shape = Enum.PartType.Ball
        rock.Material = hash01(x,z,salt+9) > 0.72 and Enum.Material.Rock or Enum.Material.Slate
        local shade = 83 + math.floor(hash01(x,z,salt)*24)
        rock.Color = Color3.fromRGB(shade,shade+2,shade+5)
        local sx = (3.2 + hash01(x,z,salt+1)*3.8) * finalScale
        local sy = (1.25 + hash01(x,z,salt+2)*1.7) * finalScale
        local sz = (3.2 + hash01(x,z,salt+3)*3.8) * finalScale
        rock.Size = Vector3.new(sx,sy,sz)
        local spread = style == "boulder" and 0 or 3.6*finalScale
        local offset = Vector3.new((hash01(x,z,salt+4)-0.5)*spread,0,(hash01(x,z,salt+5)-0.5)*spread)
        rock.CFrame = CFrame.new(p + offset + normal*(sy*0.28)) * CFrame.Angles(
            math.rad((hash01(x,z,salt+6)-0.5)*18),
            math.rad(hash01(x,z,salt+7)*180),
            math.rad((hash01(x,z,salt+8)-0.5)*18)
        )
        rock.Parent = model
    end
end

local treeCount, rockCount = 0, 0
local STEP = 20
for x = minX + STEP/2, maxX - STEP/2, STEP do
    for z = minZ + STEP/2, maxZ - STEP/2, STEP do
        local result = surfaceAt(x,z)
        if result then
            local p, normalY, material = result.Position, result.Normal.Y, result.Material
            local altitude = math.clamp((p.Y-detectedMinY)/detectedHeight,0,1)
            local closeToWater = nearWater(x,z,22)
            local veryCloseToWater = nearWater(x,z,12)

            local treeChance = math.min(TREE_DENSITY * 6.2, 0.28)
            local rockChance = math.min(ROCK_DENSITY * 8.0, 0.16)
            if TREE_CLUSTERED then treeChance *= clusterFactor(x,z,401,TREE_CLUSTER_STRENGTH) end
            if ROCK_CLUSTERED then rockChance *= clusterFactor(x,z,402,ROCK_CLUSTER_STRENGTH) end

            local style = TREE_STYLE
            if style == "mixed" then
                if material == Enum.Material.Sand and closeToWater then
                    style = hash01(x,z,411) < 0.82 and "palm" or "round"
                elseif altitude > 0.68 then
                    style = hash01(x,z,411) < 0.72 and "pine" or "round"
                else
                    style = hash01(x,z,411) < 0.78 and "round" or "pine"
                end
            end

            local treeAllowed = TREE_MATERIALS[material] and normalY >= TREE_MIN_NORMAL_Y and p.Y > WATER_Y + 5
            if style == "palm" then
                -- Palmeira só em praia/faixa costeira relativamente plana.
                treeAllowed = (material == Enum.Material.Sand or material == Enum.Material.Ground or material == Enum.Material.Grass)
                    and closeToWater
                    and not veryCloseToWater
                    and normalY >= math.max(TREE_MIN_NORMAL_Y,0.90)
                    and altitude < 0.48
                treeChance *= 0.76
            else
                treeAllowed = treeAllowed and not nearWater(x,z,TREE_SHORE_CLEARANCE)
                if style == "pine" then
                    treeAllowed = treeAllowed and altitude >= 0.28
                end
            end

            -- Pedras continuam possíveis em montanhas, mas a chance cai muito nos cumes.
            if altitude > 0.82 then
                rockChance *= 0.22
            elseif altitude > 0.68 then
                rockChance *= 0.58
            end
            local canRock = PLACE_ROCKS and ROCK_MATERIALS[material] and normalY >= ROCK_MIN_NORMAL_Y and p.Y > WATER_Y + 2 and not nearWater(x,z,ROCK_SHORE_CLEARANCE)

            if PLACE_TREES and treeAllowed and hash01(x,z,410) < treeChance then
                local scale = TREE_SCALE_MIN + (TREE_SCALE_MAX-TREE_SCALE_MIN)*hash01(x,z,412)
                if style == "palm" then scale *= 0.92 end
                createTree(style,p,result.Normal,scale,x,z)
                treeCount += 1
            elseif canRock and hash01(x,z,420) < rockChance then
                local rockStyle = chooseStyle(ROCK_STYLE,x,z,421,{{"boulder","cluster"}})
                local scale = ROCK_SCALE_MIN + (ROCK_SCALE_MAX-ROCK_SCALE_MIN)*hash01(x,z,422)
                createRock(rockStyle,p,result.Normal,scale,x,z)
                rockCount += 1
            end
        end
    end
end

-- ═══ COMPACT BUILDINGS v0.7.1 ═══
local buildingCount = 0
if PLACE_BUILDINGS and BUILDING_PRESET ~= "none" then
    local function bhash(x,z,s) local n=math.sin((x*17.37+z*43.56+BUILDING_SEED*0.007+s)*43758.55); return n-math.floor(n) end
    local function mp(n,par,sz,cf,mat,col,tr)
        local p=Instance.new("Part"); p.Name=n; p.Anchored=true; p.Size=sz; p.CFrame=cf; p.Material=mat; p.Color=col
        if tr and tr>0 then p.Transparency=tr end; p.Parent=par; return p
    end
    local function ml(par,pos,col,rng,br)
        local lp=Instance.new("Part"); lp.Name="L"; lp.Anchored=true; lp.Shape=Enum.PartType.Ball
        lp.Size=Vector3.new(2,2,2); lp.Material=Enum.Material.Neon; lp.Color=col; lp.Position=pos; lp.Parent=par
        local pl=Instance.new("PointLight"); pl.Color=col; pl.Range=rng; pl.Brightness=br; pl.Parent=lp
    end
    local function mw(n,par,sz,cf,mat,col)
        local p=Instance.new("WedgePart"); p.Name=n; p.Anchored=true; p.Size=sz; p.CFrame=cf
        p.Material=mat; p.Color=col; p.Parent=par; return p
    end
    local CC=Enum.Material.Concrete; local GL=Enum.Material.Glass; local WP=Enum.Material.WoodPlanks
    local BR=Enum.Material.Brick; local WD=Enum.Material.Wood; local SL=Enum.Material.Slate
    local wC=Color3.fromRGB(232,228,220); local gC=Color3.fromRGB(125,180,222); local cC=Color3.fromRGB(195,190,182)
    local trimC=Color3.fromRGB(96,88,78)

    -- Hotel com varandas individuais por quarto e pilares verticais, para a
    -- fachada nao ficar sendo uma parede lisa de 56 studs.
    local function mkHotel(bp,yaw)
        local m=Instance.new("Model"); m.Name="Hotel"; m.Parent=buildingsFolder
        local r=CFrame.new(bp)*CFrame.Angles(0,math.rad(yaw),0)
        local bW,bD,flH,fl=56,26,9,4; local tH=fl*flH
        local ROOMS=6
        -- Terreo em pedra, corpo claro (base escura assenta o predio)
        mp("Base",m,Vector3.new(bW+7,1.5,bD+7),r*CFrame.new(0,-0.75,0),SL,Color3.fromRGB(122,118,112),0)
        mp("Ground",m,Vector3.new(bW,flH,bD),r*CFrame.new(0,flH/2,0),SL,Color3.fromRGB(158,152,144),0)
        for f=1,fl-1 do
            local y=f*flH
            mp("F"..f,m,Vector3.new(bW,flH-0.6,bD),r*CFrame.new(0,y+flH/2,0),CC,wC,0)
            -- Laje saliente separando os andares
            mp("Slab"..f,m,Vector3.new(bW+2.5,0.55,bD+2.5),r*CFrame.new(0,y,0),CC,Color3.fromRGB(206,200,190),0)
            -- Quartos: cada um com sua janela e varanda propria
            for q=0,ROOMS-1 do
                local qx=-bW/2+3.5+q*((bW-7)/(ROOMS-1))
                mp("WinF",m,Vector3.new(4.6,flH-4,0.5),r*CFrame.new(qx,y+flH/2+0.4,bD/2+0.2),GL,gC,0.3)
                mp("WinB",m,Vector3.new(4.6,flH-4,0.5),r*CFrame.new(qx,y+flH/2+0.4,-bD/2-0.2),GL,gC,0.3)
                mp("BalFloor",m,Vector3.new(5.6,0.35,3.2),r*CFrame.new(qx,y+0.45,bD/2+1.6),CC,Color3.fromRGB(214,208,198),0)
                mp("BalRail",m,Vector3.new(5.6,1.15,0.22),r*CFrame.new(qx,y+1.1,bD/2+3.1),GL,Color3.fromRGB(178,206,228),0.25)
                mp("BalSideL",m,Vector3.new(0.22,1.15,3.2),r*CFrame.new(qx-2.7,y+1.1,bD/2+1.6),CC,Color3.fromRGB(214,208,198),0)
            end
            -- Pilares entre os quartos
            for q=0,ROOMS-2 do
                local px=-bW/2+3.5+(q+0.5)*((bW-7)/(ROOMS-1))
                mp("Pil",m,Vector3.new(1.6,flH-0.6,0.7),r*CFrame.new(px,y+flH/2,bD/2+0.3),CC,Color3.fromRGB(246,242,234),0)
            end
        end
        -- Cobertura com platibanda
        mp("Roof",m,Vector3.new(bW+3,0.9,bD+3),r*CFrame.new(0,tH+0.45,0),CC,Color3.fromRGB(178,172,164),0)
        for sz=-1,1,2 do
            mp("Parapet",m,Vector3.new(bW+3,1.6,0.6),r*CFrame.new(0,tH+1.6,sz*(bD/2+1.2)),CC,Color3.fromRGB(214,208,198),0)
        end
        for sx=-1,1,2 do
            mp("ParapetS",m,Vector3.new(0.6,1.6,bD+3),r*CFrame.new(sx*(bW/2+1.2),tH+1.6,0),CC,Color3.fromRGB(214,208,198),0)
        end
        mp("Penthouse",m,Vector3.new(16,4.5,11),r*CFrame.new(0,tH+3.2,0),CC,Color3.fromRGB(228,222,212),0)
        mp("PentGlass",m,Vector3.new(13,3.2,0.4),r*CFrame.new(0,tH+3.2,5.7),GL,gC,0.28)
        -- ENTRADA: porte-cochere com colunas
        mp("Lobby",m,Vector3.new(20,7,9),r*CFrame.new(0,3.5,bD/2+4.5),SL,Color3.fromRGB(168,162,154),0)
        mp("LGlass",m,Vector3.new(17,5.2,0.5),r*CFrame.new(0,3.6,bD/2+9.2),GL,gC,0.25)
        mp("LDoor",m,Vector3.new(6,5,0.6),r*CFrame.new(0,2.5,bD/2+9.4),GL,Color3.fromRGB(58,76,90),0.12)
        mp("Canopy",m,Vector3.new(26,0.6,11),r*CFrame.new(0,7.6,bD/2+6),CC,Color3.fromRGB(150,44,40),0)
        for sx=-1,1,2 do
            mp("Col",m,Vector3.new(1.3,7.6,1.3),r*CFrame.new(sx*11,3.8,bD/2+10.5),CC,Color3.fromRGB(244,240,232),0)
        end
        mp("Sign",m,Vector3.new(13,2.2,0.35),r*CFrame.new(0,6.6,bD/2+9.5),Enum.Material.Neon,Color3.fromRGB(255,218,130),0)
        -- Escadaria de acesso
        for st=0,2 do
            mp("Stair",m,Vector3.new(22-st*1.5,0.45,1.8),r*CFrame.new(0,0.22+st*0.45,bD/2+13.5-st*1.6),SL,Color3.fromRGB(186,180,172),0)
        end
        -- AREA DE PISCINA
        local po=r*CFrame.new(bW/2+16,0,0)
        mp("PDeck",m,Vector3.new(26,0.4,20),po*CFrame.new(0,0.2,0),SL,Color3.fromRGB(212,204,192),0)
        mp("PRim",m,Vector3.new(20,0.6,14),po*CFrame.new(0,0.4,0),CC,Color3.fromRGB(238,234,226),0)
        mp("PWater",m,Vector3.new(18,0.9,12),po*CFrame.new(0,0.35,0),GL,Color3.fromRGB(38,148,208),0.16)
        for ch=-1,1 do
            mp("Lounge",m,Vector3.new(2.2,0.4,5),po*CFrame.new(ch*4.5,0.6,8.5),CC,Color3.fromRGB(240,236,228),0)
            mp("Parasol",m,Vector3.new(0.25,4,0.25),po*CFrame.new(ch*4.5,2.4,10.5),WD,Color3.fromRGB(120,96,72),0)
            mp("ParasolTop",m,Vector3.new(5,0.3,5),po*CFrame.new(ch*4.5,4.4,10.5),Enum.Material.Fabric,Color3.fromRGB(220,206,168),0)
        end
        ml(m,(r*CFrame.new(0,tH+5,0)).Position,Color3.fromRGB(255,232,188),48,1.3)
        ml(m,(r*CFrame.new(0,5,bD/2+11)).Position,Color3.fromRGB(255,220,164),22,0.85)
        ml(m,(po*CFrame.new(0,3,0)).Position,Color3.fromRGB(120,196,255),20,0.6)
        task.wait()
    end

    -- Casa com telhado de duas aguas, molduras e detalhes que quebram as
    -- superficies grandes (parede lisa e o que da aspecto de caixa no Roblox).
    local function mkHouse(bp,yaw,v)
        local m=Instance.new("Model"); m.Name="Casa"; m.Parent=buildingsFolder
        local r=CFrame.new(bp)*CFrame.Angles(0,math.rad(yaw),0)
        local hW,hD,hH=20,15,7.5
        local wCol = v==1 and Color3.fromRGB(238,232,220)
                  or v==2 and Color3.fromRGB(214,198,172)
                  or Color3.fromRGB(196,186,172)
        local roofCol = v==1 and Color3.fromRGB(112,58,48)
                     or v==2 and Color3.fromRGB(78,74,72)
                     or Color3.fromRGB(96,64,52)
        -- Fundacao com beiral
        mp("Base",m,Vector3.new(hW+3,1.2,hD+3),r*CFrame.new(0,-0.6,0),SL,Color3.fromRGB(128,124,118),0)
        -- Corpo
        mp("Walls",m,Vector3.new(hW,hH,hD),r*CFrame.new(0,hH/2,0),BR,wCol,0)
        -- Cantoneiras verticais (quebram a parede lisa)
        for sx=-1,1,2 do for sz=-1,1,2 do
            mp("Corner",m,Vector3.new(1.1,hH,1.1),r*CFrame.new(sx*(hW/2-0.4),hH/2,sz*(hD/2-0.4)),CC,Color3.fromRGB(244,240,232),0)
        end end
        -- Faixa horizontal no meio da parede
        mp("Belt",m,Vector3.new(hW+0.4,0.5,hD+0.4),r*CFrame.new(0,hH*0.52,0),CC,Color3.fromRGB(244,240,232),0)
        -- TELHADO DE DUAS AGUAS
        local rh=4.2
        for sz=-1,1,2 do
            mw("Roof",m,Vector3.new(hW+3,rh,hD/2+1.6),
               r*CFrame.new(0,hH+rh/2,sz*(hD/4+0.8))*CFrame.Angles(0,sz>0 and 0 or math.pi,0),SL,roofCol)
        end
        -- Cumeeira
        mp("Ridge",m,Vector3.new(hW+3.4,0.5,1),r*CFrame.new(0,hH+rh,0),SL,Color3.fromRGB(70,66,64),0)
        -- Beiral frontal e traseiro
        for sz=-1,1,2 do
            mp("Eave",m,Vector3.new(hW+3.4,0.4,1.2),r*CFrame.new(0,hH+0.2,sz*(hD/2+1.4)),SL,Color3.fromRGB(70,66,64),0)
        end
        -- Chamine
        mp("Chimney",m,Vector3.new(2,5,2),r*CFrame.new(hW/2-3,hH+2.5,-hD/4),BR,Color3.fromRGB(126,84,68),0)
        mp("ChimTop",m,Vector3.new(2.6,0.5,2.6),r*CFrame.new(hW/2-3,hH+5,-hD/4),SL,Color3.fromRGB(70,66,64),0)
        -- JANELAS com moldura
        local function win(cf,wid,hei)
            mp("WinFrame",m,Vector3.new(wid+0.8,hei+0.8,0.5),cf,WD,trimC,0)
            mp("WinGlass",m,Vector3.new(wid,hei,0.6),cf*CFrame.new(0,0,0.08),GL,gC,0.3)
            mp("WinBarV",m,Vector3.new(0.22,hei,0.7),cf*CFrame.new(0,0,0.1),WD,trimC,0)
            mp("WinBarH",m,Vector3.new(wid,0.22,0.7),cf*CFrame.new(0,0,0.1),WD,trimC,0)
        end
        win(r*CFrame.new(4.5,hH*0.58,hD/2+0.2),4.5,3.4)
        win(r*CFrame.new(-5.5,hH*0.58,hD/2+0.2),3.4,3.4)
        win(r*CFrame.new(hW/2+0.2,hH*0.58,-2)*CFrame.Angles(0,math.rad(90),0),4.5,3.4)
        win(r*CFrame.new(-hW/2-0.2,hH*0.58,2)*CFrame.Angles(0,math.rad(-90),0),4.5,3.4)
        -- PORTA com batente e degrau
        mp("DoorFrame",m,Vector3.new(4,6.2,0.6),r*CFrame.new(-1,3.1,hD/2+0.2),WD,trimC,0)
        mp("Door",m,Vector3.new(3.2,5.6,0.4),r*CFrame.new(-1,2.8,hD/2+0.45),WD,Color3.fromRGB(104,66,42),0)
        mp("Knob",m,Vector3.new(0.35,0.35,0.35),r*CFrame.new(0.2,2.9,hD/2+0.7),Enum.Material.Metal,Color3.fromRGB(196,178,120),0)
        mp("Step",m,Vector3.new(5,0.5,1.6),r*CFrame.new(-1,0.25,hD/2+1.4),SL,Color3.fromRGB(146,142,136),0)
        -- Alpendre com colunas
        mp("PorchRoof",m,Vector3.new(9,0.4,4),r*CFrame.new(-1,6.4,hD/2+2.4),SL,roofCol,0)
        for sx=-1,1,2 do
            mp("Column",m,Vector3.new(0.7,6.2,0.7),r*CFrame.new(-1+sx*3.8,3.1,hD/2+4),CC,Color3.fromRGB(242,238,230),0)
        end
        -- Deck de madeira
        mp("Deck",m,Vector3.new(hW+2,0.35,5),r*CFrame.new(0,0.18,hD/2+3),WP,Color3.fromRGB(150,110,72),0)
        -- Piscina com borda
        if v~=3 then
            mp("PoolRim",m,Vector3.new(12,0.5,8),r*CFrame.new(2,0.25,-hD/2-5),SL,Color3.fromRGB(206,200,190),0)
            mp("PoolWater",m,Vector3.new(10,0.7,6),r*CFrame.new(2,0.3,-hD/2-5),GL,Color3.fromRGB(42,155,212),0.18)
        end
        ml(m,(r*CFrame.new(-1,5.6,hD/2+3.6)).Position,Color3.fromRGB(255,216,150),15,0.65)
        task.wait()
    end

    local function mkRest(bp,yaw)
        local m=Instance.new("Model"); m.Name="Restaurante"; m.Parent=buildingsFolder
        local r=CFrame.new(bp)*CFrame.Angles(0,math.rad(yaw),0)
        mp("Body",m,Vector3.new(20,5.5,14),r*CFrame.new(0,2.75,0),CC,Color3.fromRGB(232,225,212),0)
        mp("Roof",m,Vector3.new(22,0.4,16),r*CFrame.new(0,5.7,0),CC,cC,0)
        mp("Glass",m,Vector3.new(18,4,0.4),r*CFrame.new(0,3.2,7.15),GL,gC,0.28)
        mp("Terrace",m,Vector3.new(22,0.25,10),r*CFrame.new(0,0.12,12),WP,Color3.fromRGB(148,108,70),0)
        mp("Awning",m,Vector3.new(22,0.2,10),r*CFrame.new(0,4.5,12),Enum.Material.Fabric,Color3.fromRGB(178,52,42),0)
        ml(m,(r*CFrame.new(0,5,12)).Position,Color3.fromRGB(255,198,128),20,0.8)
        task.wait()
    end

    local function mkPier(bp,dir,wY)
        local m=Instance.new("Model"); m.Name="Pier"; m.Parent=buildingsFolder
        local d=dir.Unit; local s=Vector3.new(-d.Z,0,d.X); local dH=wY+3
        for seg=0,4 do
            local sp=bp+d*(seg*12+6)+Vector3.new(0,dH,0)
            local cf=CFrame.lookAt(sp,sp+d)*CFrame.Angles(0,math.rad(90),0)
            mp("D"..seg,m,Vector3.new(7,0.45,12),cf,WP,Color3.fromRGB(142,105,65),0)
        end
        ml(m,bp+d*60+Vector3.new(0,dH+3,0),Color3.fromRGB(255,208,138),22,0.9)
        task.wait()
    end

    local function mkPlaza(bp)
        local m=Instance.new("Model"); m.Name="Praca"; m.Parent=infraFolder
        mp("Floor",m,Vector3.new(36,0.3,36),CFrame.new(bp+Vector3.new(0,0.15,0)),CC,cC,0)
        mp("Fount",m,Vector3.new(7,1.8,7),CFrame.new(bp+Vector3.new(0,0.9,0)),CC,Color3.fromRGB(218,213,203),0)
        mp("Water",m,Vector3.new(5.5,0.3,5.5),CFrame.new(bp+Vector3.new(0,1.95,0)),GL,Color3.fromRGB(48,152,208),0.2)
        for i=0,3 do
            local a=math.rad(i*90+45)
            local lp=bp+Vector3.new(math.cos(a)*15,0,math.sin(a)*15)
            mp("Lamp"..i,m,Vector3.new(0.3,7,0.3),CFrame.new(lp+Vector3.new(0,3.5,0)),Enum.Material.Metal,Color3.fromRGB(68,63,58),0)
            ml(m,lp+Vector3.new(0,7.5,0),Color3.fromRGB(255,218,158),20,0.7)
        end
        task.wait()
    end

    -- Estrada segmentada que acompanha o relevo e para onde nao ha terra.
    local function mkRoad(a,b,w)
        local d=Vector3.new(b.X-a.X,0,b.Z-a.Z); local len=d.Magnitude
        if len<8 then return end
        local dir=d.Unit
        local steps=math.ceil(len/12)
        local prev=nil
        for i=0,steps do
            local t=i/steps
            local px,pz=a.X+d.X*t,a.Z+d.Z*t
            local r=surfaceAt(px,pz)
            -- So constroi onde existe terreno acima da agua
            if r and r.Material~=Enum.Material.Water and r.Position.Y>WATER_Y+0.5 then
                local cur=Vector3.new(px,r.Position.Y+0.25,pz)
                if prev then
                    local seg=cur-prev
                    local sl=seg.Magnitude
                    if sl>1 and sl<24 then
                        local mid=(cur+prev)/2
                        mp("Road",infraFolder,Vector3.new(w,0.3,sl+1.5),
                           CFrame.lookAt(mid,mid+seg),Enum.Material.Asphalt,Color3.fromRGB(72,70,66),0)
                    end
                end
                prev=cur
            else
                prev=nil  -- corta a estrada, nao atravessa agua nem vazio
            end
        end
    end

    -- Simple placement: scan for flat spots, place buildings
    local spots={{}}; local BSCAN=44
    for bx=minX+45,maxX-45,BSCAN do for bz=minZ+45,maxZ-45,BSCAN do
        local r=surfaceAt(bx,bz)
        if r and r.Normal.Y>=0.75 and r.Position.Y>WATER_Y+3 then
            local nc=nearWater(bx,bz,90) and not nearWater(bx,bz,8)
            local alt=math.clamp((r.Position.Y-detectedMinY)/math.max(detectedHeight,1),0,1)
            local sc=r.Normal.Y*100-alt*40+(nc and 95 or 0)+bhash(bx,bz,700)*10
            table.insert(spots,{{x=bx,z=bz,y=r.Position.Y,sc=sc,nc=nc}})
        end
    end end
    table.sort(spots,function(a,b) return a.sc>b.sc end)

    local used={{}}
    local function far(x,z,d) for _,u in used do if(Vector2.new(x,z)-Vector2.new(u.x,u.z)).Magnitude<d then return false end end; return true end

    if #spots>=2 then
        local hp=nil
        for _,s in spots do if s.nc and far(s.x,s.z,65) then mkHotel(Vector3.new(s.x,s.y,s.z),bhash(s.x,s.z,100)*360); table.insert(used,s); hp=s; buildingCount+=1; break end end
        if not hp and #spots>0 then hp=spots[1]; mkHotel(Vector3.new(hp.x,hp.y,hp.z),bhash(hp.x,hp.z,100)*360); table.insert(used,hp); buildingCount+=1 end
        for _,s in spots do if far(s.x,s.z,55) then mkPlaza(Vector3.new(s.x,s.y,s.z)); table.insert(used,s); if hp then mkRoad(Vector3.new(hp.x,hp.y+0.3,hp.z),Vector3.new(s.x,s.y+0.3,s.z),10) end; break end end
        for _,s in spots do if far(s.x,s.z,48) then mkRest(Vector3.new(s.x,s.y,s.z),bhash(s.x,s.z,200)*360); table.insert(used,s); buildingCount+=1; if hp then mkRoad(Vector3.new(hp.x,hp.y+0.3,hp.z),Vector3.new(s.x,s.y+0.3,s.z),10) end; break end end
        local mH=math.floor(2+BUILDING_DENSITY*5); local hc=0
        for _,s in spots do if hc>=mH then break end; if far(s.x,s.z,42) then mkHouse(Vector3.new(s.x,s.y,s.z),bhash(s.x,s.z,301)*360,1+math.floor(bhash(s.x,s.z,300)*3)); table.insert(used,s); buildingCount+=1; hc+=1; if hp then mkRoad(Vector3.new(hp.x,hp.y+0.3,hp.z),Vector3.new(s.x,s.y+0.3,s.z),8) end end end
        if hp then
            for a=0,350,15 do local ang=math.rad(a); for d=20,90,10 do
                local px,pz=hp.x+math.cos(ang)*d,hp.z+math.sin(ang)*d
                local pr=surfaceAt(px,pz)
                if pr and pr.Position.Y>WATER_Y+1 and nearWater(px,pz,15) and not nearWater(px,pz,4) then
                    local wd=Vector3.new(0,0,1)
                    for wa=0,350,30 do local waa=math.rad(wa); local tr=surfaceAt(px+math.cos(waa)*22,pz+math.sin(waa)*22)
                        if tr and tr.Position.Y<=WATER_Y+1 then wd=Vector3.new(math.cos(waa),0,math.sin(waa)); break end
                    end
                    mkPier(Vector3.new(px,pr.Position.Y,pz),wd,WATER_Y); buildingCount+=1; break
                end
            end if buildingCount>0 and spots[#spots].nc then break end end
        end
    end
    print(string.format("[Map Architect] Construcoes: %d edificios.",buildingCount))
end

local function validSpawn(result)
    return result and result.Material ~= Enum.Material.Water and result.Normal.Y >= 0.85 and result.Position.Y > WATER_Y + 6
end
local function findSpawnGround()
    if SPAWN_MODE == "center" then
        local c = surfaceAt(CENTER.X,CENTER.Z)
        if validSpawn(c) then return c end
    end
    local best, bestScore = nil, -math.huge
    for x = minX + 24, maxX - 24, 28 do
        for z = minZ + 24, maxZ - 24, 28 do
            local result = surfaceAt(x,z)
            if validSpawn(result) then
                -- Verifica que a vizinhanca tambem eh plana (evita spawn em pico ou encosta)
                local flatOk = true
                for dx = -10, 10, 10 do
                    for dz = -10, 10, 10 do
                        local nr = surfaceAt(x+dx, z+dz)
                        if not nr or nr.Normal.Y < 0.8 or math.abs(nr.Position.Y - result.Position.Y) > 6 then
                            flatOk = false
                        end
                    end
                end
                if flatOk then
                    local score
                    if SPAWN_MODE == "highest" then
                        score = result.Position.Y
                    elseif SPAWN_MODE == "beach" then
                        score = nearWater(x,z,20) and (1000-result.Position.Y) or -1000
                    else
                        local dist = (Vector2.new(x,z)-Vector2.new(CENTER.X,CENTER.Z)).Magnitude
                        score = -dist*0.5 + result.Normal.Y*120
                    end
                    if score > bestScore then best, bestScore = result, score end
                end
            end
        end
    end
    -- Fallback: aceita qualquer ponto valido se nenhum passou no teste de vizinhanca
    if not best then
        for x = minX + 24, maxX - 24, 28 do
            for z = minZ + 24, maxZ - 24, 28 do
                local result = surfaceAt(x,z)
                if validSpawn(result) then return result end
            end
        end
    end
    return best
end

local spawnResult = findSpawnGround()
local spawnGroundPos = spawnResult and spawnResult.Position or Vector3.new(CENTER.X, WATER_Y + 20, CENTER.Z)

-- Remove arvores e pedras ao redor do spawn para o jogador nao nascer preso
for _, folder in {{treesFolder, rocksFolder}} do
    for _, obj in folder:GetChildren() do
        local op = nil
        if obj:IsA("Model") then
            local ok, pivot = pcall(function() return obj:GetPivot() end)
            if ok then op = pivot.Position end
        elseif obj:IsA("BasePart") then
            op = obj.Position
        end
        if op and (Vector2.new(op.X,op.Z)-Vector2.new(spawnGroundPos.X,spawnGroundPos.Z)).Magnitude < 22 then
            obj:Destroy()
        end
    end
end

-- Plataforma solida de spawn (CanCollide true evita o jogador cair pelo terreno)
local pad = Instance.new("Part")
pad.Name = "SpawnPad"
pad.Anchored = true
pad.CanCollide = true
pad.Size = Vector3.new(20, 1, 20)
pad.Position = spawnGroundPos + Vector3.new(0, 0.5, 0)
pad.Material = Enum.Material.Concrete
pad.Color = Color3.fromRGB(198, 194, 186)
pad.TopSurface = Enum.SurfaceType.Smooth
pad.BottomSurface = Enum.SurfaceType.Smooth
pad.Parent = spawnFolder

local spawn = Instance.new("SpawnLocation")
spawn.Name = "MapArchitectSpawn"
spawn.Anchored = true
spawn.Neutral = true
spawn.Size = Vector3.new(12, 1, 12)
spawn.Transparency = 1
spawn.CanCollide = true
spawn.CanTouch = false
spawn.CanQuery = false
spawn.AllowTeamChangeOnTouch = false
spawn.Position = spawnGroundPos + Vector3.new(0, 1.5, 0)
spawn.Parent = spawnFolder

print(string.format("[Map Architect] Spawn em (%.0f, %.0f, %.0f) modo %s.", spawnGroundPos.X, spawnGroundPos.Y, spawnGroundPos.Z, SPAWN_MODE))
print(string.format("[Map Architect] Concluido: %d arvores, %d rochas, %d construcoes.", treeCount, rockCount, buildingCount))
'''


def build_test_buildings_lua(req: GenerateRequest, job_id: str) -> str:
    """Script isolado de DEBUG: cria uma plataforma plana e coloca uma de cada
    construcao lado a lado, sem depender de terreno. Serve para iterar rapido
    na arquitetura das casas antes de gerar o mapa inteiro."""
    return f'''-- ROBLOX MAP ARCHITECT - TESTE DE CONSTRUCOES (DEBUG)
-- Job {job_id}
-- Cole na Command Bar. Nao precisa de heightmap nem terreno.
-- Cria uma plataforma plana com uma de cada construcao para inspecao.

local old = workspace:FindFirstChild("BuildingTestBench")
if old then old:Destroy() end

local bench = Instance.new("Folder")
bench.Name = "BuildingTestBench"
bench.Parent = workspace

local BUILDING_SEED = {req.building_seed}
local BASE_Y = 0

local function bhash(x,z,s) local n=math.sin((x*17.37+z*43.56+BUILDING_SEED*0.007+s)*43758.55); return n-math.floor(n) end

local function mp(n,par,sz,cf,mat,col,tr)
    local p=Instance.new("Part"); p.Name=n; p.Anchored=true; p.Size=sz; p.CFrame=cf; p.Material=mat; p.Color=col
    if tr and tr>0 then p.Transparency=tr end
    p.TopSurface=Enum.SurfaceType.Smooth; p.BottomSurface=Enum.SurfaceType.Smooth
    p.Parent=par; return p
end
local function ml(par,pos,col,rng,br)
    local lp=Instance.new("Part"); lp.Name="L"; lp.Anchored=true; lp.Shape=Enum.PartType.Ball
    lp.Size=Vector3.new(2,2,2); lp.Material=Enum.Material.Neon; lp.Color=col; lp.Position=pos; lp.Parent=par
    local pl=Instance.new("PointLight"); pl.Color=col; pl.Range=rng; pl.Brightness=br; pl.Parent=lp
end
local function label(txt,pos)
    local part=Instance.new("Part"); part.Name="Label"; part.Anchored=true; part.CanCollide=false
    part.Size=Vector3.new(1,1,1); part.Transparency=1; part.Position=pos; part.Parent=bench
    local bb=Instance.new("BillboardGui"); bb.Size=UDim2.new(0,260,0,50); bb.AlwaysOnTop=true
    bb.StudsOffset=Vector3.new(0,2,0); bb.Parent=part
    local tl=Instance.new("TextLabel"); tl.Size=UDim2.new(1,0,1,0); tl.BackgroundTransparency=0.35
    tl.BackgroundColor3=Color3.fromRGB(18,20,24); tl.TextColor3=Color3.fromRGB(240,240,240)
    tl.TextScaled=true; tl.Font=Enum.Font.GothamBold; tl.Text=txt; tl.Parent=bb
end

local function mw(n,par,sz,cf,mat,col)
    local p=Instance.new("WedgePart"); p.Name=n; p.Anchored=true; p.Size=sz; p.CFrame=cf
    p.Material=mat; p.Color=col; p.Parent=par; return p
end
local CC=Enum.Material.Concrete; local GL=Enum.Material.Glass; local WP=Enum.Material.WoodPlanks
local BR=Enum.Material.Brick; local WD=Enum.Material.Wood; local SL=Enum.Material.Slate
local trimC=Color3.fromRGB(96,88,78)
local wC=Color3.fromRGB(232,228,220); local gC=Color3.fromRGB(125,180,222); local cC=Color3.fromRGB(195,190,182)

-- ═══ PLATAFORMA DE TESTE ═══
local PLATFORM_W, PLATFORM_D = 460, 200
mp("Platform",bench,Vector3.new(PLATFORM_W,4,PLATFORM_D),CFrame.new(0,BASE_Y-2,0),Enum.Material.Grass,Color3.fromRGB(112,148,88),0)
-- Grade de referencia a cada 20 studs para conferir proporcao
for gx = -PLATFORM_W/2, PLATFORM_W/2, 20 do
    mp("Grid",bench,Vector3.new(0.25,0.1,PLATFORM_D),CFrame.new(gx,BASE_Y+0.05,0),Enum.Material.SmoothPlastic,Color3.fromRGB(150,180,130),0.5)
end

-- ═══ BONECO DE ESCALA (5 studs = altura media de um player R15) ═══
mp("ScaleRef",bench,Vector3.new(2,5,1),CFrame.new(-PLATFORM_W/2+16,BASE_Y+2.5,60),Enum.Material.Neon,Color3.fromRGB(255,80,80),0)
label("REFERENCIA: 5 studs = player",Vector3.new(-PLATFORM_W/2+16,BASE_Y+8,60))

-- ═══ CONSTRUCOES ═══
local function mkHotel(bp,yaw)
    local m=Instance.new("Model"); m.Name="Hotel"; m.Parent=bench
    local r=CFrame.new(bp)*CFrame.Angles(0,math.rad(yaw),0)
    local bW,bD,flH,fl=56,26,9,4; local tH=fl*flH
    local ROOMS=6
    -- Terreo em pedra, corpo claro (base escura assenta o predio)
    mp("Base",m,Vector3.new(bW+7,1.5,bD+7),r*CFrame.new(0,-0.75,0),SL,Color3.fromRGB(122,118,112),0)
    mp("Ground",m,Vector3.new(bW,flH,bD),r*CFrame.new(0,flH/2,0),SL,Color3.fromRGB(158,152,144),0)
    for f=1,fl-1 do
        local y=f*flH
        mp("F"..f,m,Vector3.new(bW,flH-0.6,bD),r*CFrame.new(0,y+flH/2,0),CC,wC,0)
        -- Laje saliente separando os andares
        mp("Slab"..f,m,Vector3.new(bW+2.5,0.55,bD+2.5),r*CFrame.new(0,y,0),CC,Color3.fromRGB(206,200,190),0)
        -- Quartos: cada um com sua janela e varanda propria
        for q=0,ROOMS-1 do
            local qx=-bW/2+3.5+q*((bW-7)/(ROOMS-1))
            mp("WinF",m,Vector3.new(4.6,flH-4,0.5),r*CFrame.new(qx,y+flH/2+0.4,bD/2+0.2),GL,gC,0.3)
            mp("WinB",m,Vector3.new(4.6,flH-4,0.5),r*CFrame.new(qx,y+flH/2+0.4,-bD/2-0.2),GL,gC,0.3)
            mp("BalFloor",m,Vector3.new(5.6,0.35,3.2),r*CFrame.new(qx,y+0.45,bD/2+1.6),CC,Color3.fromRGB(214,208,198),0)
            mp("BalRail",m,Vector3.new(5.6,1.15,0.22),r*CFrame.new(qx,y+1.1,bD/2+3.1),GL,Color3.fromRGB(178,206,228),0.25)
            mp("BalSideL",m,Vector3.new(0.22,1.15,3.2),r*CFrame.new(qx-2.7,y+1.1,bD/2+1.6),CC,Color3.fromRGB(214,208,198),0)
        end
        -- Pilares entre os quartos
        for q=0,ROOMS-2 do
            local px=-bW/2+3.5+(q+0.5)*((bW-7)/(ROOMS-1))
            mp("Pil",m,Vector3.new(1.6,flH-0.6,0.7),r*CFrame.new(px,y+flH/2,bD/2+0.3),CC,Color3.fromRGB(246,242,234),0)
        end
    end
    -- Cobertura com platibanda
    mp("Roof",m,Vector3.new(bW+3,0.9,bD+3),r*CFrame.new(0,tH+0.45,0),CC,Color3.fromRGB(178,172,164),0)
    for sz=-1,1,2 do
        mp("Parapet",m,Vector3.new(bW+3,1.6,0.6),r*CFrame.new(0,tH+1.6,sz*(bD/2+1.2)),CC,Color3.fromRGB(214,208,198),0)
    end
    for sx=-1,1,2 do
        mp("ParapetS",m,Vector3.new(0.6,1.6,bD+3),r*CFrame.new(sx*(bW/2+1.2),tH+1.6,0),CC,Color3.fromRGB(214,208,198),0)
    end
    mp("Penthouse",m,Vector3.new(16,4.5,11),r*CFrame.new(0,tH+3.2,0),CC,Color3.fromRGB(228,222,212),0)
    mp("PentGlass",m,Vector3.new(13,3.2,0.4),r*CFrame.new(0,tH+3.2,5.7),GL,gC,0.28)
    -- ENTRADA: porte-cochere com colunas
    mp("Lobby",m,Vector3.new(20,7,9),r*CFrame.new(0,3.5,bD/2+4.5),SL,Color3.fromRGB(168,162,154),0)
    mp("LGlass",m,Vector3.new(17,5.2,0.5),r*CFrame.new(0,3.6,bD/2+9.2),GL,gC,0.25)
    mp("LDoor",m,Vector3.new(6,5,0.6),r*CFrame.new(0,2.5,bD/2+9.4),GL,Color3.fromRGB(58,76,90),0.12)
    mp("Canopy",m,Vector3.new(26,0.6,11),r*CFrame.new(0,7.6,bD/2+6),CC,Color3.fromRGB(150,44,40),0)
    for sx=-1,1,2 do
        mp("Col",m,Vector3.new(1.3,7.6,1.3),r*CFrame.new(sx*11,3.8,bD/2+10.5),CC,Color3.fromRGB(244,240,232),0)
    end
    mp("Sign",m,Vector3.new(13,2.2,0.35),r*CFrame.new(0,6.6,bD/2+9.5),Enum.Material.Neon,Color3.fromRGB(255,218,130),0)
    -- Escadaria de acesso
    for st=0,2 do
        mp("Stair",m,Vector3.new(22-st*1.5,0.45,1.8),r*CFrame.new(0,0.22+st*0.45,bD/2+13.5-st*1.6),SL,Color3.fromRGB(186,180,172),0)
    end
    -- AREA DE PISCINA
    local po=r*CFrame.new(bW/2+16,0,0)
    mp("PDeck",m,Vector3.new(26,0.4,20),po*CFrame.new(0,0.2,0),SL,Color3.fromRGB(212,204,192),0)
    mp("PRim",m,Vector3.new(20,0.6,14),po*CFrame.new(0,0.4,0),CC,Color3.fromRGB(238,234,226),0)
    mp("PWater",m,Vector3.new(18,0.9,12),po*CFrame.new(0,0.35,0),GL,Color3.fromRGB(38,148,208),0.16)
    for ch=-1,1 do
        mp("Lounge",m,Vector3.new(2.2,0.4,5),po*CFrame.new(ch*4.5,0.6,8.5),CC,Color3.fromRGB(240,236,228),0)
        mp("Parasol",m,Vector3.new(0.25,4,0.25),po*CFrame.new(ch*4.5,2.4,10.5),WD,Color3.fromRGB(120,96,72),0)
        mp("ParasolTop",m,Vector3.new(5,0.3,5),po*CFrame.new(ch*4.5,4.4,10.5),Enum.Material.Fabric,Color3.fromRGB(220,206,168),0)
    end
    ml(m,(r*CFrame.new(0,tH+5,0)).Position,Color3.fromRGB(255,232,188),48,1.3)
    ml(m,(r*CFrame.new(0,5,bD/2+11)).Position,Color3.fromRGB(255,220,164),22,0.85)
    ml(m,(po*CFrame.new(0,3,0)).Position,Color3.fromRGB(120,196,255),20,0.6)
end

local function mkHouse(bp,yaw,v)
    local m=Instance.new("Model"); m.Name="Casa"; m.Parent=bench
    local r=CFrame.new(bp)*CFrame.Angles(0,math.rad(yaw),0)
    local hW,hD,hH=20,15,7.5
    local wCol = v==1 and Color3.fromRGB(238,232,220)
              or v==2 and Color3.fromRGB(214,198,172)
              or Color3.fromRGB(196,186,172)
    local roofCol = v==1 and Color3.fromRGB(112,58,48)
                 or v==2 and Color3.fromRGB(78,74,72)
                 or Color3.fromRGB(96,64,52)
    -- Fundacao com beiral
    mp("Base",m,Vector3.new(hW+3,1.2,hD+3),r*CFrame.new(0,-0.6,0),SL,Color3.fromRGB(128,124,118),0)
    -- Corpo
    mp("Walls",m,Vector3.new(hW,hH,hD),r*CFrame.new(0,hH/2,0),BR,wCol,0)
    -- Cantoneiras verticais (quebram a parede lisa)
    for sx=-1,1,2 do for sz=-1,1,2 do
        mp("Corner",m,Vector3.new(1.1,hH,1.1),r*CFrame.new(sx*(hW/2-0.4),hH/2,sz*(hD/2-0.4)),CC,Color3.fromRGB(244,240,232),0)
    end end
    -- Faixa horizontal no meio da parede
    mp("Belt",m,Vector3.new(hW+0.4,0.5,hD+0.4),r*CFrame.new(0,hH*0.52,0),CC,Color3.fromRGB(244,240,232),0)
    -- TELHADO DE DUAS AGUAS
    local rh=4.2
    for sz=-1,1,2 do
        mw("Roof",m,Vector3.new(hW+3,rh,hD/2+1.6),
           r*CFrame.new(0,hH+rh/2,sz*(hD/4+0.8))*CFrame.Angles(0,sz>0 and 0 or math.pi,0),SL,roofCol)
    end
    -- Cumeeira
    mp("Ridge",m,Vector3.new(hW+3.4,0.5,1),r*CFrame.new(0,hH+rh,0),SL,Color3.fromRGB(70,66,64),0)
    -- Beiral frontal e traseiro
    for sz=-1,1,2 do
        mp("Eave",m,Vector3.new(hW+3.4,0.4,1.2),r*CFrame.new(0,hH+0.2,sz*(hD/2+1.4)),SL,Color3.fromRGB(70,66,64),0)
    end
    -- Chamine
    mp("Chimney",m,Vector3.new(2,5,2),r*CFrame.new(hW/2-3,hH+2.5,-hD/4),BR,Color3.fromRGB(126,84,68),0)
    mp("ChimTop",m,Vector3.new(2.6,0.5,2.6),r*CFrame.new(hW/2-3,hH+5,-hD/4),SL,Color3.fromRGB(70,66,64),0)
    -- JANELAS com moldura
    local function win(cf,wid,hei)
        mp("WinFrame",m,Vector3.new(wid+0.8,hei+0.8,0.5),cf,WD,trimC,0)
        mp("WinGlass",m,Vector3.new(wid,hei,0.6),cf*CFrame.new(0,0,0.08),GL,gC,0.3)
        mp("WinBarV",m,Vector3.new(0.22,hei,0.7),cf*CFrame.new(0,0,0.1),WD,trimC,0)
        mp("WinBarH",m,Vector3.new(wid,0.22,0.7),cf*CFrame.new(0,0,0.1),WD,trimC,0)
    end
    win(r*CFrame.new(4.5,hH*0.58,hD/2+0.2),4.5,3.4)
    win(r*CFrame.new(-5.5,hH*0.58,hD/2+0.2),3.4,3.4)
    win(r*CFrame.new(hW/2+0.2,hH*0.58,-2)*CFrame.Angles(0,math.rad(90),0),4.5,3.4)
    win(r*CFrame.new(-hW/2-0.2,hH*0.58,2)*CFrame.Angles(0,math.rad(-90),0),4.5,3.4)
    -- PORTA com batente e degrau
    mp("DoorFrame",m,Vector3.new(4,6.2,0.6),r*CFrame.new(-1,3.1,hD/2+0.2),WD,trimC,0)
    mp("Door",m,Vector3.new(3.2,5.6,0.4),r*CFrame.new(-1,2.8,hD/2+0.45),WD,Color3.fromRGB(104,66,42),0)
    mp("Knob",m,Vector3.new(0.35,0.35,0.35),r*CFrame.new(0.2,2.9,hD/2+0.7),Enum.Material.Metal,Color3.fromRGB(196,178,120),0)
    mp("Step",m,Vector3.new(5,0.5,1.6),r*CFrame.new(-1,0.25,hD/2+1.4),SL,Color3.fromRGB(146,142,136),0)
    -- Alpendre com colunas
    mp("PorchRoof",m,Vector3.new(9,0.4,4),r*CFrame.new(-1,6.4,hD/2+2.4),SL,roofCol,0)
    for sx=-1,1,2 do
        mp("Column",m,Vector3.new(0.7,6.2,0.7),r*CFrame.new(-1+sx*3.8,3.1,hD/2+4),CC,Color3.fromRGB(242,238,230),0)
    end
    -- Deck de madeira
    mp("Deck",m,Vector3.new(hW+2,0.35,5),r*CFrame.new(0,0.18,hD/2+3),WP,Color3.fromRGB(150,110,72),0)
    -- Piscina com borda
    if v~=3 then
        mp("PoolRim",m,Vector3.new(12,0.5,8),r*CFrame.new(2,0.25,-hD/2-5),SL,Color3.fromRGB(206,200,190),0)
        mp("PoolWater",m,Vector3.new(10,0.7,6),r*CFrame.new(2,0.3,-hD/2-5),GL,Color3.fromRGB(42,155,212),0.18)
    end
    ml(m,(r*CFrame.new(-1,5.6,hD/2+3.6)).Position,Color3.fromRGB(255,216,150),15,0.65)
end

local function mkRest(bp,yaw)
    local m=Instance.new("Model"); m.Name="Restaurante"; m.Parent=bench
    local r=CFrame.new(bp)*CFrame.Angles(0,math.rad(yaw),0)
    mp("Body",m,Vector3.new(20,5.5,14),r*CFrame.new(0,2.75,0),CC,Color3.fromRGB(232,225,212),0)
    mp("Roof",m,Vector3.new(22,0.4,16),r*CFrame.new(0,5.7,0),CC,cC,0)
    mp("Glass",m,Vector3.new(18,4,0.4),r*CFrame.new(0,3.2,7.15),GL,gC,0.28)
    mp("Terrace",m,Vector3.new(22,0.25,10),r*CFrame.new(0,0.12,12),WP,Color3.fromRGB(148,108,70),0)
    mp("Awning",m,Vector3.new(22,0.2,10),r*CFrame.new(0,4.5,12),Enum.Material.Fabric,Color3.fromRGB(178,52,42),0)
    ml(m,(r*CFrame.new(0,5,12)).Position,Color3.fromRGB(255,198,128),20,0.8)
    return m
end

local function mkPlaza(bp)
    local m=Instance.new("Model"); m.Name="Praca"; m.Parent=bench
    mp("Floor",m,Vector3.new(36,0.3,36),CFrame.new(bp+Vector3.new(0,0.15,0)),CC,cC,0)
    mp("Fount",m,Vector3.new(7,1.8,7),CFrame.new(bp+Vector3.new(0,0.9,0)),CC,Color3.fromRGB(218,213,203),0)
    mp("Water",m,Vector3.new(5.5,0.3,5.5),CFrame.new(bp+Vector3.new(0,1.95,0)),GL,Color3.fromRGB(48,152,208),0.2)
    for i=0,3 do
        local a=math.rad(i*90+45)
        local lp=bp+Vector3.new(math.cos(a)*15,0,math.sin(a)*15)
        mp("Lamp"..i,m,Vector3.new(0.3,7,0.3),CFrame.new(lp+Vector3.new(0,3.5,0)),Enum.Material.Metal,Color3.fromRGB(68,63,58),0)
        ml(m,lp+Vector3.new(0,7.5,0),Color3.fromRGB(255,218,158),20,0.7)
    end
    return m
end

local function mkPier(bp,dir)
    local m=Instance.new("Model"); m.Name="Pier"; m.Parent=bench
    local d=dir.Unit; local dH=3
    for seg=0,4 do
        local sp=bp+d*(seg*12+6)+Vector3.new(0,dH,0)
        local cf=CFrame.lookAt(sp,sp+d)*CFrame.Angles(0,math.rad(90),0)
        mp("D"..seg,m,Vector3.new(7,0.45,12),cf,WP,Color3.fromRGB(142,105,65),0)
    end
    ml(m,bp+d*60+Vector3.new(0,dH+3,0),Color3.fromRGB(255,208,138),22,0.9)
    return m
end

-- ═══ LAYOUT DA BANCADA ═══
mkHotel(Vector3.new(-150, BASE_Y, 0), 0)
label("HOTEL 60x28 - 4 andares",Vector3.new(-150, BASE_Y+42, 0))

mkHouse(Vector3.new(-40, BASE_Y, 0), 0, 1)
label("CASA v1 (branca + piscina)",Vector3.new(-40, BASE_Y+12, 0))

mkHouse(Vector3.new(10, BASE_Y, 0), 0, 2)
label("CASA v2 (bege + piscina)",Vector3.new(10, BASE_Y+12, 0))

mkHouse(Vector3.new(60, BASE_Y, 0), 0, 3)
label("CASA v3 (sem piscina)",Vector3.new(60, BASE_Y+12, 0))

mkRest(Vector3.new(120, BASE_Y, 0), 0)
label("RESTAURANTE 20x14",Vector3.new(120, BASE_Y+10, 0))

mkPlaza(Vector3.new(180, BASE_Y, 0))
label("PRACA 36x36",Vector3.new(180, BASE_Y+10, 0))

mkPier(Vector3.new(-150, BASE_Y, -80), Vector3.new(0,0,-1))
label("PIER 60 studs",Vector3.new(-150, BASE_Y+8, -80))

-- ═══ ILUMINACAO DE ESTUDIO ═══
local lighting = game:GetService("Lighting")
lighting.ClockTime = 14
lighting.Brightness = 2.4
lighting.Ambient = Color3.fromRGB(120,124,132)
lighting.OutdoorAmbient = Color3.fromRGB(138,142,150)

print("[Map Architect] Bancada de teste criada em workspace.BuildingTestBench")
print("[Map Architect] Use a camera para inspecionar cada construcao de perto.")
print("[Map Architect] Para limpar: workspace.BuildingTestBench:Destroy()")
'''


def build_instructions(req: GenerateRequest, job_id: str) -> str:
    size = req.resolution * 4
    return f'''ROBLOX MAP ARCHITECT - PACOTE HEIGHTMAP {job_id}

1. No Terrain Editor > Import, selecione heightmap_{job_id}.png.
2. Se desejar materiais, selecione heightmap_{job_id}_colormap.png em Colormap.
3. Use Size X={size}, Y={req.import_vertical_size}, Z={size}.
4. Use Position X={req.import_center_x}, Y={req.import_center_y}, Z={req.import_center_z}.
5. Importe e salve o projeto.
6. Abra View > Command Bar, cole todo o conteudo de setup_world_{job_id}.lua e execute.
7. O script preenche somente o ar abaixo do nivel de agua, preserva as ilhas, cria spawn e decoracao basica opcional.

Nivel de agua normalizado: {req.water_level:.4f}
Seed: {req.seed}
Preset: {req.preset}
'''


@app.get("/health")
def health():
    return {
        "status": "online",
        "service": "roblox-map-heightmap",
        "version": app.version,
        "output_dir": str(OUTPUT_DIR),
    }


@app.post("/generate")
def generate(req: GenerateRequest):
    started = time.perf_counter()
    cfg = TerrainConfig(
        seed=req.seed,
        resolution=req.resolution,
        amplitude=req.amplitude,
        water_level=req.water_level,
        octaves=req.octaves,
        lacunarity=req.lacunarity,
        gain=req.gain,
        mountain_threshold=req.mountain_threshold,
        mountain_amplitude=req.mountain_amplitude,
        island_mode=req.island_mode,
        volcano=req.volcano,
    )

    base = generate_base(cfg)
    erosion_cfg = erosion_for(req.erosion, req.resolution)
    result = base
    if erosion_cfg is not None:
        result = hydraulic_erosion(base, erosion_cfg, req.seed)
        result, water_filter = postprocess_terrain(result, base, req.water_level)

    if erosion_cfg is None:
        result, water_filter = postprocess_terrain(result, base, req.water_level)

    job_id = uuid.uuid4().hex[:16]
    stem = f"heightmap_{job_id}"
    png_path = OUTPUT_DIR / f"{stem}.png"
    preview_path = OUTPUT_DIR / f"{stem}_preview.png"
    colormap_path = OUTPUT_DIR / f"{stem}_colormap.png"
    lua_path = OUTPUT_DIR / f"setup_world_{job_id}.lua"
    decorations_lua_path = OUTPUT_DIR / f"regenerate_decorations_{job_id}.lua"
    clear_lua_path = OUTPUT_DIR / f"clear_decorations_{job_id}.lua"
    test_buildings_path = OUTPUT_DIR / f"test_buildings_{job_id}.lua"
    instructions_path = OUTPUT_DIR / f"instructions_{job_id}.txt"
    package_path = OUTPUT_DIR / f"map_package_{job_id}.zip"
    meta_path = OUTPUT_DIR / f"{stem}.json"

    save_png16(png_path, result)
    save_preview(preview_path, result, req.water_level, req.preset.lower())
    save_colormap(colormap_path, result, req.water_level, req.preset.lower())
    lua_path.write_text(build_setup_lua(req, job_id, "full"), encoding="utf-8")
    decorations_lua_path.write_text(build_setup_lua(req, job_id, "decorations"), encoding="utf-8")
    clear_lua_path.write_text(build_setup_lua(req, job_id, "clear"), encoding="utf-8")
    test_buildings_path.write_text(build_test_buildings_lua(req, job_id), encoding="utf-8")
    instructions_path.write_text(build_instructions(req, job_id), encoding="utf-8")

    metadata = {
        "id": job_id,
        "seconds": round(time.perf_counter() - started, 4),
        "terrain": asdict(cfg),
        "erosion": req.erosion,
        "preset": req.preset,
        "erosion_config": asdict(erosion_cfg) if erosion_cfg else None,
        "stats": stats(result, req.water_level),
        "water_filter": water_filter,
        "files": {
            "heightmap": f"/files/{png_path.name}",
            "preview": f"/files/{preview_path.name}",
            "colormap": f"/files/{colormap_path.name}",
            "setup_lua": f"/files/{lua_path.name}",
            "regenerate_decorations_lua": f"/files/{decorations_lua_path.name}",
            "clear_decorations_lua": f"/files/{clear_lua_path.name}",
            "test_buildings_lua": f"/files/{test_buildings_path.name}",
            "instructions": f"/files/{instructions_path.name}",
            "package": f"/files/{package_path.name}",
            "metadata": f"/files/{meta_path.name}",
        },
    }
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in (png_path, preview_path, colormap_path, lua_path, decorations_lua_path, clear_lua_path, test_buildings_path, instructions_path, meta_path):
            zf.write(file_path, arcname=file_path.name)

    if req.response == "png":
        return FileResponse(png_path, media_type="image/png", filename=png_path.name)
    if req.response == "preview":
        return FileResponse(preview_path, media_type="image/png", filename=preview_path.name)
    if req.response == "package":
        return FileResponse(package_path, media_type="application/zip", filename=package_path.name)
    return JSONResponse(metadata)


@app.get("/files/{filename}")
def get_file(filename: str):
    safe_name = Path(filename).name
    path = OUTPUT_DIR / safe_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")
    suffix = path.suffix.lower()
    media_types = {".png":"image/png", ".json":"application/json", ".lua":"text/plain; charset=utf-8", ".txt":"text/plain; charset=utf-8", ".zip":"application/zip"}
    return FileResponse(path, media_type=media_types.get(suffix, "application/octet-stream"), filename=path.name)
