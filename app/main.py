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

# ═══════════════════════════════════════════════════════════════════════
# BIBLIOTECA LUA DE CONSTRUCOES
# Mantida como string crua (sem f-string) para que as chaves do Lua nao
# precisem de escape duplo. E compartilhada entre o script do mapa e o
# script de bancada de teste, para as duas nunca divergirem — na v0.8.7
# a bancada ainda tinha a casa da v0.7.
# ═══════════════════════════════════════════════════════════════════════
LUA_BUILD_LIB = r"""
    -- ═══ BIBLIOTECA PROPRIA (opcional) ═══
    -- Se existir ServerStorage.MapArchitectKit, o gerador usa os modelos de la
    -- em vez de montar com Parts. Estrutura esperada:
    --   ServerStorage/MapArchitectKit/
    --     Hotel/        (um ou mais modelos; escolhe um por sorteio)
    --     Casa/
    --     Restaurante/
    --     Praca/
    -- Cada modelo precisa de PrimaryPart definida ou sera usado o pivo padrao.
    -- Assim voce monta as casas a mao no Studio e o script so posiciona.
    -- ═══ ASSET IDs DO ROBLOX ═══
    -- Cole o ID de um modelo publico do Creator Store nos campos do site.
    -- O script baixa uma vez com InsertService e depois so clona.
    local InsertService = game:GetService("InsertService")
    local assetCache = {}
    local function assetLoad(id)
        if not id or id == "" then return nil end
        local num = tonumber(id)
        if not num then return nil end
        if assetCache[num] ~= nil then
            if assetCache[num] == false then return nil end
            return assetCache[num]
        end
        local ok, res = pcall(function() return InsertService:LoadAsset(num) end)
        if not ok or not res then
            warn("[Map Architect] Falha ao carregar asset "..tostring(num)..
                 " — precisa ser publico. Usando construcao procedural.")
            assetCache[num] = false
            return nil
        end
        local modelo = nil
        for _,c in res:GetChildren() do
            if c:IsA("Model") then modelo = c break end
        end
        if not modelo then
            warn("[Map Architect] Asset "..tostring(num).." nao contem um Model.")
            assetCache[num] = false
            return nil
        end
        -- Remove scripts de terceiros por seguranca e ancora as pecas
        for _,d in modelo:GetDescendants() do
            if d:IsA("Script") or d:IsA("LocalScript") or d:IsA("ModuleScript") then
                d:Destroy()
            end
        end
        for _,d in modelo:GetDescendants() do
            if d:IsA("BasePart") then d.Anchored = true end
        end
        assetCache[num] = modelo
        print("[Map Architect] Asset "..tostring(num).." carregado.")
        return modelo
    end
    local function assetPlace(id, pos, yaw)
        local molde = assetLoad(id)
        if not molde then return false end
        local c = molde:Clone()
        c.Parent = buildingsFolder
        c:PivotTo(CFrame.new(pos) * CFrame.Angles(0, math.rad(yaw), 0))
        return true
    end

    local KIT = game:GetService("ServerStorage"):FindFirstChild("MapArchitectKit")
    local function kitPick(categoria, x, z)
        if not KIT then return nil end
        local pasta = KIT:FindFirstChild(categoria)
        if not pasta then return nil end
        local opcoes = {}
        for _,o in pasta:GetChildren() do
            if o:IsA("Model") then table.insert(opcoes,o) end
        end
        if #opcoes == 0 then return nil end
        local n = math.sin((x*12.9898+z*78.233+BUILDING_SEED*0.013)*43758.5453)
        n = n - math.floor(n)
        return opcoes[1 + math.floor(n * #opcoes) % #opcoes]
    end
    local function kitPlace(categoria, pos, yaw, parent)
        local molde = kitPick(categoria, pos.X, pos.Z)
        if not molde then return false end
        local c = molde:Clone()
        c.Parent = parent or buildingsFolder
        local cf = CFrame.new(pos) * CFrame.Angles(0, math.rad(yaw), 0)
        if c.PrimaryPart then c:PivotTo(cf) else c:PivotTo(cf) end
        return true
    end
    if KIT then
        print("[Map Architect] Biblioteca MapArchitectKit encontrada — usando modelos proprios.")
    end

    local function bhash(x,z,s) local n=math.sin((x*17.37+z*43.56+BUILDING_SEED*0.007+s)*43758.55); return n-math.floor(n) end
    -- Peca decorativa: pequena o bastante para ninguem notar a sombra dela
    -- nem precisar acerta-la com raycast. Telha (1.9 studs cubicos),
    -- balaustre (0.14), tijolo (0.64) entram; parede (140) e leito de rua
    -- ficam de fora e continuam com sombra e consulta normais.
    local DECOR_VOL=10
    local decorativas=0
    local function mp(n,par,sz,cf,mat,col,tr)
        local p=Instance.new("Part"); p.Name=n; p.Anchored=true; p.Size=sz; p.CFrame=cf; p.Material=mat; p.Color=col
        if tr and tr>0 then p.Transparency=tr end
        -- Toque nunca e usado por construcao nenhuma, e cada peca ativa custa
        -- no processamento de colisao do cliente.
        p.CanTouch=false
        if sz.X*sz.Y*sz.Z < DECOR_VOL then
            p.CanQuery=false
            p.CastShadow=false
            decorativas+=1
        end
        p.Parent=par; return p
    end
    -- Orcamento de luzes. A Metropole chegou a 600 PointLights: iluminacao
    -- dinamica e cara, e em modo Future cada uma pode projetar sombra. Poste
    -- de rua e o que se ve a noite, entao ele tem prioridade — a luz interna
    -- de casa e o numero da porta cedem a vez quando o orcamento acaba.
    local LUZ_ORCAMENTO=190
    local luzes=0
    local function ml(par,pos,col,rng,br,prioridade)
        if not prioridade and luzes>=LUZ_ORCAMENTO then return end
        luzes+=1
        local lp=Instance.new("Part"); lp.Name="L"; lp.Anchored=true; lp.Shape=Enum.PartType.Ball
        lp.Size=Vector3.new(2,2,2); lp.Material=Enum.Material.Neon; lp.Color=col; lp.Position=pos
        lp.CanTouch=false; lp.CanQuery=false; lp.CastShadow=false; lp.Parent=par
        local pl=Instance.new("PointLight"); pl.Color=col; pl.Range=rng; pl.Brightness=br; pl.Parent=lp
    end
    local function mw(n,par,sz,cf,mat,col)
        local p=Instance.new("WedgePart"); p.Name=n; p.Anchored=true; p.Size=sz; p.CFrame=cf
        p.Material=mat; p.Color=col; p.CanTouch=false
        if sz.X*sz.Y*sz.Z < DECOR_VOL then p.CanQuery=false; p.CastShadow=false end
        p.Parent=par; return p
    end
    -- SP (SmoothPlastic) nao tem textura: e o unico jeito de evitar o xadrez
    -- que aparece quando a textura do material se repete numa face grande.
    -- ─── AGUA DE TERRENO NA PISCINA ────────────────────────────────────
    -- Este e o UNICO FillBlock do gerador, e a diferenca em relacao ao que
    -- travou a v0.7.0 e de ordem de grandeza: la o flattenArea chamava
    -- FillBlock de 4 em 4 studs para cada edificio e cada segmento de rua —
    -- milhares de chamadas sem yield. Aqui e UMA chamada por piscina (dez ou
    -- duas dezenas no mapa inteiro), com yield depois de cada uma. Continua
    -- valendo a regra: nada de terraplanagem pelo Lua.
    local piscinasCheias=0
    local function encherPiscina(cf,largura,comprimento,profundidade)
        local ok=pcall(function()
            Terrain:FillBlock(cf,Vector3.new(largura,profundidade,comprimento),Enum.Material.Water)
        end)
        if ok then
            piscinasCheias+=1
            task.wait()
        else
            warn("[Map Architect] FillBlock falhou nesta piscina — ela fica sem agua.")
        end
        -- Onda de mar numa piscina de 12x8 vira espuma branca atravessada.
        -- So acalma a agua quando NAO ha oceano no mapa, senao tiraria a onda
        -- da praia junto.
        if piscinasCheias==1 and WATER_LEVEL<=0.005 then
            pcall(function()
                Terrain.WaterWaveSize=0
                Terrain.WaterWaveSpeed=0
            end)
        end
        return ok
    end

    local SP=Enum.Material.SmoothPlastic
    local CC=SP; local GL=Enum.Material.Glass; local WP=SP

    -- Escada que desce do piso ate encontrar o terreno. Quando a construcao
    -- assenta no ponto mais alto, o lado baixo fica elevado; em vez de tentar
    -- eliminar esse desnivel, damos acesso a ele.
    -- maxRun limita ATE ONDE a escada pode avancar. Sem ele o degrau descia
    -- 0.55 por 1.6 studs de avanco (19 graus): a casa assenta no ponto mais
    -- alto da pegada, e quando o jardim caia 10 studs a escada precisava de
    -- 29 studs de corrida — atravessava o recuo frontal e desembocava na rua.
    -- Agora o desnivel e medido primeiro e a altura do degrau se ajusta para
    -- caber no espaco disponivel.
    local function escadaAoSolo(model, deWorld, dirWorld, largura, corDegrau, corGuarda, maxRun)
        local dir = Vector3.new(dirWorld.X,0,dirWorld.Z)
        if dir.Magnitude < 0.01 then return end
        dir = dir.Unit
        maxRun = maxRun or 26
        local passo = 1.6
        local nMax = math.max(1, math.floor(maxRun/passo))
        local fim = deWorld + dir*maxRun
        local srFim = surfaceAt(fim.X, fim.Z)
        local yFim = srFim and srFim.Position.Y or (deWorld.Y - 1)
        local queda = math.max(0, deWorld.Y - yFim)
        local alturaDegrau = math.clamp(queda/nMax, 0.35, 2.2)
        local pos = deWorld
        local degraus = 0
        for i = 1, nMax do
            local frente = pos + dir*passo
            local sr = surfaceAt(frente.X, frente.Z)
            if not sr then break end
            local proximoY = pos.Y - alturaDegrau
            -- Chegou ao solo: encerra
            if proximoY <= sr.Position.Y + 0.4 then
                mp("StepLast",model,Vector3.new(largura,0.5,passo+1.2),
                   CFrame.lookAt(Vector3.new(frente.X,sr.Position.Y+0.25,frente.Z),
                                 Vector3.new(frente.X,sr.Position.Y+0.25,frente.Z)+dir),
                   SP,corDegrau,0)
                degraus = degraus + 1
                break
            end
            local c = Vector3.new(frente.X, proximoY, frente.Z)
            mp("Step",model,Vector3.new(largura,0.5,passo+0.4),
               CFrame.lookAt(c,c+dir),SP,corDegrau,0)
            -- Contra-degrau, para nao ficar vazado
            mp("Riser",model,Vector3.new(largura,alturaDegrau,0.35),
               CFrame.lookAt(c+Vector3.new(0,-alturaDegrau/2,0)-dir*(passo/2),
                             c+Vector3.new(0,-alturaDegrau/2,0)-dir*(passo/2)+dir),
               SP,corDegrau,0)
            -- Corrimao dos dois lados
            local lado = Vector3.new(-dir.Z,0,dir.X)
            for sx=-1,1,2 do
                mp("Handrail",model,Vector3.new(0.22,1.0,passo+0.4),
                   CFrame.lookAt(c+lado*(sx*largura/2)+Vector3.new(0,0.75,0),
                                 c+lado*(sx*largura/2)+Vector3.new(0,0.75,0)+dir),
                   SP,corGuarda,0)
            end
            pos = c
            degraus = degraus + 1
        end
        return degraus
    end
    local BR=SP; local WD=Enum.Material.Wood; local SL=SP
    local wC=Color3.fromRGB(232,228,220); local gC=Color3.fromRGB(125,180,222); local cC=Color3.fromRGB(195,190,182)
    local trimC=Color3.fromRGB(96,88,78)

    -- Hotel com varandas individuais por quarto e pilares verticais, para a
    -- fachada nao ficar sendo uma parede lisa de 56 studs.
    -- Hotel detalhado: molduras por janela, guarda-corpo com balaustres,
    -- faixas horizontais, cobertura com platibanda. ~600 pecas.
    local function mkHotel(bp,yaw)
        if assetPlace(ASSET_HOTEL,bp,yaw) then task.wait(); return end
        if kitPlace("Hotel",bp,yaw) then task.wait(); return end
        local m=Instance.new("Model"); m.Name="Hotel"; m.Parent=buildingsFolder
        local r=CFrame.new(bp)*CFrame.Angles(0,math.rad(yaw),0)
        local bW,bD,flH,fl=54,26,9.5,4
        local tH=fl*flH
        local ROOMS=6
        local corpo=Color3.fromRGB(238,233,224)
        local trim=Color3.fromRGB(252,250,246)
        local pedra=Color3.fromRGB(158,152,144)

        -- Fundacao funda + embasamento em pedra
        mp("Base",m,Vector3.new(bW+8,40,bD+8),r*CFrame.new(0,-20,0),SL,Color3.fromRGB(118,114,108),0)
        for k=0,17 do
            local px=-bW/2-1+k*((bW+2)/17)
            for sz=-1,1,2 do
                mp("Plinth",m,Vector3.new((bW+2)/17-0.12,1.6,0.5),
                   r*CFrame.new(px,0.5,sz*(bD/2+1.4)),SP,
                   Color3.fromRGB(150+(k%3)*8,144+(k%3)*8,136+(k%3)*8),0)
            end
        end

        -- Terreo em pedra
        mp("Ground",m,Vector3.new(bW,flH,bD),r*CFrame.new(0,flH/2,0),SP,pedra,0)
        for k=0,13 do
            mp("Rustic",m,Vector3.new(bW/14-0.1,flH-1,0.35),
               r*CFrame.new(-bW/2+bW/28+k*(bW/14),flH/2,bD/2+0.2),SP,
               Color3.fromRGB(166+(k%2)*10,160+(k%2)*10,152+(k%2)*10),0)
        end
        task.wait()

        -- Andares
        for f=1,fl-1 do
            local y=f*flH
            mp("Body",m,Vector3.new(bW,flH-0.7,bD),r*CFrame.new(0,y+flH/2,0),SP,corpo,0)
            -- Laje saliente com friso
            mp("Slab",m,Vector3.new(bW+3,0.6,bD+3),r*CFrame.new(0,y,0),SP,trim,0)
            mp("Fillet",m,Vector3.new(bW+3.4,0.25,bD+3.4),r*CFrame.new(0,y-0.42,0),SP,Color3.fromRGB(206,200,190),0)

            for q=0,ROOMS-1 do
                local qx=-bW/2+4+q*((bW-8)/(ROOMS-1))
                -- Janela com moldura completa (frente e fundo)
                for sz=-1,1,2 do
                    local zf=sz*(bD/2+0.15)
                    mp("WSill",m,Vector3.new(5.6,0.4,0.8),r*CFrame.new(qx,y+2.4,zf),SP,trim,0)
                    mp("WHead",m,Vector3.new(5.6,0.45,0.7),r*CFrame.new(qx,y+flH-2.2,zf),SP,trim,0)
                    for sx=-1,1,2 do
                        mp("WJamb",m,Vector3.new(0.5,flH-4.6,0.6),r*CFrame.new(qx+sx*2.3,y+flH/2,zf),SP,trim,0)
                    end
                    mp("WGlass",m,Vector3.new(4.2,flH-4.8,0.3),r*CFrame.new(qx,y+flH/2,zf),GL,gC,0.3)
                    mp("WMull",m,Vector3.new(0.18,flH-4.8,0.4),r*CFrame.new(qx,y+flH/2,zf),SP,trim,0)
                end
                -- Varanda com balaustres
                mp("BalFloor",m,Vector3.new(6,0.4,3.4),r*CFrame.new(qx,y+0.5,bD/2+1.7),SP,trim,0)
                mp("BalTop",m,Vector3.new(6,0.3,0.5),r*CFrame.new(qx,y+1.9,bD/2+3.3),SP,trim,0)
                for bl=0,6 do
                    mp("Bal",m,Vector3.new(0.26,1.3,0.26),
                       r*CFrame.new(qx-2.5+bl*(5/6),y+1.15,bD/2+3.3),SP,trim,0)
                end
                for sx=-1,1,2 do
                    mp("BalSide",m,Vector3.new(0.3,1.5,3.4),r*CFrame.new(qx+sx*2.9,y+1.25,bD/2+1.7),SP,trim,0)
                end
            end
            -- Pilastras entre os quartos
            for q=0,ROOMS-2 do
                local px=-bW/2+4+(q+0.5)*((bW-8)/(ROOMS-1))
                mp("Pilaster",m,Vector3.new(1.8,flH-0.7,0.8),r*CFrame.new(px,y+flH/2,bD/2+0.35),SP,trim,0)
                mp("PilCap",m,Vector3.new(2.3,0.45,1.1),r*CFrame.new(px,y+flH-0.6,bD/2+0.45),SP,trim,0)
            end
            task.wait()
        end

        -- Cobertura com platibanda e balaustrada
        mp("Roof",m,Vector3.new(bW+3,1,bD+3),r*CFrame.new(0,tH+0.5,0),SP,Color3.fromRGB(196,190,182),0)
        -- Balaustrada contornando os QUATRO lados. Antes so existia na frente
        -- e no fundo, entao o terraco ficava aberto nas laterais.
        local bz = bD/2 + 1.6
        local bx = bW/2 + 1.8
        for sz=-1,1,2 do
            mp("Cornice",m,Vector3.new(bW+4.6,0.8,1.2),r*CFrame.new(0,tH+1.4,sz*bz),SP,trim,0)
            for bl=0,25 do
                mp("RoofBal",m,Vector3.new(0.42,2.2,0.42),
                   r*CFrame.new(-bW/2-1.6+bl*((bW+3.2)/25),tH+2.9,sz*bz),SP,trim,0)
            end
            mp("RailTop",m,Vector3.new(bW+4.6,0.5,0.9),r*CFrame.new(0,tH+4.2,sz*bz),SP,trim,0)
        end
        for sx=-1,1,2 do
            mp("CorniceS",m,Vector3.new(1.2,0.8,bD+4.6),r*CFrame.new(sx*bx,tH+1.4,0),SP,trim,0)
            for bl=0,12 do
                mp("RoofBalS",m,Vector3.new(0.42,2.2,0.42),
                   r*CFrame.new(sx*bx,tH+2.9,-bD/2-1.6+bl*((bD+3.2)/12)),SP,trim,0)
            end
            mp("RailTopS",m,Vector3.new(0.9,0.5,bD+4.6),r*CFrame.new(sx*bx,tH+4.2,0),SP,trim,0)
        end
        -- Pilaretes nos cantos, arrematando o contorno
        for sx=-1,1,2 do for sz=-1,1,2 do
            mp("CornerPost",m,Vector3.new(1.6,3.6,1.6),r*CFrame.new(sx*bx,tH+2.6,sz*bz),SP,trim,0)
            mp("CornerCap",m,Vector3.new(2.1,0.5,2.1),r*CFrame.new(sx*bx,tH+4.6,sz*bz),SP,Color3.fromRGB(214,208,198),0)
        end end
        mp("Penthouse",m,Vector3.new(18,5,12),r*CFrame.new(0,tH+3.5,0),SP,corpo,0)
        mp("PentGlass",m,Vector3.new(14,3.4,0.4),r*CFrame.new(0,tH+3.5,6.2),GL,gC,0.28)
        mp("PentRoof",m,Vector3.new(20,0.6,14),r*CFrame.new(0,tH+6.2,0),SP,trim,0)
        task.wait()

        -- Entrada: porte-cochere
        mp("Lobby",m,Vector3.new(20,8,10),r*CFrame.new(0,4,bD/2+5),SP,pedra,0)
        mp("LGlass",m,Vector3.new(17,5.6,0.5),r*CFrame.new(0,4,bD/2+10.2),GL,gC,0.24)
        for dv=0,3 do
            mp("LMull",m,Vector3.new(0.3,5.6,0.7),r*CFrame.new(-6.5+dv*4.3,4,bD/2+10.3),SP,trim,0)
        end
        mp("LDoor",m,Vector3.new(6.5,5.4,0.6),r*CFrame.new(0,2.7,bD/2+10.5),GL,Color3.fromRGB(56,74,88),0.1)
        mp("Canopy",m,Vector3.new(26,0.7,12),r*CFrame.new(0,8.4,bD/2+6.5),SP,Color3.fromRGB(150,44,40),0)
        mp("CanopyEdge",m,Vector3.new(26.6,0.35,12.6),r*CFrame.new(0,7.9,bD/2+6.5),SP,Color3.fromRGB(120,34,32),0)
        for sx=-1,1,2 do
            mp("Col",m,Vector3.new(1.5,8.4,1.5),r*CFrame.new(sx*11,4.2,bD/2+11),SP,trim,0)
            mp("ColBase",m,Vector3.new(2.2,0.7,2.2),r*CFrame.new(sx*11,0.35,bD/2+11),SP,trim,0)
            mp("ColCap",m,Vector3.new(2.2,0.6,2.2),r*CFrame.new(sx*11,8.3,bD/2+11),SP,trim,0)
        end
        mp("Sign",m,Vector3.new(14,2.4,0.4),r*CFrame.new(0,7,bD/2+10.6),Enum.Material.Neon,Color3.fromRGB(255,218,130),0)
        local degrauHotel = (r*CFrame.new(0,0.4,bD/2+14))
        escadaAoSolo(m, degrauHotel.Position, degrauHotel.LookVector, 14,
                     Color3.fromRGB(180,174,166), trim)
        task.wait()

        -- Piscina
        local po=r*CFrame.new(bW/2+12,0,0)
        -- Saia funda: sem isso a plataforma da piscina ficava suspensa quando
        -- o terreno caia daquele lado do hotel.
        -- Bacia OCA, como a das casas: a piscina do hotel ainda era placa de
        -- vidro sobre bloco macico (era ela que aparecia "sem agua").
        mp("PSkirt",m,Vector3.new(28,32,22),po*CFrame.new(0,-19.4,0),SL,Color3.fromRGB(150,146,140),0)
        for sx=-1,1,2 do
            mp("PDeck",m,Vector3.new(3.5,0.5,22),po*CFrame.new(sx*12.25,0.25,0),SP,Color3.fromRGB(216,210,198),0)
        end
        for sz=-1,1,2 do
            mp("PDeck",m,Vector3.new(28,0.5,3.5),po*CFrame.new(0,0.25,sz*9.25),SP,Color3.fromRGB(216,210,198),0)
        end
        mp("PFloor",m,Vector3.new(21.4,0.8,15.4),po*CFrame.new(0,-3.4,0),SP,Color3.fromRGB(196,214,226),0)
        for sx=-1,1,2 do
            mp("PWall",m,Vector3.new(0.8,4.4,15.4),po*CFrame.new(sx*10.9,-1.0,0),SP,Color3.fromRGB(206,220,230),0)
        end
        for sz=-1,1,2 do
            mp("PWall",m,Vector3.new(21.4,4.4,0.8),po*CFrame.new(0,-1.0,sz*7.9),SP,Color3.fromRGB(206,220,230),0)
        end
        encherPiscina(po*CFrame.new(0,-1.2,0),20,14,4.4)
        for cp=0,15 do
            local a=cp/15*math.pi*2
            mp("Coping",m,Vector3.new(1.6,0.3,1.6),po*CFrame.new(math.cos(a)*11,0.65,math.sin(a)*8),SP,Color3.fromRGB(238,234,226),0)
        end
        for ch=-1,1 do
            mp("Lounge",m,Vector3.new(2.4,0.4,5.2),po*CFrame.new(ch*5,0.7,9.5),SP,Color3.fromRGB(244,240,232),0)
            mp("LoungeBack",m,Vector3.new(2.4,2,0.3),po*CFrame.new(ch*5,1.5,11.8)*CFrame.Angles(math.rad(-28),0,0),SP,Color3.fromRGB(244,240,232),0)
            mp("Parasol",m,Vector3.new(0.28,4.5,0.28),po*CFrame.new(ch*5,2.6,12.5),WD,Color3.fromRGB(118,94,70),0)
            mp("ParasolTop",m,Vector3.new(5.5,0.35,5.5),po*CFrame.new(ch*5,4.8,12.5),Enum.Material.Fabric,Color3.fromRGB(222,208,170),0)
        end

        ml(m,(r*CFrame.new(0,tH+7,0)).Position,Color3.fromRGB(255,232,188),50,1.3)
        ml(m,(r*CFrame.new(0,5.5,bD/2+12)).Position,Color3.fromRGB(255,220,164),24,0.9)
        ml(m,(po*CFrame.new(0,3.5,0)).Position,Color3.fromRGB(120,196,255),22,0.6)
        task.wait()
    end

    -- Casa com telhado de duas aguas, molduras e detalhes que quebram as
    -- superficies grandes (parede lisa e o que da aspecto de caixa no Roblox).
    -- Casa detalhada: telhas individuais, tabuas de fachada, esquadrias
    -- ═══════════════════════════════════════════════════════════════════
    -- CASA POR COMPOSICAO DE MODULOS  (v0.9 URBANO)
    -- ═══════════════════════════════════════════════════════════════════
    -- Ate a v0.8.7 existiam 3 variantes de casa que diferiam SO NA COR: a
    -- geometria era sempre a mesma, entao cinco casas no mapa eram cinco
    -- copias. Agora a casa e montada a partir de modulos sorteados pela
    -- seed do lote:
    --
    --   planta (3) x pavimentos (2) x telhado (4) x anexo (5)
    --   x acabamento (4) x esquadria (3) x quintal (5)  =  7200 combinacoes
    --
    -- antes de contar cor, largura e profundidade. Num bairro de 12 casas a
    -- chance de duas iguais e praticamente nula.

    local function shade(c,d)
        return Color3.fromRGB(math.clamp(c.R*255+d,0,255),
                              math.clamp(c.G*255+d,0,255),
                              math.clamp(c.B*255+d,0,255))
    end
    -- Sorteio estavel: mesma seed de lote sempre devolve a mesma casa.
    -- Hash inteiro (quadratico modulo primo), NAO frac(sin(x)). O truque do
    -- seno colapsa aqui: com seeds na casa dos milhares o argumento passa de
    -- 1e8, o double perde os bits baixos e as escolhas ficam correlacionadas
    -- — oito casas seguidas cairam em apenas quatro combinacoes. Toda a
    -- aritmetica abaixo cabe exata em double (65521^2 = 4.3e9, bem abaixo de
    -- 2^53), entao o resultado e identico no Studio e em qualquer conferencia
    -- offline.
    local function hval(hs,k)
        local x=(math.floor(hs)+k*7919+BUILDING_SEED*13)%65521
        x=(x*x+12345)%65521
        x=(x*x+6791)%65521
        x=(x*x+54121)%65521
        return x/65521
    end
    local function hpick(hs,k,lista)
        return lista[1 + math.floor(hval(hs,k) * #lista) % #lista]
    end

    -- ─── UMA AGUA DE TELHADO ────────────────────────────────────────────
    -- ridgeZ e a linha alta; o beiral fica em ridgeZ + dz*run.
    -- A telha sobe em Y e recua em Z na MESMA proporcao — foi exatamente
    -- isso que faltava na v0.7.9 e deixou as telhas pairando no ar.
    -- taper > 0 estreita a agua conforme sobe (usado no telhado de 4 aguas).
    local function agua(m,fr,cx,ridgeZ,larg,run,rise,y0,dz,cor,rows,cols,taper)
        if run<=0.2 or larg<=0.5 then return end
        local pitch=math.atan(rise/run)
        local slope=math.sqrt(rise*rise+run*run)
        taper=taper or 0
        -- Sub-cobertura continua: fecha qualquer vao entre as telhas
        mp("RoofDeck",m,Vector3.new(larg,0.5,slope+0.6),
           fr*CFrame.new(cx,y0+rise/2,ridgeZ+dz*run/2)*CFrame.Angles(dz*pitch,0,0),
           SP,Color3.fromRGB(58,53,50),0)
        local tlen=slope/rows*1.55
        for row=0,rows-1 do
            local t=(row+0.5)/rows
            local ty=y0+t*rise
            local tz=ridgeZ+dz*run*(1-t)
            local lw=larg-2*taper*t
            if lw>1.2 then
                local c=shade(cor,(row%2==0) and 11 or -9)
                local step=lw/cols
                for col=0,cols-1 do
                    mp("Tile",m,Vector3.new(step-0.08,0.25,tlen),
                       fr*CFrame.new(cx-lw/2+step/2+col*step,ty+0.35,tz)*CFrame.Angles(dz*pitch,0,0),
                       SP,c,0)
                end
            end
        end
    end

    -- ─── EMPENA (o triangulo da ponta) ──────────────────────────────────
    -- Sem isso o telhado fica com dois buracos abertos nas laterais.
    local function empena(m,fr,cx,cz,xs,halfBase,y0,rise,cor,trim,tipo,tf,hf)
        local steps=11
        for g=0,steps-1 do
            local t=(g+0.5)/steps
            local gy=y0+t*rise
            local hw
            if tipo=="mansarda" then
                if t<tf then hw=halfBase*(1-(t/tf)*(1-hf))
                else hw=halfBase*hf*(1-(t-tf)/(1-tf)) end
            else
                hw=halfBase*(1-t)
            end
            if hw>0.4 then
                mp("Gable",m,Vector3.new(0.7,rise/steps+0.16,hw*2),
                   fr*CFrame.new(cx+xs*0.35,gy,cz),SP,shade(cor,-8),0)
                mp("Barge",m,Vector3.new(0.4,rise/steps+0.2,hw*2+0.5),
                   fr*CFrame.new(cx+xs*0.9,gy,cz),SP,trim,0)
            end
        end
    end

    -- ─── ESQUADRIA ──────────────────────────────────────────────────────
    -- cf aponta para FORA da parede (o vidro fica em +Z local do cf).
    local function janelaMod(m,cf,wid,hei,tipo,trim,corPostigo)
        mp("Sill",m,Vector3.new(wid+1.6,0.4,0.9),cf*CFrame.new(0,-hei/2-0.3,0.2),SP,trim,0)
        for sx=-1,1,2 do
            mp("Jamb",m,Vector3.new(0.55,hei+0.8,0.7),cf*CFrame.new(sx*(wid/2+0.3),0,0.1),SP,trim,0)
        end
        mp("Glass",m,Vector3.new(wid,hei,0.25),cf*CFrame.new(0,0,0.05),GL,gC,0.32)
        mp("MullV",m,Vector3.new(0.2,hei,0.4),cf*CFrame.new(0,0,0.12),SP,trim,0)
        for k=1,2 do
            mp("MullH",m,Vector3.new(wid,0.18,0.4),cf*CFrame.new(0,-hei/2+k*hei/3,0.12),SP,trim,0)
        end
        if tipo=="arco" then
            -- Verga em arco montada com blocos girados (aduelas)
            for a=0,6 do
                local ang=math.rad(180*a/6)
                mp("Arch",m,Vector3.new(wid/6+0.25,0.55,0.7),
                   cf*CFrame.new(-math.cos(ang)*wid/2,hei/2+math.sin(ang)*wid/2*0.55,0.15)
                     *CFrame.Angles(0,0,-(ang-math.pi/2)*0.55),SP,trim,0)
            end
        else
            mp("Header",m,Vector3.new(wid+1.6,0.5,0.8),cf*CFrame.new(0,hei/2+0.35,0.15),SP,trim,0)
        end
        if tipo=="postigo" then
            for sx=-1,1,2 do
                mp("Shutter",m,Vector3.new(1.5,hei+0.4,0.3),
                   cf*CFrame.new(sx*(wid/2+1.1),0,0.2),SP,corPostigo,0)
                for sl=0,4 do
                    mp("Slat",m,Vector3.new(1.3,0.2,0.4),
                       cf*CFrame.new(sx*(wid/2+1.1),-hei/2+0.5+sl*(hei/5),0.35),
                       SP,shade(corPostigo,-12),0)
                end
            end
        end
    end

    -- ─── ACABAMENTO DA FACHADA ──────────────────────────────────────────
    -- O material NUNCA muda em face grande: material texturizado repete a
    -- textura e vira xadrez (erro 5.7). A "textura" aqui e feita de pecas.
    local function fachada(m,fr,cx,cz,w,d,h,y0,estilo,cor,trim,vaoX,vaoH)
        -- Casca solida fina (a casa fica OCA por dentro)
        for sz=-1,1,2 do
            if sz==1 and vaoX then
                -- Frente com o vao da porta aberto
                mp("Wall",m,Vector3.new(math.max(vaoX-1.9+w/2,0.4),h,0.6),
                   fr*CFrame.new(cx+(-w/2+(vaoX-1.9+w/2)/2),y0+h/2,cz+d/2),SP,cor,0)
                mp("Wall",m,Vector3.new(math.max(w/2-vaoX-1.9,0.4),h,0.6),
                   fr*CFrame.new(cx+(vaoX+1.9+(w/2-vaoX-1.9)/2),y0+h/2,cz+d/2),SP,cor,0)
                mp("Lintel",m,Vector3.new(3.8,h-vaoH,0.6),
                   fr*CFrame.new(cx+vaoX,y0+vaoH+(h-vaoH)/2,cz+d/2),SP,cor,0)
            else
                mp("Wall",m,Vector3.new(w,h,0.6),fr*CFrame.new(cx,y0+h/2,cz+sz*d/2),SP,cor,0)
            end
        end
        for sx=-1,1,2 do
            mp("Wall",m,Vector3.new(0.6,h,d),fr*CFrame.new(cx+sx*w/2,y0+h/2,cz),SP,cor,0)
        end

        if estilo=="lambril" then
            local rows=math.floor(h/0.9)
            for b=0,rows-1 do
                local by=y0+0.55+b*0.9
                local c=shade(cor,(b%2==0) and 0 or -7)
                for sz=-1,1,2 do
                    if sz==1 and vaoX and by<vaoH then
                        mp("Board",m,Vector3.new(math.max(vaoX-1.9+w/2,0.4),0.9,0.45),
                           fr*CFrame.new(cx+(-w/2+(vaoX-1.9+w/2)/2),by,cz+d/2+0.25),SP,c,0)
                        mp("Board",m,Vector3.new(math.max(w/2-vaoX-1.9,0.4),0.9,0.45),
                           fr*CFrame.new(cx+(vaoX+1.9+(w/2-vaoX-1.9)/2),by,cz+d/2+0.25),SP,c,0)
                    else
                        mp("Board",m,Vector3.new(w,0.9,0.45),fr*CFrame.new(cx,by,cz+sz*(d/2+0.25)),SP,c,0)
                    end
                end
                for sx=-1,1,2 do
                    mp("Board",m,Vector3.new(0.45,0.9,d),fr*CFrame.new(cx+sx*(w/2+0.25),by,cz),SP,c,0)
                end
            end
        elseif estilo=="tijolo" then
            local rows=math.floor(h/1.15)
            local cols=5
            for b=0,rows-1 do
                local by=y0+0.7+b*1.15
                local off=(b%2==0) and 0 or 0.5
                for sz=-1,1,2 do
                    local step=w/cols
                    for cc=0,cols-1 do
                        local bx=cx-w/2+step*(cc+0.5+off*0.5)
                        if bx<cx+w/2-0.5 then
                            if not (sz==1 and vaoX and by<vaoH and math.abs(bx-cx-vaoX)<2.4) then
                                mp("Brick",m,Vector3.new(step-0.18,1.02,0.42),
                                   fr*CFrame.new(bx,by,cz+sz*(d/2+0.22)),SP,
                                   shade(cor,((cc+b)%3)*8-8),0)
                            end
                        end
                    end
                end
                for sx=-1,1,2 do
                    local step=d/4
                    for cc=0,3 do
                        mp("Brick",m,Vector3.new(0.42,1.02,step-0.18),
                           fr*CFrame.new(cx+sx*(w/2+0.22),by,cz-d/2+step*(cc+0.5+off*0.5)),SP,
                           shade(cor,((cc+b)%3)*8-8),0)
                    end
                end
            end
        elseif estilo=="pedra" then
            -- Embasamento em pedra ate 40% da altura, reboco liso acima
            local rows=math.max(3,math.floor(h*0.42/1.05))
            local cols=4
            for b=0,rows-1 do
                local by=y0+0.65+b*1.05
                for sz=-1,1,2 do
                    local step=w/cols
                    for cc=0,cols-1 do
                        local bx=cx-w/2+step*(cc+0.5)
                        if not (sz==1 and vaoX and math.abs(bx-cx-vaoX)<2.4) then
                            mp("Stone",m,Vector3.new(step-0.2,0.95,0.5),
                               fr*CFrame.new(bx,by,cz+sz*(d/2+0.26)),SP,
                               Color3.fromRGB(126+((cc*3+b)%4)*9,122+((cc*3+b)%4)*8,114+((cc*3+b)%4)*8),0)
                        end
                    end
                end
                for sx=-1,1,2 do
                    for cc=0,2 do
                        mp("Stone",m,Vector3.new(0.5,0.95,d/3-0.2),
                           fr*CFrame.new(cx+sx*(w/2+0.26),by,cz-d/2+(d/3)*(cc+0.5)),SP,
                           Color3.fromRGB(126+((cc+b)%4)*9,122+((cc+b)%4)*8,114+((cc+b)%4)*8),0)
                    end
                end
            end
            mp("Band",m,Vector3.new(w+1.1,0.5,d+1.1),fr*CFrame.new(cx,y0+0.65+rows*1.05,cz),SP,trim,0)
        else -- reboco: liso, com cimalha, rodape e cantoneiras almofadadas
            mp("Plinth",m,Vector3.new(w+1.2,1.1,d+1.2),fr*CFrame.new(cx,y0+0.55,cz),SP,shade(cor,-22),0)
            mp("Band",m,Vector3.new(w+0.9,0.55,d+0.9),fr*CFrame.new(cx,y0+h*0.52,cz),SP,trim,0)
            mp("Cornice",m,Vector3.new(w+1.5,0.7,d+1.5),fr*CFrame.new(cx,y0+h-0.4,cz),SP,trim,0)
            for sx=-1,1,2 do for sz=-1,1,2 do
                for q=0,math.floor(h/1.4)-1 do
                    mp("Quoin",m,Vector3.new((q%2==0) and 1.9 or 1.3,1.3,(q%2==0) and 1.3 or 1.9),
                       fr*CFrame.new(cx+sx*(w/2+0.1),y0+0.9+q*1.4,cz+sz*(d/2+0.1)),SP,trim,0)
                end
            end end
        end
        -- Cantoneiras (sempre): fecham a junta entre as faces
        for sx=-1,1,2 do for sz=-1,1,2 do
            mp("Corner",m,Vector3.new(1.1,h,1.1),
               fr*CFrame.new(cx+sx*(w/2+0.15),y0+h/2,cz+sz*(d/2+0.15)),SP,trim,0)
        end end
    end

    -- ─── MOBILIA DO INTERIOR ────────────────────────────────────────────
    -- Estava na lista de pendencias desde a v0.8.5: a casa era oca e vazia.
    local function mobiliar(m,fr,cx,cz,w,d,y0,corMad,corTec)
        -- Sala: sofa, mesa de centro, tapete, estante
        mp("Rug",m,Vector3.new(w*0.45,0.08,d*0.4),fr*CFrame.new(cx-w*0.2,y0+0.75,cz+d*0.1),SP,shade(corTec,-30),0)
        mp("SofaBase",m,Vector3.new(6.4,1.5,2.6),fr*CFrame.new(cx-w*0.2,y0+1.45,cz+d*0.28),SP,corTec,0)
        mp("SofaBack",m,Vector3.new(6.4,2.0,0.7),fr*CFrame.new(cx-w*0.2,y0+2.2,cz+d*0.28+1.3),SP,corTec,0)
        for sx=-1,1,2 do
            mp("SofaArm",m,Vector3.new(0.7,1.9,2.6),fr*CFrame.new(cx-w*0.2+sx*3.2,y0+2.0,cz+d*0.28),SP,shade(corTec,-14),0)
        end
        mp("CTable",m,Vector3.new(3.2,0.25,1.8),fr*CFrame.new(cx-w*0.2,y0+1.7,cz+d*0.05),SP,corMad,0)
        for sx=-1,1,2 do
            mp("CLeg",m,Vector3.new(0.25,1.0,0.25),fr*CFrame.new(cx-w*0.2+sx*1.3,y0+1.1,cz+d*0.05),SP,shade(corMad,-25),0)
        end
        mp("Shelf",m,Vector3.new(0.6,5.2,4.2),fr*CFrame.new(cx-w/2+1.1,y0+3.2,cz-d*0.15),SP,corMad,0)
        for sh=0,3 do
            mp("Book",m,Vector3.new(0.5,0.9,3.4),fr*CFrame.new(cx-w/2+1.5,y0+1.4+sh*1.2,cz-d*0.15),SP,
               Color3.fromRGB(150+sh*22,90+sh*18,70+sh*10),0)
        end
        -- Cozinha: bancada com armarios e pia
        mp("Counter",m,Vector3.new(w*0.36,0.35,2.4),fr*CFrame.new(cx+w*0.26,y0+3.1,cz-d/2+1.7),SP,Color3.fromRGB(228,224,216),0)
        mp("Cabinet",m,Vector3.new(w*0.36,2.6,2.4),fr*CFrame.new(cx+w*0.26,y0+1.9,cz-d/2+1.7),SP,corMad,0)
        for cd=0,3 do
            mp("CabDoor",m,Vector3.new(w*0.36/4-0.2,2.2,0.2),
               fr*CFrame.new(cx+w*0.26-w*0.18+(w*0.36/4)*(cd+0.5),y0+1.9,cz-d/2+2.95),SP,shade(corMad,12),0)
        end
        mp("Sink",m,Vector3.new(2.2,0.3,1.6),fr*CFrame.new(cx+w*0.26,y0+3.3,cz-d/2+1.7),Enum.Material.Metal,Color3.fromRGB(196,198,200),0)
        -- Mesa de jantar com quatro cadeiras
        local tp=fr*CFrame.new(cx+w*0.22,y0,cz+d*0.18)
        mp("Table",m,Vector3.new(4.4,0.25,3.0),tp*CFrame.new(0,2.6,0),SP,corMad,0)
        for sx=-1,1,2 do for sz=-1,1,2 do
            mp("TLeg",m,Vector3.new(0.3,2.4,0.3),tp*CFrame.new(sx*1.9,1.3,sz*1.2),SP,shade(corMad,-25),0)
        end end
        for ch=0,3 do
            local a=math.rad(ch*90)
            local px,pz=math.sin(a)*3.4,math.cos(a)*3.4
            mp("Chair",m,Vector3.new(1.4,0.2,1.4),tp*CFrame.new(px,1.9,pz),SP,shade(corMad,18),0)
            mp("ChairBack",m,Vector3.new(1.4,1.7,0.2),tp*CFrame.new(px*1.22,2.7,pz*1.22)*CFrame.Angles(0,a,0),SP,shade(corMad,18),0)
            for lg=-1,1,2 do
                mp("ChLeg",m,Vector3.new(0.2,1.8,0.2),tp*CFrame.new(px+lg*0.5,1.0,pz),SP,shade(corMad,-20),0)
            end
        end
    end

    local function mobiliarQuarto(m,fr,cx,cz,w,d,y0,corMad,corTec)
        mp("Bed",m,Vector3.new(5.4,1.3,7.2),fr*CFrame.new(cx-w*0.22,y0+1.4,cz),SP,corMad,0)
        mp("Mattress",m,Vector3.new(5.0,1.0,6.8),fr*CFrame.new(cx-w*0.22,y0+2.4,cz),SP,Color3.fromRGB(238,236,230),0)
        mp("Quilt",m,Vector3.new(5.2,0.35,4.6),fr*CFrame.new(cx-w*0.22,y0+2.95,cz-1.1),SP,corTec,0)
        for pw=-1,1,2 do
            mp("Pillow",m,Vector3.new(2.2,0.5,1.4),fr*CFrame.new(cx-w*0.22+pw*1.2,y0+3.05,cz+2.7),SP,Color3.fromRGB(246,244,238),0)
        end
        mp("Head",m,Vector3.new(5.4,2.6,0.4),fr*CFrame.new(cx-w*0.22,y0+2.6,cz+3.7),SP,shade(corMad,-18),0)
        mp("Night",m,Vector3.new(1.8,1.8,1.8),fr*CFrame.new(cx-w*0.22+3.6,y0+1.7,cz+3.0),SP,corMad,0)
        mp("Lamp",m,Vector3.new(0.9,1.2,0.9),fr*CFrame.new(cx-w*0.22+3.6,y0+3.2,cz+3.0),Enum.Material.Neon,Color3.fromRGB(255,232,190),0)
        mp("Wardrobe",m,Vector3.new(2.2,6.0,5.0),fr*CFrame.new(cx+w*0.28,y0+3.8,cz-d*0.1),SP,corMad,0)
        for dz2=-1,1,2 do
            mp("WDoor",m,Vector3.new(0.25,5.4,2.2),fr*CFrame.new(cx+w*0.28-1.2,y0+3.8,cz-d*0.1+dz2*1.2),SP,shade(corMad,14),0)
        end
        mp("Desk",m,Vector3.new(4.0,0.25,2.0),fr*CFrame.new(cx,y0+2.7,cz-d/2+1.6),SP,corMad,0)
        for sx=-1,1,2 do
            mp("DLeg",m,Vector3.new(0.25,2.6,0.25),fr*CFrame.new(cx+sx*1.7,y0+1.4,cz-d/2+1.6),SP,shade(corMad,-22),0)
        end
    end

    -- ═══ A CASA ═════════════════════════════════════════════════════════
    -- lote = {w=,d=} (opcional). Quando vem preenchido a casa ganha muro
    -- frontal, portao, caminho ate a calcada e caixa de correio — e o que
    -- transforma casas soltas em bairro.
    local function mkHouse(bp,yaw,hs,lote,simples)
        if assetPlace(ASSET_HOUSE,bp,yaw) then task.wait(); return end
        if kitPlace("Casa",bp,yaw) then task.wait(); return end
        hs=hs or 1
        local m=Instance.new("Model"); m.Name="Casa"; m.Parent=buildingsFolder
        local r=CFrame.new(bp)*CFrame.Angles(0,math.rad(yaw),0)
        -- LEMBRETE: em Roblox LookVector e -Z. A frente da casa (porta,
        -- alpendre) fica em +Z LOCAL, ou seja no sentido -LookVector.

        local planta  = hpick(hs, 1,{"I","I","I","L","L","U"})
        local pav     = hpick(hs, 2,{1,1,1,2,2})
        local telhado = hpick(hs, 3,{"duas","duas","quatro","uma","mansarda"})
        local anexo   = hpick(hs, 4,{"garagem","varanda","alpendre","alpendre","nenhum"})
        local acabam  = hpick(hs, 5,{"lambril","tijolo","reboco","pedra"})
        local esquad  = hpick(hs, 6,{"postigo","liso","arco"})
        local quintal = hpick(hs, 7,{"piscina","jardim","deck","horta","nenhum"})

        -- Paleta: parede, telha, postigo/madeira
        local PAL={
            {Color3.fromRGB(238,232,220),Color3.fromRGB(112,56,44),Color3.fromRGB(72,94,86)},
            {Color3.fromRGB(214,196,168),Color3.fromRGB(78,72,68),Color3.fromRGB(96,80,60)},
            {Color3.fromRGB(196,186,172),Color3.fromRGB(96,60,48),Color3.fromRGB(58,72,92)},
            {Color3.fromRGB(226,214,196),Color3.fromRGB(64,78,84),Color3.fromRGB(120,72,56)},
            {Color3.fromRGB(180,196,190),Color3.fromRGB(102,52,42),Color3.fromRGB(70,66,62)},
            {Color3.fromRGB(232,214,196),Color3.fromRGB(122,66,50),Color3.fromRGB(46,86,78)},
            {Color3.fromRGB(206,208,204),Color3.fromRGB(86,48,42),Color3.fromRGB(104,92,72)},
            {Color3.fromRGB(222,206,178),Color3.fromRGB(70,84,90),Color3.fromRGB(88,64,52)},
        }
        local pal=PAL[1+math.floor(hval(hs,8)*#PAL)%#PAL]
        local wCol,tileCol,accCol=pal[1],pal[2],pal[3]
        if acabam=="tijolo" then wCol=Color3.fromRGB(158,92,72) end
        local trim=Color3.fromRGB(250,248,244)
        local corMad=Color3.fromRGB(146,106,70)

        -- Proporcoes sorteadas
        local W=24+math.floor(hval(hs,11)*4)*2      -- 24..30
        local D=17+math.floor(hval(hs,12)*3)*2      -- 17..21
        -- Trava de seguranca: a fachada nunca pode passar da testada do lote
        -- menos o afastamento lateral, senao a casa invade a divisa.
        if lote and lote.w then W=math.max(18,math.min(W,lote.w-6)) end
        local FH=9.2
        local H=FH*pav
        local ov=1.8

        -- ─── VOLUMES ────────────────────────────────────────────────────
        -- As alas sempre projetam para TRAS (-Z), preservando a fachada
        -- limpa voltada para a rua.
        local vols={{cx=0,cz=0,w=W,d=D,h=H,main=true}}
        local alaW=math.floor(W*0.42)
        local alaD=math.floor(D*0.75)
        if planta=="L" then
            local lado=(hval(hs,13)<0.5) and -1 or 1
            table.insert(vols,{cx=lado*(W/2-alaW/2),cz=-(D/2+alaD/2-1),w=alaW,d=alaD,h=FH,main=false})
        elseif planta=="U" then
            for _,lado in {-1,1} do
                table.insert(vols,{cx=lado*(W/2-alaW/2),cz=-(D/2+alaD/2-1),w=alaW,d=alaD,h=FH,main=false})
            end
        end

        -- ─── FUNDACAO ───────────────────────────────────────────────────
        -- Assenta no ponto MAIS ALTO e deixa a fundacao preencher o vao
        -- abaixo (erro 5.5: no ponto mais baixo o predio afundava).
        -- Fundacao funda de proposito: a casa assenta no ponto mais ALTO da
        -- pegada e a saia preenche todo o vao ate o ponto mais baixo. Como
        -- fica enterrada, sobrar altura nao custa nada; faltar deixa a casa
        -- pendurada no ar de um lado.
        for _,v in vols do
            mp("Base",m,Vector3.new(v.w+2.6,34,v.d+2.6),r*CFrame.new(v.cx,-17.2,v.cz),SP,Color3.fromRGB(118,114,108),0)
        end
        for k=0,11 do
            local bx=-W/2+1.2+k*((W-2.4)/11)
            mp("Plinth",m,Vector3.new((W-2.4)/11-0.15,1.4,0.45),
               r*CFrame.new(bx,0.25+(k%2)*0.06,D/2+1.35),SP,
               Color3.fromRGB(130+(k%3)*8,126+(k%3)*8,120+(k%3)*8),0)
        end

        -- ─── CORPO ──────────────────────────────────────────────────────
        local vaoX=-1.0                     -- posicao da porta na fachada
        local vaoH=7.0
        for vi,v in vols do
            -- Na versao simplificada o revestimento vira reboco liso: e o
            -- unico acabamento que nao multiplica pecas por fiada.
            fachada(m,r,v.cx,v.cz,v.w,v.d,v.h,0,(simples and "reboco" or acabam),wCol,trim,
                    v.main and vaoX or nil, vaoH)
            -- Piso e forro de cada pavimento
            for f=0,math.floor(v.h/FH)-1 do
                mp("Floor",m,Vector3.new(v.w-1.2,0.6,v.d-1.2),r*CFrame.new(v.cx,f*FH+0.3,v.cz),SP,corMad,0)
                local cols=(simples and 0 or 10)
                for fp=0,cols-1 do
                    mp("Plank",m,Vector3.new((v.w-1.2)/cols-0.08,0.14,v.d-1.2),
                       r*CFrame.new(v.cx-v.w/2+0.6+((v.w-1.2)/cols)*(fp+0.5),f*FH+0.67,v.cz),SP,
                       shade(corMad,((fp%3)*7)-4),0)
                end
                mp("Ceiling",m,Vector3.new(v.w-1.2,0.5,v.d-1.2),r*CFrame.new(v.cx,(f+1)*FH-0.25,v.cz),SP,Color3.fromRGB(248,246,240),0)
                if not simples then
                    ml(m,(r*CFrame.new(v.cx,(f+1)*FH-1.4,v.cz)).Position,Color3.fromRGB(255,236,200),15,0.45)
                end
            end
            if vi==1 then task.wait() end
        end
        -- Divisoria interna e mobilia
        mp("InnerWall",m,Vector3.new(0.5,FH-1,D-6),r*CFrame.new(W*0.06,FH/2,-2),SP,Color3.fromRGB(244,240,232),0)
        if not simples then mobiliar(m,r,0,0,W,D,0,corMad,accCol) end
        if pav==2 then
            if not simples then mobiliarQuarto(m,r,0,0,W,D,FH,corMad,accCol) end
            -- Escada interna ligando os pavimentos
            for st=0,13 do
                mp("IStep",m,Vector3.new(4.2,0.45,1.1),
                   r*CFrame.new(W/2-3.4,0.9+st*(FH-1)/14,-D/2+2.0+st*1.05),SP,shade(corMad,-10),0)
            end
        end
        task.wait()

        -- ─── TELHADO ────────────────────────────────────────────────────
        local rows,cols=(simples and 4 or 7),(simples and 4 or 6)
        local function telhaVolume(v,tipo)
            local larg=v.w+ov*2
            local run=v.d/2+ov
            local rise=(tipo=="uma") and (v.d*0.28) or (run*0.62)
            if tipo=="duas" then
                for dz=-1,1,2 do
                    agua(m,r,v.cx,v.cz,larg,run,rise,v.h,dz,tileCol,rows,cols,0)
                end
                for sx=-1,1,2 do
                    empena(m,r,v.cx+sx*(v.w/2+ov-0.35),v.cz,sx,(v.d+ov*2)/2,v.h,rise,wCol,trim,"duas",0,0)
                end
                mp("Ridge",m,Vector3.new(larg+0.8,0.85,1.6),r*CFrame.new(v.cx,v.h+rise+0.25,v.cz),SP,Color3.fromRGB(56,52,50),0)
            elseif tipo=="quatro" then
                -- Quatro aguas com a MESMA inclinacao nos quatro lados:
                -- a cumeeira mede w-d e as aguas longas sao trapezios.
                local tap=run
                for dz=-1,1,2 do
                    agua(m,r,v.cx,v.cz,larg,run,rise,v.h,dz,tileCol,rows,cols,tap)
                end
                local ridgeHalf=math.max((v.w-v.d)/2,0.5)
                local fr90=r*CFrame.new(v.cx,0,v.cz)*CFrame.Angles(0,math.rad(90),0)
                for dz=-1,1,2 do
                    agua(m,fr90,0,dz*ridgeHalf,v.d+ov*2,run,rise,v.h,dz,tileCol,rows,5,(v.d+ov*2)/2)
                end
                mp("Ridge",m,Vector3.new(ridgeHalf*2+1.2,0.85,1.6),r*CFrame.new(v.cx,v.h+rise+0.25,v.cz),SP,Color3.fromRGB(56,52,50),0)
            elseif tipo=="uma" then
                -- Uma agua: cumeeira no fundo, caimento para a rua
                agua(m,r,v.cx,v.cz-v.d/2-ov,larg,v.d+ov*2,rise,v.h,1,tileCol,rows+2,cols,0)
                for sx=-1,1,2 do
                    -- Triangulo lateral (a agua unica deixa os dois lados abertos)
                    local steps=10
                    for g=0,steps-1 do
                        local t=(g+0.5)/steps
                        local hw=(v.d+ov*2)*(1-t)/2
                        mp("Gable",m,Vector3.new(0.7,rise/steps+0.16,hw*2),
                           r*CFrame.new(v.cx+sx*(v.w/2+ov-0.35),v.h+t*rise,v.cz-v.d/2-ov+hw),SP,shade(wCol,-8),0)
                    end
                end
                mp("BackWall",m,Vector3.new(larg,rise,0.7),r*CFrame.new(v.cx,v.h+rise/2,v.cz-v.d/2-ov),SP,shade(wCol,-6),0)
            else -- mansarda (gambrel): agua baixa ingreme + agua alta suave
                local runB=run*0.42
                local runA=run-runB
                local riseB=runB*1.55
                local riseA=runA*0.42
                for dz=-1,1,2 do
                    agua(m,r,v.cx,v.cz+dz*runA,larg,runB,riseB,v.h,dz,tileCol,4,cols,0)
                    agua(m,r,v.cx,v.cz,larg-0.4,runA,riseA,v.h+riseB,dz,tileCol,4,cols,0)
                end
                for sx=-1,1,2 do
                    empena(m,r,v.cx+sx*(v.w/2+ov-0.35),v.cz,sx,(v.d+ov*2)/2,v.h,riseB+riseA,wCol,trim,
                           "mansarda",riseB/(riseB+riseA),runA/run)
                end
                mp("Ridge",m,Vector3.new(larg+0.8,0.85,1.6),r*CFrame.new(v.cx,v.h+riseB+riseA+0.25,v.cz),SP,Color3.fromRGB(56,52,50),0)
                -- Mansarda pede agua-furtada: janela saliente no plano ingreme
                local dw=4.2
                mp("DormerBox",m,Vector3.new(dw+1.6,riseB+1.2,3.6),
                   r*CFrame.new(v.cx,v.h+riseB*0.55,v.cz+runA+runB*0.55),SP,wCol,0)
                mp("DormerRoof",m,Vector3.new(dw+2.6,0.5,4.2),
                   r*CFrame.new(v.cx,v.h+riseB+0.75,v.cz+runA+runB*0.55),SP,shade(tileCol,-12),0)
                janelaMod(m,r*CFrame.new(v.cx,v.h+riseB*0.5,v.cz+runA+runB*0.55+1.9),dw*0.7,2.6,"liso",trim,accCol)
            end
            -- Calha e beiral
            for dz=-1,1,2 do
                mp("Gutter",m,Vector3.new(larg,0.45,0.55),r*CFrame.new(v.cx,v.h-0.15,v.cz+dz*(run+0.3)),SP,Color3.fromRGB(88,84,80),0)
            end
        end
        for vi,v in vols do
            telhaVolume(v, v.main and telhado or "duas")
            if vi%2==0 then task.wait() end
        end
        task.wait()

        -- ─── CHAMINE ────────────────────────────────────────────────────
        if hval(hs,14)<0.7 then
            local chx,chz=W/2-4.5,-D/4
            local topo=H+6
            -- O corpo vai ATE o chapeu. Antes ele parava em H+6 e o chapeu
            -- ficava em H+7.2: nas casas com fiada de tijolo os tijolos
            -- tapavam o vao, na versao simplificada (sem tijolo) o chapeu
            -- ficava pairando sozinho sobre o telhado.
            mp("ChimCore",m,Vector3.new(3,H*0.5+8.9,2.6),r*CFrame.new(chx,H*0.75+2.45,chz),SP,Color3.fromRGB(118,76,60),0)
            for br=0,(simples and -1 or 9) do
                for bc=-1,1,2 do
                    mp("Brick",m,Vector3.new(1.5,0.62,2.8),
                       r*CFrame.new(chx+bc*0.78,H+0.6+br*0.66,chz),SP,
                       Color3.fromRGB(128+(br%3)*9,82+(br%3)*7,64+(br%3)*6),0)
                end
            end
            mp("ChimCap",m,Vector3.new(3.8,0.55,3.4),r*CFrame.new(chx,H+7.2,chz),SP,Color3.fromRGB(68,64,62),0)
        end

        -- ─── ESQUADRIAS ─────────────────────────────────────────────────
        -- A janela e sempre colocada na face EXTERNA do volume principal.
        local wid,hei=4.2,3.4
        for f=0,pav-1 do
            local yj=f*FH+FH*0.55
            janelaMod(m,r*CFrame.new(W*0.22,yj,D/2+0.5),wid,hei,esquad,trim,accCol)
            janelaMod(m,r*CFrame.new(-W*0.30,yj,D/2+0.5),wid*0.8,hei,esquad,trim,accCol)
            for sx=-1,1,2 do
                if not simples or sx==1 then
                    janelaMod(m,r*CFrame.new(sx*(W/2+0.5),yj,D*0.12)*CFrame.Angles(0,math.rad(sx*90),0),
                              wid,hei,esquad,trim,accCol)
                end
            end
            if f>0 then
                janelaMod(m,r*CFrame.new(0,yj,-D/2-0.5)*CFrame.Angles(0,math.rad(180),0),wid,hei,esquad,trim,accCol)
            end
        end
        task.wait()

        -- ─── PORTA ──────────────────────────────────────────────────────
        mp("DoorFrame",m,Vector3.new(5.0,7.2,0.9),r*CFrame.new(vaoX,3.6,D/2+0.5),SP,trim,0)
        mp("Door",m,Vector3.new(3.6,6.4,0.35),r*CFrame.new(vaoX,3.2,D/2+0.72),SP,Color3.fromRGB(102,64,40),0)
        for pn=0,3 do
            mp("Panel",m,Vector3.new(1.3,2.0,0.45),
               r*CFrame.new(vaoX+((pn%2)*1.6-0.8),1.7+math.floor(pn/2)*2.6,D/2+0.9),SP,Color3.fromRGB(88,54,34),0)
        end
        mp("Knob",m,Vector3.new(0.35,0.35,0.4),r*CFrame.new(vaoX+1.4,3.2,D/2+1.0),Enum.Material.Metal,Color3.fromRGB(198,178,118),0)
        mp("Number",m,Vector3.new(1.2,0.8,0.2),r*CFrame.new(vaoX+2.6,6.4,D/2+0.85),Enum.Material.Metal,Color3.fromRGB(206,190,140),0)
        if not simples then
            ml(m,(r*CFrame.new(vaoX+2.6,7.2,D/2+1.0)).Position,Color3.fromRGB(255,216,150),14,0.6)
        end

        -- ─── ANEXO ──────────────────────────────────────────────────────
        local frenteZ=D/2      -- borda da fachada em Z local
        local acessoZ=frenteZ  -- de onde a escada desce ate o solo
        if anexo=="alpendre" then
            local pD=6.4
            mp("DeckSkirt",m,Vector3.new(W+2,34,pD),r*CFrame.new(0,-16.9,frenteZ+pD/2),SP,Color3.fromRGB(126,122,116),0)
            mp("Deck",m,Vector3.new(W+2,0.5,pD),r*CFrame.new(0,0.45,frenteZ+pD/2),SP,corMad,0)
            for pl=0,(simples and -1 or 11) do
                mp("Plank",m,Vector3.new((W+2)/12-0.08,0.14,pD),
                   r*CFrame.new(-W/2-1+((W+2)/12)*(pl+0.5),0.72,frenteZ+pD/2),SP,shade(corMad,(pl%3)*7-4),0)
            end
            mp("PorchRoof",m,Vector3.new(W+2.6,0.5,pD+0.8),r*CFrame.new(0,7.0,frenteZ+pD/2),SP,shade(tileCol,-10),0)
            for st=0,7 do
                mp("PorchTile",m,Vector3.new((W+2.6)/8-0.08,0.22,pD+0.8),
                   r*CFrame.new(-W/2-1.3+((W+2.6)/8)*(st+0.5),7.3,frenteZ+pD/2),SP,shade(tileCol,(st%2==0) and 10 or -8),0)
            end
            for sx=-1,1,2 do
                mp("Post",m,Vector3.new(0.8,6.6,0.8),r*CFrame.new(sx*(W/2-0.4),3.6,frenteZ+pD-0.5),SP,trim,0)
                mp("PostCap",m,Vector3.new(1.2,0.4,1.2),r*CFrame.new(sx*(W/2-0.4),6.9,frenteZ+pD-0.5),SP,trim,0)
                mp("Bracket",m,Vector3.new(1.6,1.6,0.5),r*CFrame.new(sx*(W/2-1.4),6.2,frenteZ+pD-0.5),SP,trim,0)
            end
            -- Guarda-corpo PARTIDO: o vao de 8 studs deixa a escada livre.
            -- Erro 5.10: na v0.8.6 o corrimao passava na frente dos degraus.
            for sx=-1,1,2 do
                local trecho=(W+2)/2-4
                mp("Rail",m,Vector3.new(trecho,0.35,0.5),
                   r*CFrame.new(sx*(trecho/2+4)+vaoX,2.5,frenteZ+pD-0.3),SP,trim,0)
            end
            for bl=0,(simples and 7 or 15) do
                local bx=-W/2-0.5+bl*((W+1)/(simples and 7 or 15))
                if math.abs(bx-vaoX)>4 then
                    mp("Baluster",m,Vector3.new(0.28,1.8,0.28),r*CFrame.new(bx,1.55,frenteZ+pD-0.3),SP,trim,0)
                end
            end
            for sx=-1,1,2 do
                mp("RailPost",m,Vector3.new(0.6,2.7,0.6),r*CFrame.new(vaoX+sx*4,1.6,frenteZ+pD-0.3),SP,trim,0)
            end
            -- Duas cadeiras de varanda
            for sx=-1,1,2 do
                mp("PChair",m,Vector3.new(1.6,0.2,1.6),r*CFrame.new(sx*(W/2-3.5),1.5,frenteZ+pD*0.5),SP,shade(corMad,16),0)
                mp("PChairBack",m,Vector3.new(1.6,1.8,0.2),r*CFrame.new(sx*(W/2-3.5),2.4,frenteZ+pD*0.5-0.7),SP,shade(corMad,16),0)
            end
            acessoZ=frenteZ+pD+0.4
        elseif anexo=="garagem" then
            local gW,gD=13,16
            local lado=(hval(hs,15)<0.5) and -1 or 1
            local gx=lado*(W/2+gW/2+0.6)
            mp("GarBase",m,Vector3.new(gW+2,34,gD+2),r*CFrame.new(gx,-17.1,frenteZ-gD/2),SP,Color3.fromRGB(118,114,108),0)
            fachada(m,r,gx,frenteZ-gD/2,gW,gD,6.8,0,acabam,wCol,trim,nil,0)
            agua(m,r,gx,frenteZ-gD/2,gW+2.4,gD/2+1.2,3.0,6.8,1,tileCol,5,4,0)
            agua(m,r,gx,frenteZ-gD/2,gW+2.4,gD/2+1.2,3.0,6.8,-1,tileCol,5,4,0)
            for sx=-1,1,2 do
                empena(m,r,gx+sx*(gW/2+1.2-0.35),frenteZ-gD/2,sx,(gD+2.4)/2,6.8,3.0,wCol,trim,"duas",0,0)
            end
            -- Porta de enrolar em laminas
            for lm=0,10 do
                mp("GDoor",m,Vector3.new(gW-1.6,0.52,0.35),
                   r*CFrame.new(gx,0.7+lm*0.58,frenteZ+0.35),SP,
                   Color3.fromRGB(196+(lm%2)*10,192+(lm%2)*10,186+(lm%2)*10),0)
            end
            mp("GLintel",m,Vector3.new(gW+0.8,0.9,0.8),r*CFrame.new(gx,7.2,frenteZ+0.4),SP,trim,0)
            -- Entrada de carro ate a rua
            for dv=0,5 do
                mp("Drive",m,Vector3.new(gW-1,1.8,3.4),r*CFrame.new(gx,-0.6,frenteZ+2.2+dv*3.5),SP,
                   Color3.fromRGB(176+(dv%2)*8,172+(dv%2)*8,166+(dv%2)*8),0)
            end
            acessoZ=frenteZ+1.2
        elseif anexo=="varanda" then
            local lado=(hval(hs,16)<0.5) and -1 or 1
            local vW,vD=8.5,D*0.7
            local vx=lado*(W/2+vW/2)
            mp("VarSkirt",m,Vector3.new(vW,34,vD),r*CFrame.new(vx,-16.9,0),SP,Color3.fromRGB(126,122,116),0)
            mp("VarDeck",m,Vector3.new(vW,0.5,vD),r*CFrame.new(vx,0.45,0),SP,corMad,0)
            for pl=0,7 do
                mp("Plank",m,Vector3.new(vW/8-0.08,0.14,vD),r*CFrame.new(vx-vW/2+(vW/8)*(pl+0.5),0.72,0),SP,shade(corMad,(pl%3)*7-4),0)
            end
            mp("VarRoof",m,Vector3.new(vW+1.2,0.5,vD+1.2),r*CFrame.new(vx,7.0,0),SP,shade(tileCol,-10),0)
            for sz=-1,1,2 do
                mp("VarPost",m,Vector3.new(0.7,6.6,0.7),r*CFrame.new(vx+lado*(vW/2-0.5),3.6,sz*(vD/2-0.6)),SP,trim,0)
            end
            for bl=0,9 do
                mp("VarBal",m,Vector3.new(0.28,1.8,0.28),r*CFrame.new(vx+lado*(vW/2-0.4),1.55,-vD/2+(vD/9)*bl),SP,trim,0)
            end
            mp("VarRail",m,Vector3.new(0.45,0.35,vD),r*CFrame.new(vx+lado*(vW/2-0.4),2.5,0),SP,trim,0)
            acessoZ=frenteZ+0.6
        end

        -- Escada descendo ate o terreno. A direcao correta e +Z LOCAL, que
        -- e -LookVector: usar LookVector fazia a escada nascer para dentro
        -- da casa (bug silencioso desde a v0.8.6).
        local pe=r*CFrame.new(vaoX,0.5,acessoZ)
        -- A escada nao pode passar do recuo frontal: alem dele e calcada.
        -- acessoZ muda conforme o anexo: com alpendre a escada ja comeca 17
        -- studs a frente do centro da casa. Medir a corrida a partir DELE, e
        -- nao do centro, e o que impede o ultimo degrau de cair no meio-fio.
        local corridaMax=lote and math.max(3,lote.d-acessoZ-1.5) or 20
        escadaAoSolo(m,pe.Position,-pe.LookVector,5.5,Color3.fromRGB(158,154,148),trim,corridaMax)

        -- ─── QUINTAL ────────────────────────────────────────────────────
        local qz=-(D/2+((planta=="I") and 9 or (alaD+8)))
        -- Altura do terreno num ponto local, para o quintal nao ficar
        -- pendurado quando o fundo do lote e mais baixo que a casa.
        local function soloQuintal(lx,lz2)
            local w=r*CFrame.new(lx,0,lz2)
            local sr=surfaceAt(w.Position.X,w.Position.Z)
            return sr and math.min(0,sr.Position.Y-bp.Y) or 0
        end
        local qy=soloQuintal(0,qz)
        if quintal=="piscina" then
            -- Bacia OCA: antes a saia era um bloco macico ate a borda e a
            -- "agua" era uma placa de vidro em cima. Agora ha vao de verdade
            -- e o vao e preenchido com agua do Terrain.
            mp("PoolSkirt",m,Vector3.new(14,32,10),r*CFrame.new(0,qy-19.4,qz),SP,Color3.fromRGB(140,136,130),0)
            mp("PoolFloor",m,Vector3.new(12.4,0.8,8.4),r*CFrame.new(0,qy-3.0,qz),SP,Color3.fromRGB(196,214,226),0)
            for sx=-1,1,2 do
                mp("PoolWall",m,Vector3.new(0.8,4.0,10),r*CFrame.new(sx*6.6,qy-0.6,qz),SP,Color3.fromRGB(206,220,230),0)
            end
            for sz=-1,1,2 do
                mp("PoolWall",m,Vector3.new(14,4.0,0.8),r*CFrame.new(0,qy-0.6,qz+sz*4.6),SP,Color3.fromRGB(206,220,230),0)
            end
            -- Borda em MOLDURA, nao laje: uma laje inteira taparia a agua.
            for sz=-1,1,2 do
                mp("PoolRim",m,Vector3.new(16,0.7,1.8),r*CFrame.new(0,qy+1.05,qz+sz*5.1),SP,Color3.fromRGB(216,210,200),0)
            end
            for sx=-1,1,2 do
                mp("PoolRim",m,Vector3.new(1.8,0.7,8.4),r*CFrame.new(sx*7.1,qy+1.05,qz),SP,Color3.fromRGB(216,210,200),0)
            end
            encherPiscina(r*CFrame.new(0,qy-0.9,qz),12,8,4)
            for lc=0,9 do
                mp("Coping",m,Vector3.new(1.35,0.28,0.8),r*CFrame.new(-6.75+lc*1.5,qy+1.5,qz+5.1),SP,Color3.fromRGB(234,230,222),0)
            end
            for sx=-1,1,2 do
                mp("Lounger",m,Vector3.new(2.0,0.25,4.6),r*CFrame.new(sx*9.4,qy+1.55,qz),SP,Color3.fromRGB(238,236,230),0)
                mp("LoungerBack",m,Vector3.new(2.0,2.2,0.3),r*CFrame.new(sx*9.4,qy+2.45,qz-2.1)*CFrame.Angles(math.rad(-30),0,0),SP,Color3.fromRGB(238,236,230),0)
            end
        elseif quintal=="jardim" then
            for gx2=-2,2 do for gz2=-1,1 do
                local px,pz=gx2*3.6,qz+gz2*3.6
                mp("Bush",m,Vector3.new(2.6,3.6,2.6),r*CFrame.new(px,soloQuintal(px,pz)+1.1,pz),SP,
                   Color3.fromRGB(66+((gx2+gz2)%3)*12,104+((gx2+gz2)%3)*14,52),0)
            end end
            mp("GardenPath",m,Vector3.new(2.4,1.6,12),r*CFrame.new(0,qy-0.55,qz),SP,Color3.fromRGB(190,184,172),0)
            for sx=-1,1,2 do
                mp("Bench",m,Vector3.new(5.0,0.25,1.6),r*CFrame.new(sx*8,soloQuintal(sx*8,qz)+1.5,qz),SP,corMad,0)
                mp("BenchBack",m,Vector3.new(5.0,1.5,0.2),r*CFrame.new(sx*8,soloQuintal(sx*8,qz)+2.3,qz-0.7),SP,corMad,0)
            end
        elseif quintal=="deck" then
            mp("DeckSkirt2",m,Vector3.new(16,34,12),r*CFrame.new(0,qy-16.9,qz),SP,Color3.fromRGB(126,122,116),0)
            for pl=0,13 do
                mp("DPlank",m,Vector3.new(16/14-0.08,0.35,12),r*CFrame.new(-8+(16/14)*(pl+0.5),qy+0.5,qz),SP,shade(corMad,(pl%3)*7-4),0)
            end
            mp("Table2",m,Vector3.new(4.0,0.25,4.0),r*CFrame.new(0,qy+2.6,qz),SP,shade(corMad,14),0)
            mp("Umbrella",m,Vector3.new(0.35,6.0,0.35),r*CFrame.new(0,qy+3.0,qz),SP,Color3.fromRGB(90,84,78),0)
            for uc=0,5 do
                local a=math.rad(uc*60)
                mp("UmbSeg",m,Vector3.new(3.6,0.2,3.6),r*CFrame.new(math.sin(a)*1.6,qy+6.0,qz+math.cos(a)*1.6)*CFrame.Angles(math.rad(-14),a,0),SP,
                   (uc%2==0) and Color3.fromRGB(198,72,60) or Color3.fromRGB(242,238,230),0)
            end
            for ch=0,3 do
                local a=math.rad(ch*90+45)
                mp("DChair",m,Vector3.new(1.5,0.2,1.5),r*CFrame.new(math.sin(a)*4.2,qy+1.8,qz+math.cos(a)*4.2),SP,shade(corMad,18),0)
                mp("DChairB",m,Vector3.new(1.5,1.6,0.2),r*CFrame.new(math.sin(a)*5.0,qy+2.6,qz+math.cos(a)*5.0)*CFrame.Angles(0,a,0),SP,shade(corMad,18),0)
            end
        elseif quintal=="horta" then
            for bd=0,3 do
                local bz=qz-4.5+bd*3.2
                mp("BedFrame",m,Vector3.new(12,2.4,2.4),r*CFrame.new(0,soloQuintal(0,bz)+0.45,bz),SP,shade(corMad,-18),0)
                mp("Soil",m,Vector3.new(11.2,0.5,1.8),r*CFrame.new(0,soloQuintal(0,bz)+0.85,bz),SP,Color3.fromRGB(96,74,52),0)
                for pl=0,6 do
                    mp("Plant",m,Vector3.new(0.9,1.1,0.9),r*CFrame.new(-5+pl*1.7,soloQuintal(0,bz)+1.5,bz),SP,
                       Color3.fromRGB(74+(pl%3)*14,122+(pl%3)*16,56),0)
                end
            end
            mp("Shed",m,Vector3.new(5,7,4.5),r*CFrame.new(8.5,soloQuintal(8.5,qz)+2.5,qz),SP,shade(corMad,-8),0)
            mp("ShedRoof",m,Vector3.new(6,0.5,5.5),r*CFrame.new(8.5,soloQuintal(8.5,qz)+5.2,qz),SP,shade(tileCol,-14),0)
        end

        -- ─── FRENTE DO LOTE ─────────────────────────────────────────────
        -- Muro baixo, portao, caminho ate a calcada e caixa de correio.
        -- E o que faz um conjunto de casas virar uma rua.
        if lote then
            local lw=lote.w/2
            local lz=lote.d
            local muroC=Color3.fromRGB(224,220,212)
            -- Altura do TERRENO num ponto local do lote. A casa assenta no
            -- ponto mais alto da pegada; o muro na divisa costuma ficar mais
            -- baixo, e sem isto ele nascia no ar (bug visivel na foto).
            local function soloLocal(lx,lz2)
                local w=r*CFrame.new(lx,0,lz2)
                local sr=surfaceAt(w.Position.X,w.Position.Z)
                return sr and (sr.Position.Y-bp.Y) or 0
            end
            local nseg=math.max(4,math.floor(lote.w/3.2))
            for sg=0,nseg-1 do
                local mx=-lw+(lote.w/nseg)*(sg+0.5)
                if math.abs(mx-vaoX)>3.6 then
                    local sy=soloLocal(mx,lz)
                    -- Altura generosa com a base enterrada: em divisa
                    -- inclinada o muro acompanha sem abrir vao embaixo.
                    mp("Wall",m,Vector3.new(lote.w/nseg-0.2,5.0,0.7),r*CFrame.new(mx,sy+0.7,lz),SP,muroC,0)
                    mp("WallCap",m,Vector3.new(lote.w/nseg-0.1,0.3,1.0),r*CFrame.new(mx,sy+3.35,lz),SP,trim,0)
                end
            end
            local syPortao=soloLocal(vaoX,lz)
            for sx=-1,1,2 do
                mp("GatePost",m,Vector3.new(1.2,7.0,1.2),r*CFrame.new(vaoX+sx*3.6,syPortao+1.7,lz),SP,trim,0)
                mp("GatePostCap",m,Vector3.new(1.6,0.4,1.6),r*CFrame.new(vaoX+sx*3.6,syPortao+5.4,lz),SP,trim,0)
            end
            for gb=0,8 do
                mp("GateBar",m,Vector3.new(0.2,2.4,0.2),r*CFrame.new(vaoX-3.0+gb*0.75,syPortao+1.3,lz),Enum.Material.Metal,Color3.fromRGB(66,64,62),0)
            end
            mp("GateTop",m,Vector3.new(6.6,0.24,0.24),r*CFrame.new(vaoX,syPortao+2.5,lz),Enum.Material.Metal,Color3.fromRGB(66,64,62),0)
            -- Caminho da calcada ate a porta, seguindo o desnivel do terreno
            local passos=math.max(2,math.floor((lz-acessoZ)/3.2))
            for pt=0,passos-1 do
                local pzl=acessoZ+((lz-acessoZ)/passos)*(pt+0.5)
                local wp=r*CFrame.new(vaoX,0,pzl)
                local sr=surfaceAt(wp.Position.X,wp.Position.Z)
                local py=sr and (sr.Position.Y+0.2) or bp.Y
                mp("Path",m,Vector3.new(4.2,1.8,(lz-acessoZ)/passos-0.25),
                   CFrame.new(Vector3.new(wp.Position.X,py-0.72,wp.Position.Z))*CFrame.Angles(0,math.rad(yaw),0),SP,
                   Color3.fromRGB(198+(pt%2)*8,194+(pt%2)*8,186+(pt%2)*8),0)
            end
            local syCorreio=soloLocal(vaoX+5.0,lz-0.4)
            mp("MailPost",m,Vector3.new(0.35,5.0,0.35),r*CFrame.new(vaoX+5.0,syCorreio+2.0,lz-0.4),SP,shade(corMad,-16),0)
            mp("MailBox",m,Vector3.new(1.5,1.2,2.4),r*CFrame.new(vaoX+5.0,syCorreio+4.4,lz-0.4),Enum.Material.Metal,accCol,0)
            mp("MailFlag",m,Vector3.new(0.18,1.0,0.6),r*CFrame.new(vaoX+5.7,syCorreio+5.2,lz-0.4),SP,Color3.fromRGB(196,58,48),0)
            -- Cerca viva nas divisas laterais
            for sx=-1,1,2 do
                local nh=math.max(3,math.floor((lz+D/2)/3.4))
                for hb=0,nh-1 do
                    local hz=lz-((lz+D/2)/nh)*(hb+0.5)
                    local sy=soloLocal(sx*lw,hz)
                    mp("Hedge",m,Vector3.new(1.8,5.2,(lz+D/2)/nh-0.2),
                       r*CFrame.new(sx*lw,sy+0.8,hz),SP,
                       Color3.fromRGB(62+(hb%3)*9,98+(hb%3)*12,50),0)
                end
            end
        end
        task.wait()
        return m
    end


    -- Restaurante detalhado (era uma caixa lisa). ~180 pecas.
    local function mkRest(bp,yaw)
        if assetPlace(ASSET_REST,bp,yaw) then task.wait(); return end
        if kitPlace("Restaurante",bp,yaw) then task.wait(); return end
        local m=Instance.new("Model"); m.Name="Restaurante"; m.Parent=buildingsFolder
        local r=CFrame.new(bp)*CFrame.Angles(0,math.rad(yaw),0)
        local rW,rD,rH=26,18,7
        local paredeC=Color3.fromRGB(236,228,214)
        local trimC2=Color3.fromRGB(250,248,244)
        local telhaC=Color3.fromRGB(122,58,46)

        mp("Base",m,Vector3.new(rW+3,34,rD+3),r*CFrame.new(0,-17,0),SL,Color3.fromRGB(122,118,112),0)
        mp("Body",m,Vector3.new(rW,rH,rD),r*CFrame.new(0,rH/2,0),SP,paredeC,0)
        -- Lambril horizontal
        for b=0,7 do
            local tone=(b%2==0) and 0 or -7
            mp("Board",m,Vector3.new(rW+0.3,0.82,rD+0.3),r*CFrame.new(0,0.5+b*0.85,0),SP,
               Color3.fromRGB(math.clamp(paredeC.R*255+tone,0,255),
                              math.clamp(paredeC.G*255+tone,0,255),
                              math.clamp(paredeC.B*255+tone,0,255)),0)
        end
        for sx=-1,1,2 do for sz=-1,1,2 do
            mp("Corner",m,Vector3.new(1.2,rH,1.2),r*CFrame.new(sx*(rW/2+0.1),rH/2,sz*(rD/2+0.1)),SP,trimC2,0)
        end end

        -- Telhado de duas aguas com telhas
        local rrh,rov = 3.6, 1.6
        local rrun = rD/2 + rov
        local rpitch = math.atan(rrh/rrun)
        local rslope = math.sqrt(rrh*rrh + rrun*rrun)
        for sz=-1,1,2 do
            mp("RoofDeck",m,Vector3.new(rW+rov*2,0.5,rslope+0.6),
               r*CFrame.new(0,rH+rrh/2,sz*rrun/2)*CFrame.Angles(sz*rpitch,0,0),SP,Color3.fromRGB(62,56,52),0)
            for row=0,7 do
                local t=(row+0.5)/8
                local shade=(row%2==0) and 12 or -10
                for col=0,7 do
                    mp("Tile",m,Vector3.new((rW+rov*2)/8-0.08,0.25,rslope/8*1.5),
                       r*CFrame.new(-rW/2-rov+((rW+rov*2)/16)+col*((rW+rov*2)/8),
                                    rH+t*rrh+0.35, sz*rrun*(1-t))*CFrame.Angles(sz*rpitch,0,0),SP,
                       Color3.fromRGB(math.clamp(telhaC.R*255+shade,0,255),
                                      math.clamp(telhaC.G*255+shade,0,255),
                                      math.clamp(telhaC.B*255+shade,0,255)),0)
                end
            end
        end
        -- Empenas fechadas
        for sx=-1,1,2 do
            for g=0,9 do
                local t=(g+0.5)/10
                mp("Gable",m,Vector3.new(0.7,rrh/10+0.14,(rD+rov*2)*(1-t)),
                   r*CFrame.new(sx*(rW/2+rov-0.35),rH+t*rrh,0),SP,paredeC,0)
            end
        end
        mp("Ridge",m,Vector3.new(rW+rov*2+0.6,0.8,1.4),r*CFrame.new(0,rH+rrh+0.2,0),SP,Color3.fromRGB(58,54,52),0)
        task.wait()

        -- Fachada de vidro com montantes
        mp("Glass",m,Vector3.new(rW-4,rH-2.4,0.35),r*CFrame.new(0,rH/2+0.4,rD/2+0.25),GL,gC,0.26)
        for mu=0,5 do
            mp("Mull",m,Vector3.new(0.35,rH-2.4,0.55),r*CFrame.new(-rW/2+3+mu*((rW-6)/5),rH/2+0.4,rD/2+0.32),SP,trimC2,0)
        end
        mp("Door",m,Vector3.new(4,5.2,0.4),r*CFrame.new(0,2.6,rD/2+0.4),GL,Color3.fromRGB(58,76,88),0.12)

        -- Terraco com pergolado
        local tD=13
        mp("TerrSkirt",m,Vector3.new(rW+3,34,tD),r*CFrame.new(0,-16.6,rD/2+tD/2),SL,Color3.fromRGB(142,138,132),0)
        mp("Terrace",m,Vector3.new(rW+3,0.4,tD),r*CFrame.new(0,0.4,rD/2+tD/2),WP,Color3.fromRGB(150,110,72),0)
        for pl=0,11 do
            mp("Plank",m,Vector3.new((rW+3)/12-0.08,0.14,tD),
               r*CFrame.new(-rW/2-1.5+((rW+3)/24)+pl*((rW+3)/12),0.62,rD/2+tD/2),SP,
               Color3.fromRGB(156+(pl%3)*6,116+(pl%3)*5,76+(pl%3)*4),0)
        end
        -- Vigas do pergolado
        for vg=0,7 do
            mp("Beam",m,Vector3.new(0.45,0.5,tD),
               r*CFrame.new(-rW/2-1+vg*((rW+2)/7),5.4,rD/2+tD/2),WD,Color3.fromRGB(128,92,58),0)
        end
        for sx=-1,1,2 do for sz=0,1 do
            mp("Post",m,Vector3.new(0.6,5.4,0.6),r*CFrame.new(sx*(rW/2+1),2.7,rD/2+2+sz*(tD-4)),WD,Color3.fromRGB(120,86,54),0)
        end end
        -- Toldo listrado
        for st=0,7 do
            mp("Awning",m,Vector3.new((rW+3)/8-0.05,0.25,4),
               r*CFrame.new(-rW/2-1.5+((rW+3)/16)+st*((rW+3)/8),5.9,rD/2+2.4),SP,
               (st%2==0) and Color3.fromRGB(186,58,48) or Color3.fromRGB(244,240,232),0)
        end
        -- Mesas e cadeiras
        for tx=-1,1 do for tz=0,1 do
            local tp=r*CFrame.new(tx*7.5,0,rD/2+4+tz*5.5)
            mp("Table",m,Vector3.new(3.2,0.18,3.2),tp*CFrame.new(0,2.4,0),SP,Color3.fromRGB(246,242,234),0)
            mp("TLeg",m,Vector3.new(0.35,2.2,0.35),tp*CFrame.new(0,1.3,0),Enum.Material.Metal,Color3.fromRGB(78,74,70),0)
            for ch=0,3 do
                local a=math.rad(ch*90+45)
                mp("Chair",m,Vector3.new(1.3,0.16,1.3),tp*CFrame.new(math.cos(a)*2.5,1.7,math.sin(a)*2.5),SP,Color3.fromRGB(198,192,182),0)
                mp("ChairBack",m,Vector3.new(1.3,1.5,0.2),tp*CFrame.new(math.cos(a)*3.1,2.4,math.sin(a)*3.1)*CFrame.Angles(0,-a,0),SP,Color3.fromRGB(198,192,182),0)
            end
        end end
        -- Guarda-corpo do terraco
        mp("Rail",m,Vector3.new(rW+3,0.3,0.4),r*CFrame.new(0,2.2,rD/2+tD),SP,trimC2,0)
        for bl=0,17 do
            mp("Bal",m,Vector3.new(0.26,1.6,0.26),r*CFrame.new(-rW/2-1.5+bl*((rW+3)/17),1.4,rD/2+tD),SP,trimC2,0)
        end
        -- Escada do terraco ate o terreno
        local degrauRest = (r*CFrame.new(0,0.5,rD/2+tD+0.5))
        escadaAoSolo(m, degrauRest.Position, degrauRest.LookVector, 6,
                     Color3.fromRGB(158,154,148), trimC2)
        mp("Sign",m,Vector3.new(9,1.8,0.3),r*CFrame.new(0,rH-1,rD/2+0.5),Enum.Material.Neon,Color3.fromRGB(255,204,132),0)
        ml(m,(r*CFrame.new(0,5.4,rD/2+tD/2)).Position,Color3.fromRGB(255,198,128),24,0.9)
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
        mp("PlazaSkirt",m,Vector3.new(36,34,36),CFrame.new(bp+Vector3.new(0,-16.9,0)),SL,Color3.fromRGB(146,142,136),0)
        mp("Floor",m,Vector3.new(36,2.0,36),CFrame.new(bp+Vector3.new(0,-0.45,0)),CC,cC,0)
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

    -- Estrada suavizada. Antes cada segmento ficava na altura exata do
    -- terreno naquele ponto, o que produzia ziguezague acompanhando cada
    -- ondulacao. Agora as alturas passam por media movel e a estrada e um
    -- pouco mais alta que o solo, como um leito de verdade.
    local ROAD_LVL=0
    local ROAD_SEG=10   -- comprimento do segmento de leito, em studs
    local function mkRoad(a,b,w)
        ROAD_LVL=ROAD_LVL+1
        local eps=(ROAD_LVL%5)*0.035
        local d=Vector3.new(b.X-a.X,0,b.Z-a.Z); local len=d.Magnitude
        if len<10 then return end
        local steps=math.ceil(len/ROAD_SEG)

        -- 1) Coleta alturas ao longo do tracado
        local pts={}
        for i=0,steps do
            local t=i/steps
            local px,pz=a.X+d.X*t,a.Z+d.Z*t
            local r=surfaceAt(px,pz)
            if r and r.Material~=Enum.Material.Water and r.Position.Y>WATER_Y+0.5 then
                table.insert(pts,{x=px,z=pz,y=r.Position.Y,ok=true})
            else
                table.insert(pts,{x=px,z=pz,y=0,ok=false})
            end
        end

        -- 2) Media movel de 5 pontos para tirar o ziguezague
        local suave={}
        for i=1,#pts do
            if pts[i].ok then
                local soma,n=0,0
                for k=math.max(1,i-2),math.min(#pts,i+2) do
                    if pts[k].ok then soma=soma+pts[k].y; n=n+1 end
                end
                suave[i]={x=pts[i].x,z=pts[i].z,y=soma/n,ok=true}
            else
                suave[i]={ok=false}
            end
        end

        -- 3) Constroi o leito ligando pontos consecutivos validos
        local prev=nil
        local idx=0
        for i=1,#suave do
            local s=suave[i]
            if s.ok then
                -- Limita o quanto a suavizacao pode levantar o leito acima do
                -- terreno real: sem isso a estrada flutuava em encostas.
                local real = surfaceAt(s.x,s.z)
                local yy = s.y
                if real then
                    yy = math.clamp(s.y, real.Position.Y-0.6, real.Position.Y+1.2)
                end
                local cur=Vector3.new(s.x,yy+0.25,s.z)
                -- (a espessura do leito enterra a face de baixo; ver mp("Road"))
                if prev then
                    idx=idx+1
                    local seg=cur-prev
                    local sl=seg.Magnitude
                    if sl>1 and sl<ROAD_SEG*2 then
                        local mid=(cur+prev)/2
                        local dir=seg.Unit
                        local right=Vector3.new(-dir.Z,0,dir.X)
                        -- Asfalto em cinza medio (preto puro ficava pesado)
                        -- Leito grosso com o topo onde estava e a base
                        -- enterrada 1.5 stud. Com 0.5 de espessura a face de
                        -- baixo caia rente ao terreno e as duas superficies
                        -- disputavam o mesmo plano: e o xadrez cinza que
                        -- aparecia na rua (nao era textura, era z-fighting).
                        mp("Road",infraFolder,Vector3.new(w,2.0,sl+1.2),
                           CFrame.lookAt(mid,mid+seg)*CFrame.new(0,-0.75+eps,0),SP,Color3.fromRGB(96,94,92),0)
                        -- Faixa central tracejada
                        if idx % 2 == 0 then
                            mp("Lane",infraFolder,Vector3.new(0.5,0.55,sl*0.55),
                               CFrame.lookAt(mid,mid+seg),SP,Color3.fromRGB(228,222,198),0)
                        end
                        -- Meio-fio e calcada dos dois lados
                        for sx=-1,1,2 do
                            local o=mid+right*(sx*(w/2+0.55))
                            mp("Curb",infraFolder,Vector3.new(1.1,2.2,sl+1.2),
                               CFrame.lookAt(o,o+seg)*CFrame.new(0,-0.72+eps,0),SP,Color3.fromRGB(206,202,194),0)
                            local o2=mid+right*(sx*(w/2+2.6))
                            mp("Walk",infraFolder,Vector3.new(3,2.0,sl+1.2),
                               CFrame.lookAt(o2,o2+seg)*CFrame.new(0,-0.72+eps,0),SP,Color3.fromRGB(188,184,176),0)
                        end
                    end
                end
                prev=cur
            else
                prev=nil
            end
        end
    end
"""

LUA_FOREST_PLAN = r"""
    -- ═══════════════════════════════════════════════════════════════════
    -- APOIO COMPARTILHADO (urbanismo e floresta usam os dois)
    -- ═══════════════════════════════════════════════════════════════════
    -- Amostra a pegada INTEIRA, nao so o centro (erro 5.6: com raio 32 a
    -- piscina do hotel, que fica a 58, ficava pendurada no ar).
    -- Devolve o ponto MAIS ALTO — a fundacao preenche o vao abaixo (5.5).
    -- Folga acima da linha d'agua. Era fixa em 2.5 studs, o que num mapa de
    -- relevo baixo (Metropole tem 2.6 studs no total) reprovava praticamente
    -- o mapa inteiro. Sem agua no mapa, nao ha folga a respeitar.
    local MARGEM_AGUA=(WATER_LEVEL<=0.005) and 0 or math.min(2.5,detectedHeight*0.02)
    local function pegada(x,z,raio,maxDrop)
        local c=surfaceAt(x,z)
        if not c or c.Position.Y<=WATER_Y+MARGEM_AGUA then return nil end
        local lo,hi=c.Position.Y,c.Position.Y
        for a=0,330,60 do
            local ang=math.rad(a)
            for _,rr in {raio*0.55,raio} do
                local s=surfaceAt(x+math.cos(ang)*rr,z+math.sin(ang)*rr)
                if not s or s.Position.Y<=WATER_Y+MARGEM_AGUA*0.4 then return nil end
                if s.Position.Y<lo then lo=s.Position.Y end
                if s.Position.Y>hi then hi=s.Position.Y end
            end
        end
        if (hi-lo)>maxDrop then return nil end
        return hi,(hi-lo)
    end

    -- Remove vegetacao de uma area (a decoracao roda antes das construcoes)
    local function limpar(x,z,raio)
        for _,pasta in {treesFolder,rocksFolder} do
            for _,obj in pasta:GetChildren() do
                local op=nil
                if obj:IsA("Model") then
                    local ok,piv=pcall(function() return obj:GetPivot() end)
                    if ok then op=piv.Position end
                elseif obj:IsA("BasePart") then op=obj.Position end
                if op and (Vector2.new(op.X,op.Z)-Vector2.new(x,z)).Magnitude<raio then
                    obj:Destroy()
                end
            end
        end
    end

    -- ═══════════════════════════════════════════════════════════════════
    -- CABANA NA FLORESTA  (preset "forest_horror")
    -- ═══════════════════════════════════════════════════════════════════
    -- Mesma logica de composicao da casa urbana — planta, telhado e
    -- acabamento sorteados pela seed do ponto — mas com vocabulario de mata:
    -- tronco aparente, tabua torta, telhado de tabuas, alpendre com balanco,
    -- lampiao, lenha empilhada, poco, varal, cerca de estacas.
    -- Quanto mais "abandono", mais tabua faltando, vidro quebrado e musgo.

    local function mkCabin(bp,yaw,hs,abandono)
        if assetPlace(ASSET_HOUSE,bp,yaw) then task.wait(); return end
        if kitPlace("Cabana",bp,yaw) then task.wait(); return end
        abandono = abandono or 0.5
        local m=Instance.new("Model"); m.Name="Cabana"; m.Parent=buildingsFolder
        local r=CFrame.new(bp)*CFrame.Angles(0,math.rad(yaw),0)

        local tipo    = hpick(hs, 1,{"toras","tabuas","tabuas","pedra"})
        local telhado = hpick(hs, 2,{"duas","duas","uma"})
        local anexo   = hpick(hs, 3,{"alpendre","alpendre","lenha","nenhum"})
        local extra   = hpick(hs, 4,{"poco","varal","tocos","nenhum"})

        -- Madeira escurece com o abandono
        local base=Color3.fromRGB(112,84,58)
        local madeira=shade(base,-abandono*30)
        local escura=shade(madeira,-18)
        local telha=shade(Color3.fromRGB(84,72,62),-abandono*22)
        local musgo=Color3.fromRGB(74,92,56)

        -- Cabana era pequena demais: 17x14 com pe-direito 8.4 fica do
        -- tamanho de um galpao e some no meio da mata. Agora tem porte de
        -- casa de campo, com sotao aproveitavel sob o telhado.
        local W=25+math.floor(hval(hs,11)*3)*2      -- 25..29
        local D=20+math.floor(hval(hs,12)*3)*2      -- 20..24
        local H=11.0
        local ov=2.2

        -- Fundacao em pedra bruta, funda o bastante para encosta de mata
        mp("Base",m,Vector3.new(W+2.4,30,D+2.4),r*CFrame.new(0,-15.2,0),SP,Color3.fromRGB(96,92,86),0)
        -- Embasamento em pedra bruta contornando a casa toda, nao so a frente
        for _,lado in {{-1,"z"},{1,"z"},{-1,"x"},{1,"x"}} do
            local n=(lado[2]=="z") and 9 or 7
            for k=0,n-1 do
                local t=(k+0.5)/n
                local px,pz,sx2,sz2
                if lado[2]=="z" then
                    px=-W/2+t*W; pz=lado[1]*(D/2+1.2); sx2=W/n-0.2; sz2=0.9
                else
                    px=lado[1]*(W/2+1.2); pz=-D/2+t*D; sx2=0.9; sz2=D/n-0.2
                end
                mp("Pedra",m,Vector3.new(sx2,1.7,sz2),
                   r*CFrame.new(px,0.35+(k%2)*0.12,pz),SP,
                   Color3.fromRGB(104+(k%3)*10,100+(k%3)*9,94+(k%3)*8),0)
            end
        end
        -- Estacas de canto: a cabana fica assentada sobre toras, nao no chao
        for sx=-1,1,2 do for sz=-1,1,2 do
            mp("Estaca",m,Vector3.new(1.6,4.0,1.6),r*CFrame.new(sx*(W/2-0.9),-1.4,sz*(D/2-0.9)),SP,escura,0)
        end end

        -- ─── PAREDES ────────────────────────────────────────────────────
        local vaoX=-0.8
        local vaoH=7.4
        for sz=-1,1,2 do
            if sz==1 then
                mp("Wall",m,Vector3.new(W/2+vaoX-1.9,H,0.7),
                   r*CFrame.new((-W/2+vaoX-1.9)/2,H/2,D/2),SP,madeira,0)
                mp("Wall",m,Vector3.new(W/2-vaoX-1.9,H,0.7),
                   r*CFrame.new((vaoX+1.9+W/2)/2,H/2,D/2),SP,madeira,0)
                mp("Lintel",m,Vector3.new(3.8,H-vaoH,0.7),
                   r*CFrame.new(vaoX,vaoH+(H-vaoH)/2,D/2),SP,madeira,0)
            else
                mp("Wall",m,Vector3.new(W,H,0.7),r*CFrame.new(0,H/2,-D/2),SP,madeira,0)
            end
        end
        for sx=-1,1,2 do
            mp("Wall",m,Vector3.new(0.7,H,D),r*CFrame.new(sx*W/2,H/2,0),SP,madeira,0)
        end

        if tipo=="toras" then
            -- Tora horizontal empilhada, com as pontas cruzando nos cantos
            local n=math.floor(H/1.5)
            for b=0,n-1 do
                local by=0.9+b*1.5
                local c=shade(madeira,(b%2==0) and 8 or -8)
                for sz=-1,1,2 do
                    if not (sz==1 and by<vaoH) then
                        mp("Tora",m,Vector3.new(W+1.4,1.35,1.35),
                           r*CFrame.new(0,by,sz*(D/2+0.3))*CFrame.Angles(0,0,math.rad(90)),SP,c,0)
                    else
                        for sx=-1,1,2 do
                            local larg=(sx<0) and (W/2+vaoX-1.9) or (W/2-vaoX-1.9)
                            local cx=(sx<0) and (-W/2+larg/2) or (W/2-larg/2)
                            if larg>0.8 then
                                mp("Tora",m,Vector3.new(larg,1.35,1.35),
                                   r*CFrame.new(cx,by,D/2+0.3)*CFrame.Angles(0,0,math.rad(90)),SP,c,0)
                            end
                        end
                    end
                end
                for sx=-1,1,2 do
                    mp("Tora",m,Vector3.new(D+1.4,1.35,1.35),
                       r*CFrame.new(sx*(W/2+0.3),by,0)*CFrame.Angles(math.rad(90),0,math.rad(90)),SP,c,0)
                end
            end
        elseif tipo=="tabuas" then
            -- Tabua vertical; com abandono alto algumas somem, deixando fresta
            local n=math.floor(W/1.3)
            for b=0,n-1 do
                local bx=-W/2+0.65+b*1.3
                if hval(hs,60+b)>abandono*0.30 then
                    local c=shade(madeira,((b%3)*9)-9)
                    for sz=-1,1,2 do
                        if not (sz==1 and math.abs(bx-vaoX)<2.2) then
                            mp("Tabua",m,Vector3.new(1.15,H-0.4,0.4),
                               r*CFrame.new(bx,H/2,sz*(D/2+0.3)),SP,c,0)
                        end
                    end
                end
            end
            local nd=math.floor(D/1.3)
            for b=0,nd-1 do
                if hval(hs,90+b)>abandono*0.30 then
                    for sx=-1,1,2 do
                        mp("Tabua",m,Vector3.new(0.4,H-0.4,1.15),
                           r*CFrame.new(sx*(W/2+0.3),H/2,-D/2+0.65+b*1.3),SP,shade(madeira,((b%3)*9)-9),0)
                    end
                end
            end
            -- Travessas em X, marca de construcao tosca
            for sz=-1,1,2 do
                for dg=-1,1,2 do
                    mp("Trava",m,Vector3.new(math.sqrt(W*W+H*H)*0.9,0.5,0.3),
                       r*CFrame.new(0,H/2,sz*(D/2+0.55))*CFrame.Angles(0,0,math.rad(dg*math.deg(math.atan(H/W)))),
                       SP,escura,0)
                end
            end
        else -- pedra
            local rows=math.floor(H/1.2)
            for b=0,rows-1 do
                local by=0.8+b*1.2
                local off=(b%2==0) and 0 or 0.5
                for sz=-1,1,2 do
                    for cc=0,4 do
                        local bx=-W/2+(W/5)*(cc+0.5+off*0.4)
                        if bx<W/2-0.6 and not (sz==1 and by<vaoH and math.abs(bx-vaoX)<2.4) then
                            mp("Pedra",m,Vector3.new(W/5-0.2,1.1,0.55),
                               r*CFrame.new(bx,by,sz*(D/2+0.28)),SP,
                               Color3.fromRGB(108+((cc+b)%4)*8,104+((cc+b)%4)*7,96+((cc+b)%4)*7),0)
                        end
                    end
                end
                for sx=-1,1,2 do
                    for cc=0,3 do
                        mp("Pedra",m,Vector3.new(0.55,1.1,D/4-0.2),
                           r*CFrame.new(sx*(W/2+0.28),by,-D/2+(D/4)*(cc+0.5)),SP,
                           Color3.fromRGB(108+((cc+b)%4)*8,104+((cc+b)%4)*7,96+((cc+b)%4)*7),0)
                    end
                end
            end
        end

        -- Musgo nas quinas viradas ao norte: pouca coisa, mas tira o ar de novo
        for sx=-1,1,2 do
            for k=0,2 do
                if hval(hs,120+k+sx)<abandono*0.7 then
                    mp("Musgo",m,Vector3.new(1.3,2.2,0.35),
                       r*CFrame.new(sx*(W/2+0.5),1.4+k*2.4,-D/2+1.6),SP,shade(musgo,-(k*8)),0)
                end
            end
        end

        -- ─── PISO E INTERIOR ────────────────────────────────────────────
        mp("Floor",m,Vector3.new(W-1,0.6,D-1),r*CFrame.new(0,0.3,0),SP,escura,0)
        for fp=0,7 do
            mp("Plank",m,Vector3.new((W-1)/8-0.1,0.14,D-1),
               r*CFrame.new(-W/2+0.5+((W-1)/8)*(fp+0.5),0.67,0),SP,shade(escura,((fp%3)*7)-4),0)
        end
        mp("Ceiling",m,Vector3.new(W-1,0.4,D-1),r*CFrame.new(0,H-0.2,0),SP,shade(escura,-10),0)
        for vg=0,3 do
            mp("Viga",m,Vector3.new(W,0.6,0.6),r*CFrame.new(0,H-0.9,-D/2+(D/4)*(vg+0.5)),SP,escura,0)
        end
        -- Mobilia: mesa, banco, beliche, lareira, lampiao
        mp("Mesa",m,Vector3.new(4.4,0.3,2.6),r*CFrame.new(-W*0.16,2.5,D*0.10),SP,escura,0)
        for sx=-1,1,2 do for sz=-1,1,2 do
            mp("MesaPe",m,Vector3.new(0.35,2.3,0.35),r*CFrame.new(-W*0.16+sx*1.9,1.25,D*0.10+sz*1.0),SP,shade(escura,-12),0)
        end end
        mp("Banco",m,Vector3.new(4.2,0.3,1.1),r*CFrame.new(-W*0.16,1.5,D*0.10-2.2),SP,escura,0)
        mp("Cama",m,Vector3.new(4.6,1.1,7.0),r*CFrame.new(W*0.24,1.25,-D*0.10),SP,escura,0)
        mp("Colchao",m,Vector3.new(4.2,0.8,6.6),r*CFrame.new(W*0.24,2.2,-D*0.10),SP,shade(Color3.fromRGB(150,140,124),-abandono*40),0)
        -- Lareira de pedra com brasa
        mp("Lareira",m,Vector3.new(5.4,5.2,1.6),r*CFrame.new(W*0.05,2.6,-D/2+0.9),SP,Color3.fromRGB(104,100,94),0)
        mp("Boca",m,Vector3.new(3.2,2.6,1.9),r*CFrame.new(W*0.05,1.6,-D/2+1.1),SP,Color3.fromRGB(38,34,32),0)
        for lg=0,2 do
            mp("Lenha",m,Vector3.new(2.4,0.5,0.5),
               r*CFrame.new(W*0.05,1.0+lg*0.45,-D/2+1.1)*CFrame.Angles(0,math.rad(lg*22),0),SP,escura,0)
        end
        mp("Brasa",m,Vector3.new(2.6,0.4,1.1),r*CFrame.new(W*0.05,0.9,-D/2+1.1),Enum.Material.Neon,Color3.fromRGB(210,88,32),0.25)
        ml(m,(r*CFrame.new(W*0.05,1.6,-D/2+1.8)).Position,Color3.fromRGB(255,142,58),16,0.8)

        -- ─── JANELAS ────────────────────────────────────────────────────
        -- Vidro quebrado: transparencia alta e tabua pregada por cima.
        local function janelaCabana(cf,wid,hei,quebrada)
            mp("Batente",m,Vector3.new(wid+1.4,0.5,0.8),cf*CFrame.new(0,-hei/2-0.25,0.15),SP,escura,0)
            mp("Batente",m,Vector3.new(wid+1.4,0.5,0.8),cf*CFrame.new(0,hei/2+0.25,0.15),SP,escura,0)
            for sx=-1,1,2 do
                mp("Batente",m,Vector3.new(0.5,hei+1,0.8),cf*CFrame.new(sx*(wid/2+0.25),0,0.15),SP,escura,0)
            end
            mp("Vidro",m,Vector3.new(wid,hei,0.2),cf*CFrame.new(0,0,0.05),GL,
               quebrada and Color3.fromRGB(46,52,50) or gC, quebrada and 0.72 or 0.35)
            mp("Cruz",m,Vector3.new(0.18,hei,0.3),cf*CFrame.new(0,0,0.1),SP,escura,0)
            mp("Cruz",m,Vector3.new(wid,0.18,0.3),cf*CFrame.new(0,0,0.1),SP,escura,0)
            if quebrada then
                for tb=0,1 do
                    mp("Pregada",m,Vector3.new(wid+1.8,0.7,0.28),
                       cf*CFrame.new(0,-0.8+tb*1.9,0.32)*CFrame.Angles(0,0,math.rad((tb==0) and 9 or -12)),
                       SP,shade(escura,10),0)
                end
            end
        end

        -- ─── TELHADO ────────────────────────────────────────────────────
        -- Tabua corrida em vez de telha: mais barato e mais rustico. Com
        -- abandono alto abrem-se buracos.
        local larg=W+ov*2
        local run=D/2+ov
        -- Caimento forte: cabana de mata tem telhado ingreme, e a silhueta
        -- alta e o que a torna reconhecivel de longe entre as arvores.
        local rise=(telhado=="uma") and (D*0.38) or (run*0.92)
        local function aguaTabua(cx,ridgeZ,lg,rn,rs,y0,dz,rows,cols)
            local pitch=math.atan(rs/rn)
            local slope=math.sqrt(rs*rs+rn*rn)
            mp("Deck",m,Vector3.new(lg,0.45,slope+0.5),
               r*CFrame.new(cx,y0+rs/2,ridgeZ+dz*rn/2)*CFrame.Angles(dz*pitch,0,0),SP,escura,0)
            local tlen=slope/rows*1.5
            for row=0,rows-1 do
                local t=(row+0.5)/rows
                local ty=y0+t*rs
                local tz=ridgeZ+dz*rn*(1-t)
                local step=lg/cols
                for col=0,cols-1 do
                    if hval(hs,200+row*7+col)>abandono*0.22 then
                        mp("Tabua",m,Vector3.new(step-0.1,0.3,tlen),
                           r*CFrame.new(cx-lg/2+step/2+col*step,ty+0.35,tz)*CFrame.Angles(dz*pitch,0,0),
                           SP,shade(telha,((col+row)%3)*9-9),0)
                    end
                end
            end
        end
        if telhado=="duas" then
            for dz=-1,1,2 do aguaTabua(0,0,larg,run,rise,H,dz,6,7) end
            for sx=-1,1,2 do
                empena(m,r,sx*(W/2+ov-0.35),0,sx,(D+ov*2)/2,H,rise,madeira,escura,"duas",0,0)
            end
            mp("Cumeeira",m,Vector3.new(larg+0.6,0.8,1.4),r*CFrame.new(0,H+rise+0.2,0),SP,escura,0)
            -- Caibros aparentes sob o beiral: da espessura ao telhado, que
            -- de perto parecia uma placa fina apoiada na parede.
            for dz=-1,1,2 do
                for cb=0,6 do
                    mp("Caibro",m,Vector3.new(0.45,0.55,1.9),
                       r*CFrame.new(-larg/2+1.2+cb*((larg-2.4)/6),H-0.25,dz*(run-0.4)),SP,escura,0)
                end
            end
            -- Janela do sotao na empena da frente: de fora, e uma luz alta
            -- que nao se sabe de onde vem.
            janelaCabana(r*CFrame.new(0,H+rise*0.42,D/2+ov-0.5),2.6,2.2,hval(hs,146)<abandono)
            for sx=-1,1,2 do
                mp("Contravento",m,Vector3.new(0.45,rise*0.9,0.45),
                   r*CFrame.new(sx*(W*0.28),H+rise*0.45,D/2+ov-0.15)*CFrame.Angles(0,0,math.rad(sx*16)),SP,escura,0)
            end
        else
            aguaTabua(0,-D/2-ov,larg,D+ov*2,rise,H,1,8,7)
            mp("Fundo",m,Vector3.new(larg,rise,0.7),r*CFrame.new(0,H+rise/2,-D/2-ov),SP,madeira,0)
            for sx=-1,1,2 do
                for g=0,8 do
                    local t=(g+0.5)/9
                    local hw=(D+ov*2)*(1-t)/2
                    mp("Empena",m,Vector3.new(0.6,rise/9+0.16,hw*2),
                       r*CFrame.new(sx*(W/2+ov-0.3),H+t*rise,-D/2-ov+hw),SP,shade(madeira,-8),0)
                end
            end
        end

        -- Chamine de pedra encostada na lateral da lareira
        mp("Chamine",m,Vector3.new(3.2,H+rise+5,2.8),r*CFrame.new(W*0.05,(H+rise+5)/2-1,-D/2-0.6),SP,Color3.fromRGB(100,96,90),0)
        mp("ChamCap",m,Vector3.new(3.9,0.5,3.5),r*CFrame.new(W*0.05,H+rise+4.2,-D/2-0.6),SP,Color3.fromRGB(70,66,62),0)

        for sx=-1,1,2 do
            janelaCabana(r*CFrame.new(sx*(W/2+0.45),H*0.55,D*0.12)*CFrame.Angles(0,math.rad(sx*90),0),
                         3.4,2.8,hval(hs,140+sx)<abandono)
        end
        janelaCabana(r*CFrame.new(W*0.26,H*0.55,D/2+0.45),3.4,2.8,hval(hs,143)<abandono)

        -- ─── PORTA ──────────────────────────────────────────────────────
        local aberta=hval(hs,150)<abandono*0.6
        mp("Marco",m,Vector3.new(4.6,6.9,0.9),r*CFrame.new(vaoX,3.45,D/2+0.4),SP,escura,0)
        if aberta then
            -- Porta entreaberta: escuro la dentro e o que assusta
            mp("Porta",m,Vector3.new(3.4,6.2,0.3),
               r*CFrame.new(vaoX-1.4,3.1,D/2+0.9)*CFrame.Angles(0,math.rad(58),0),SP,shade(escura,8),0)
        else
            mp("Porta",m,Vector3.new(3.4,6.2,0.3),r*CFrame.new(vaoX,3.1,D/2+0.65),SP,shade(escura,8),0)
            for pn=0,2 do
                mp("Ripa",m,Vector3.new(3.4,0.35,0.4),r*CFrame.new(vaoX,1.4+pn*2.1,D/2+0.8),SP,escura,0)
            end
        end
        mp("Macaneta",m,Vector3.new(0.3,0.3,0.4),r*CFrame.new(vaoX+1.3,3.1,D/2+0.9),Enum.Material.Metal,Color3.fromRGB(92,84,70),0)
        -- Lampiao ao lado da porta: a unica luz do lado de fora
        mp("Suporte",m,Vector3.new(0.25,0.25,1.4),r*CFrame.new(vaoX+2.9,6.0,D/2+1.0),Enum.Material.Metal,Color3.fromRGB(58,54,50),0)
        mp("Lampiao",m,Vector3.new(1.0,1.5,1.0),r*CFrame.new(vaoX+2.9,5.4,D/2+1.6),GL,Color3.fromRGB(255,196,110),0.3)
        ml(m,(r*CFrame.new(vaoX+2.9,5.4,D/2+1.6)).Position,Color3.fromRGB(255,178,90),22,1.0,true)

        -- ─── ANEXO ──────────────────────────────────────────────────────
        local acessoZ=D/2
        if anexo=="alpendre" then
            local pD=5.6
            mp("DeckBase",m,Vector3.new(W+1.6,30,pD),r*CFrame.new(0,-15.0,D/2+pD/2),SP,Color3.fromRGB(96,92,86),0)
            mp("Deck",m,Vector3.new(W+1.6,0.5,pD),r*CFrame.new(0,0.45,D/2+pD/2),SP,escura,0)
            for pl=0,8 do
                if hval(hs,170+pl)>abandono*0.2 then
                    mp("Plank",m,Vector3.new((W+1.6)/9-0.1,0.16,pD),
                       r*CFrame.new(-W/2-0.8+((W+1.6)/9)*(pl+0.5),0.72,D/2+pD/2),SP,shade(escura,(pl%3)*8-4),0)
                end
            end
            mp("Cobertura",m,Vector3.new(W+2.2,0.45,pD+0.8),r*CFrame.new(0,6.6,D/2+pD/2),SP,telha,0)
            for sx=-1,1,2 do
                mp("Poste",m,Vector3.new(0.8,6.2,0.8),r*CFrame.new(sx*(W/2-0.2),3.4,D/2+pD-0.5),SP,escura,0)
            end
            -- Guarda-corpo partido, com vao para a escada
            for sx=-1,1,2 do
                local trecho=(W+1.6)/2-3.5
                if trecho>1 then
                    mp("Corrimao",m,Vector3.new(trecho,0.35,0.45),
                       r*CFrame.new(sx*(trecho/2+3.5)+vaoX,2.4,D/2+pD-0.3),SP,escura,0)
                end
            end
            for bl=0,9 do
                local bx=-W/2+bl*(W/9)
                if math.abs(bx-vaoX)>3.5 and hval(hs,180+bl)>abandono*0.28 then
                    mp("Baluster",m,Vector3.new(0.3,1.7,0.3),r*CFrame.new(bx,1.5,D/2+pD-0.3),SP,escura,0)
                end
            end
            -- Cadeira de balanco: silhueta que le como "alguem morava aqui"
            mp("Balanco",m,Vector3.new(1.8,0.25,1.8),r*CFrame.new(W*0.26,1.5,D/2+pD*0.5),SP,shade(escura,12),0)
            mp("BalancoEncosto",m,Vector3.new(1.8,2.0,0.25),
               r*CFrame.new(W*0.26,2.4,D/2+pD*0.5-0.8)*CFrame.Angles(math.rad(-12),0,0),SP,shade(escura,12),0)
            for sx=-1,1,2 do
                mp("Patim",m,Vector3.new(0.25,0.25,2.6),r*CFrame.new(W*0.26+sx*0.7,1.0,D/2+pD*0.5),SP,shade(escura,-8),0)
            end
            acessoZ=D/2+pD+0.4
        elseif anexo=="lenha" then
            local lx=W/2+2.6
            mp("Abrigo",m,Vector3.new(5.2,0.4,7.4),r*CFrame.new(lx,5.0,0),SP,telha,0)
            -- Quatro mourões, nao dois: com apoio so de um lado o telhadinho
            -- ficava em balanco e lia como caixa flutuando.
            for sz=-1,1,2 do for sx2=-1,1,2 do
                mp("Mourao",m,Vector3.new(0.6,5.0,0.6),r*CFrame.new(lx+sx2*2.2,2.5,sz*3.4),SP,escura,0)
            end end
            for fila=0,3 do
                for col=0,5 do
                    -- Falha so na fila de cima: tirar tora de fila do meio
                    -- deixava as de cima penduradas no ar.
                    if fila<3 or hval(hs,220+fila*6+col)>0.35 then
                        mp("Tora",m,Vector3.new(1.0,1.0,4.6),
                           r*CFrame.new(lx-0.6+col*1.05,0.9+fila*1.05,0)*CFrame.Angles(0,0,0),SP,
                           shade(madeira,((fila+col)%3)*10-10),0)
                    end
                end
            end
            mp("Machado",m,Vector3.new(0.25,0.25,3.0),r*CFrame.new(lx-2.6,1.8,3.6)*CFrame.Angles(math.rad(64),0,0),SP,escura,0)
            mp("Lamina",m,Vector3.new(0.4,1.5,1.0),r*CFrame.new(lx-2.6,3.0,4.6),Enum.Material.Metal,Color3.fromRGB(126,126,122),0)
            mp("Cepo",m,Vector3.new(3.0,2.4,3.0),r*CFrame.new(lx-2.6,1.2,4.2),SP,shade(madeira,-14),0)
        end

        -- Sinais de que alguem morou aqui: e o que separa "caixa de madeira"
        -- de "cabana". Ficam sempre do lado da porta, onde o jogador chega.
        do
            local fz=D/2+2.6
            -- Barril de agua sob a calha
            mp("Barril",m,Vector3.new(3.0,3.4,3.0),r*CFrame.new(W/2-2.5,1.7,fz),SP,shade(madeira,-12),0)
            for arc=0,1 do
                mp("Arco",m,Vector3.new(3.2,0.35,3.2),r*CFrame.new(W/2-2.5,0.9+arc*1.7,fz),Enum.Material.Metal,Color3.fromRGB(76,70,64),0)
            end
            mp("Agua",m,Vector3.new(2.5,0.2,2.5),r*CFrame.new(W/2-2.5,3.3,fz),GL,Color3.fromRGB(52,66,64),0.35)
            -- Lampiao velho pendurado num gancho
            mp("Gancho",m,Vector3.new(0.2,1.2,0.2),r*CFrame.new(-W/2+1.6,5.6,D/2+0.9),Enum.Material.Metal,Color3.fromRGB(70,66,60),0)
            mp("LampiaoVelho",m,Vector3.new(0.9,1.3,0.9),r*CFrame.new(-W/2+1.6,4.6,D/2+0.9),GL,Color3.fromRGB(120,116,104),0.45)
            -- Botas na soleira e um degrau de pedra
            mp("Soleira",m,Vector3.new(5.0,0.5,2.2),r*CFrame.new(vaoX,0.5,D/2+1.4),SP,Color3.fromRGB(112,108,102),0)
            for bt=-1,1,2 do
                mp("Bota",m,Vector3.new(1.0,1.1,2.0),r*CFrame.new(vaoX+bt*0.9,1.2,D/2+1.5),SP,shade(escura,-6),0)
            end
        end

        -- Escada curta ate o solo (a cabana e baixa, poucos degraus)
        local pe=r*CFrame.new(vaoX,0.5,acessoZ)
        escadaAoSolo(m,pe.Position,-pe.LookVector,4.5,escura,escura,10)

        -- ─── ELEMENTO DE PATIO ──────────────────────────────────────────
        if extra=="poco" then
            local px,pz=-W*0.55-4,D*0.30
            for k=0,11 do
                local a=math.rad(k*30)
                mp("Poco",m,Vector3.new(1.3,2.4,1.0),
                   r*CFrame.new(px+math.cos(a)*3.0,1.2,pz+math.sin(a)*3.0)*CFrame.Angles(0,-a,0),SP,
                   Color3.fromRGB(102+(k%3)*8,98+(k%3)*7,92+(k%3)*7),0)
            end
            mp("PocoFundo",m,Vector3.new(5.2,0.4,5.2),r*CFrame.new(px,0.2,pz),SP,Color3.fromRGB(26,26,28),0)
            for sx=-1,1,2 do
                mp("Forquilha",m,Vector3.new(0.5,5.0,0.5),r*CFrame.new(px+sx*3.0,2.5,pz),SP,escura,0)
            end
            mp("Travessa",m,Vector3.new(7.0,0.5,0.5),r*CFrame.new(px,5.0,pz),SP,escura,0)
            mp("Corda",m,Vector3.new(0.16,3.2,0.16),r*CFrame.new(px,3.4,pz),SP,Color3.fromRGB(148,136,112),0)
            mp("Balde",m,Vector3.new(1.4,1.4,1.4),r*CFrame.new(px,1.9,pz),SP,escura,0)
        elseif extra=="varal" then
            local px,pz=-W*0.55-5,-D*0.20
            for sz=-1,1,2 do
                mp("Estaca",m,Vector3.new(0.4,6.0,0.4),r*CFrame.new(px,3.0,pz+sz*5.0),SP,escura,0)
                mp("Braco",m,Vector3.new(2.4,0.35,0.35),r*CFrame.new(px,5.6,pz+sz*5.0),SP,escura,0)
            end
            mp("Fio",m,Vector3.new(0.1,0.1,10),r*CFrame.new(px,5.6,pz),SP,Color3.fromRGB(150,144,130),0)
            for pn=0,3 do
                mp("Pano",m,Vector3.new(0.15,2.6,1.8),
                   r*CFrame.new(px,4.3,pz-3.4+pn*2.3)*CFrame.Angles(0,0,math.rad((pn%2==0) and 5 or -6)),SP,
                   shade(Color3.fromRGB(178,172,160),-abandono*45),0)
            end
        elseif extra=="tocos" then
            for k=0,4 do
                local a=hval(hs,240+k)*math.pi*2
                local d=8+hval(hs,250+k)*7
                mp("Toco",m,Vector3.new(2.6,1.8,2.6),
                   r*CFrame.new(math.cos(a)*d,0.7,math.sin(a)*d-D*0.2),SP,shade(madeira,-16),0)
                mp("TocoTopo",m,Vector3.new(2.7,0.25,2.7),
                   r*CFrame.new(math.cos(a)*d,1.65,math.sin(a)*d-D*0.2),SP,shade(madeira,12),0)
            end
        end

        -- Cerca de estacas tortas na frente, sempre com falhas
        local lz=D/2+13
        local nseg=10
        for sg=0,nseg-1 do
            local ex=-W/2-2+sg*((W+4)/nseg)
            if math.abs(ex-vaoX)>3.4 and hval(hs,260+sg)>abandono*0.45 then
                local w2=r*CFrame.new(ex,0,lz)
                local sr=surfaceAt(w2.Position.X,w2.Position.Z)
                local sy=sr and (sr.Position.Y-bp.Y) or 0
                mp("Estaca",m,Vector3.new(0.5,5.0,0.5),
                   r*CFrame.new(ex,sy+1.2,lz)*CFrame.Angles(math.rad((hval(hs,270+sg)-0.5)*14),0,math.rad((hval(hs,280+sg)-0.5)*16)),
                   SP,escura,0)
            end
        end
        task.wait()
    end

    -- ═══════════════════════════════════════════════════════════════════
    -- PLANEJADOR DE FLORESTA
    -- ═══════════════════════════════════════════════════════════════════
    -- Nada de avenida, quadra ou lote: aqui o que organiza o mapa e a
    -- CLAREIRA. Cabanas ficam longe umas das outras, cada uma no meio de um
    -- vazio aberto na mata, ligadas por trilhas estreitas e tortas — ou por
    -- nenhuma trilha, se forem poucas. Com CABIN_COUNT = 0 sai floresta pura.

    local function planejarFloresta()
        print(string.format("[Map Architect] Floresta: %d cabana(s) pedida(s).",CABIN_COUNT))

        -- Ambientacao. E o que transforma "mapa com arvores" em "mapa de
        -- terror" — mais do que qualquer peca que eu coloque.
        pcall(function()
            local lighting=game:GetService("Lighting")
            lighting.ClockTime=HORROR_MOOD and 3.2 or 8.5
            lighting.Brightness=HORROR_MOOD and 0.7 or 1.6
            lighting.Ambient=HORROR_MOOD and Color3.fromRGB(18,20,26) or Color3.fromRGB(70,74,80)
            lighting.OutdoorAmbient=HORROR_MOOD and Color3.fromRGB(28,32,42) or Color3.fromRGB(96,100,106)
            lighting.FogEnd=HORROR_MOOD and 220 or 620
            lighting.FogStart=HORROR_MOOD and 30 or 120
            lighting.FogColor=HORROR_MOOD and Color3.fromRGB(28,32,34) or Color3.fromRGB(150,156,150)
            lighting.GlobalShadows=true
            local atm=lighting:FindFirstChildOfClass("Atmosphere")
            if not atm then atm=Instance.new("Atmosphere"); atm.Parent=lighting end
            atm.Density=HORROR_MOOD and 0.62 or 0.32
            atm.Haze=HORROR_MOOD and 3.4 or 1.4
            atm.Color=HORROR_MOOD and Color3.fromRGB(78,84,86) or Color3.fromRGB(190,195,190)
            atm.Decay=HORROR_MOOD and Color3.fromRGB(48,54,58) or Color3.fromRGB(140,148,150)
            if HORROR_MOOD then
                local sky=lighting:FindFirstChildOfClass("Sky")
                if not sky then sky=Instance.new("Sky"); sky.Parent=lighting end
                sky.MoonAngularSize=18
                sky.StarCount=1400
            end
            print(string.format("[Map Architect] Ambientacao %s aplicada (ClockTime %.1f, neblina em %d studs).",
                  HORROR_MOOD and "de terror" or "diurna", lighting.ClockTime, lighting.FogEnd))
        end)

        -- A grama do Terrain e volumetrica e cresce ATRAVES das pecas: na
        -- clareira ela subia por dentro do alpendre e escondia a cabana.
        -- Numa floresta quem faz o volume e a arvore, nao a grama.
        pcall(function()
            if Terrain.Decoration then
                Terrain.Decoration=false
                print("[Map Architect] Grama decorativa desligada (cobria as cabanas).")
                print("[Map Architect] Para reativar: workspace.Terrain.Decoration = true")
            end
        end)

        -- A mata e criada na PARTE 1 (setup_world / setup_world_nocity).
        -- Rodar so o setup_city devolve cabanas num campo pelado, e o motivo
        -- nao e obvio olhando o mapa. Melhor dizer do que deixar adivinhar.
        local nArv=0
        for _,_ in treesFolder:GetChildren() do nArv+=1 end
        if nArv==0 then
            warn("[Map Architect] NENHUMA ARVORE no mapa. Se voce colou so o setup_city,")
            warn("[Map Architect] rode antes o setup_world_nocity — e ele que planta a mata.")
        else
            print(string.format("[Map Architect] Mata existente: %d arvores.",nArv))
        end

        if CABIN_COUNT<=0 then
            print("[Map Architect] Zero cabanas: floresta intacta.")
            return
        end

        -- ─── CLAREIRAS ──────────────────────────────────────────────────
        -- Varre o mapa procurando pontos planos, e escolhe os que estao mais
        -- distantes entre si. Cabana isolada assusta; cabana em fila, nao.
        local cands={}
        local varridos=0
        for bx=minX+90,maxX-90,40 do
            for bz=minZ+90,maxZ-90,40 do
                local y,dr=pegada(bx,bz,26,12)
                -- Sob a mata fechada a agua nao aparece; abrir a clareira em
                -- cima de uma depressao alagada revelava um poco d'agua no
                -- meio do terreiro. Exige terra seca em volta.
                if y and not nearWater(bx,bz,40) then
                    table.insert(cands,{x=bx,y=y,z=bz,dr=dr})
                end
                varridos+=1
                if varridos%200==0 then task.wait() end
            end
        end
        if #cands==0 then
            warn("[Map Architect] Nenhuma clareira viavel — nenhuma cabana colocada.")
            return
        end
        print(string.format("[Map Architect] %d pontos planos candidatos a clareira.",#cands))

        -- Primeira cabana: a mais plana. Demais: a mais LONGE das ja postas,
        -- que espalha melhor do que sortear.
        local escolhidas={}
        table.sort(cands,function(a,b) return a.dr<b.dr end)
        table.insert(escolhidas,cands[1])
        while #escolhidas<CABIN_COUNT do
            local melhor,md=nil,-1
            for _,c in cands do
                local dmin=math.huge
                for _,e in escolhidas do
                    local d=(Vector2.new(c.x,c.z)-Vector2.new(e.x,e.z)).Magnitude
                    if d<dmin then dmin=d end
                end
                -- desnivel entra como desempate, nao como veto
                local nota=dmin-c.dr*3
                if dmin>70 and nota>md then md=nota; melhor=c end
            end
            if not melhor then break end
            table.insert(escolhidas,melhor)
            task.wait()
        end
        if #escolhidas<CABIN_COUNT then
            print(string.format("[Map Architect] So couberam %d cabanas com afastamento minimo de 70 studs.",#escolhidas))
        end

        -- ─── ABRIR A CLAREIRA E CONSTRUIR ───────────────────────────────
        for i,c in escolhidas do
            -- Raio de clareira sorteado: uma cabana espremida entre arvores e
            -- outra num vazio amplo dao sensacoes diferentes.
            local raio=26+bhash(c.x,c.z,700+i)*16
            limpar(c.x,c.z,raio)
            -- Abandono cresce com a distancia ao spawn: a primeira cabana e a
            -- mais inteira, as do fundo da mata sao as mais degradadas.
            local aband=math.clamp(0.25+(i-1)*(0.7/math.max(#escolhidas-1,1)),0.2,0.95)
            local hs=math.floor(c.x*7.13+c.z*3.77+BUILDING_SEED)%100000
            local yaw=bhash(c.x,c.z,701+i)*360
            mkCabin(Vector3.new(c.x,c.y,c.z),yaw,hs,aband)
            buildingCount+=1
            -- Toco e galho caido na borda da clareira: marca de que a mata foi
            -- derrubada ali, em vez de a cabana ter nascido num buraco redondo.
            for k=0,5 do
                local a=bhash(c.x,c.z,710+i*7+k)*math.pi*2
                local d=raio*0.62+bhash(c.x,c.z,720+i*7+k)*raio*0.3
                local px,pz=c.x+math.cos(a)*d,c.z+math.sin(a)*d
                local sr=surfaceAt(px,pz)
                if sr and sr.Position.Y>WATER_Y+1 then
                    if k%2==0 then
                        mp("Toco",infraFolder,Vector3.new(2.4,1.6,2.4),
                           CFrame.new(px,sr.Position.Y+0.5,pz),SP,Color3.fromRGB(84,64,44),0)
                    else
                        mp("Galho",infraFolder,Vector3.new(1.3,1.3,7.0),
                           CFrame.new(px,sr.Position.Y+0.6,pz)*CFrame.Angles(0,a,math.rad(6)),
                           SP,Color3.fromRGB(74,58,42),0)
                    end
                end
            end
            print(string.format("[Map Architect] Cabana %d em (%.0f, %.0f, %.0f), clareira de %.0f studs, abandono %.0f%%.",
                  i,c.x,c.y,c.z,raio,aband*100))
            task.wait()
        end

        -- ─── TRILHAS ────────────────────────────────────────────────────
        -- Trilha de terra batida, estreita e sem meio-fio. Liga cada cabana a
        -- anterior. Nao usa mkRoad: rua asfaltada com calcada na floresta
        -- destruiria o clima.
        if #escolhidas>1 and TRAILS then
            local function mkTrail(a,b)
                local dx,dz=b.x-a.x,b.z-a.z
                local comp=math.sqrt(dx*dx+dz*dz)
                local passos=math.ceil(comp/7)
                local dir=Vector3.new(dx,0,dz).Unit
                local nor=Vector3.new(-dir.Z,0,dir.X)
                local prev=nil
                for k=0,passos do
                    local t=k/passos
                    -- Serpenteia: trilha reta parece estrada, nao trilha
                    local desvio=math.sin(t*math.pi*2.6+bhash(a.x,a.z,760)*6)*9*math.sin(t*math.pi)
                    local px=a.x+dx*t+nor.X*desvio
                    local pz=a.z+dz*t+nor.Z*desvio
                    local sr=surfaceAt(px,pz)
                    if sr and sr.Position.Y>WATER_Y+0.5 then
                        local cur=Vector3.new(px,sr.Position.Y,pz)
                        if prev then
                            local seg=cur-prev
                            local sl=seg.Magnitude
                            if sl>1 and sl<22 then
                                local mid=prev:Lerp(cur,0.5)
                                mp("Trilha",infraFolder,Vector3.new(5.5+bhash(px,pz,770)*2,1.6,sl+1.0),
                                   CFrame.lookAt(mid,mid+seg)*CFrame.new(0,-0.62,0),SP,
                                   Color3.fromRGB(96+math.floor(bhash(px,pz,771)*12),
                                                  80+math.floor(bhash(px,pz,772)*10),60),0)
                                limpar(px,pz,7)
                            end
                        end
                        prev=cur
                    else
                        prev=nil
                    end
                end
            end
            for i=2,#escolhidas do
                mkTrail(escolhidas[i-1],escolhidas[i])
                task.wait()
            end
            print(string.format("[Map Architect] %d trilha(s) de terra ligando as cabanas.",#escolhidas-1))
        end

        print(string.format("[Map Architect] Floresta pronta: %d cabanas.",#escolhidas))
    end

"""

LUA_URBAN_PLAN = r"""
    -- Preset de floresta nao usa nada do urbanismo: sai por aqui.
    if BUILDING_PRESET=="forest_horror" then
        planejarFloresta()
        local totalPecas=0
        for _,pasta in {buildingsFolder,infraFolder} do
            for _,d in pasta:GetDescendants() do
                if d:IsA("BasePart") then totalPecas+=1 end
            end
        end
        print(string.format("[Map Architect] Construcoes: %d cabanas, %d pecas.",buildingCount,totalPecas))
        print(string.format("[Map Architect] Custo de jogo: %d luzes dinamicas (teto %d), %d pecas sem sombra e sem consulta.",
              luzes,LUZ_ORCAMENTO,decorativas))
        return
    end

    -- ═══════════════════════════════════════════════════════════════════
    -- MOBILIARIO URBANO
    -- ═══════════════════════════════════════════════════════════════════
    local function poste(pos,yaw,lado)
        local f=CFrame.new(pos)*CFrame.Angles(0,math.rad(yaw),0)
        mp("PoleBase",infraFolder,Vector3.new(1.6,0.8,1.6),f*CFrame.new(0,0.4,0),SP,Color3.fromRGB(96,94,90),0)
        mp("Pole",infraFolder,Vector3.new(0.55,15,0.55),f*CFrame.new(0,7.5,0),Enum.Material.Metal,Color3.fromRGB(72,70,68),0)
        mp("Arm",infraFolder,Vector3.new(0.4,0.4,5.0),f*CFrame.new(0,14.8,lado*2.5),Enum.Material.Metal,Color3.fromRGB(72,70,68),0)
        mp("Head",infraFolder,Vector3.new(1.6,0.7,3.0),f*CFrame.new(0,14.4,lado*4.6),SP,Color3.fromRGB(58,56,54),0)
        ml(infraFolder,(f*CFrame.new(0,13.9,lado*4.6)).Position,Color3.fromRGB(255,226,168),26,1.1,true)
    end
    local function banco(pos,yaw)
        local f=CFrame.new(pos)*CFrame.Angles(0,math.rad(yaw),0)
        for sx=-1,1,2 do
            mp("BLeg",infraFolder,Vector3.new(0.4,1.6,1.8),f*CFrame.new(sx*2.4,0.8,0),Enum.Material.Metal,Color3.fromRGB(66,64,62),0)
        end
        for sl=0,2 do
            mp("BSeat",infraFolder,Vector3.new(6.0,0.3,0.55),f*CFrame.new(0,1.7,-0.6+sl*0.6),SP,Color3.fromRGB(146,106,68),0)
        end
        for sl=0,2 do
            mp("BBack",infraFolder,Vector3.new(6.0,0.5,0.28),f*CFrame.new(0,2.2+sl*0.6,0.8),SP,Color3.fromRGB(146,106,68),0)
        end
    end
    local function lixeira(pos)
        mp("BinBody",infraFolder,Vector3.new(1.8,3.0,1.8),CFrame.new(pos+Vector3.new(0,1.5,0)),SP,Color3.fromRGB(62,84,66),0)
        mp("BinLid",infraFolder,Vector3.new(2.2,0.4,2.2),CFrame.new(pos+Vector3.new(0,3.2,0)),SP,Color3.fromRGB(48,66,52),0)
    end
    local function floreira(pos)
        mp("PlBox",infraFolder,Vector3.new(4.2,1.6,4.2),CFrame.new(pos+Vector3.new(0,0.8,0)),SP,Color3.fromRGB(206,200,190),0)
        mp("PlSoil",infraFolder,Vector3.new(3.6,0.4,3.6),CFrame.new(pos+Vector3.new(0,1.7,0)),SP,Color3.fromRGB(92,72,52),0)
        for fl=0,3 do
            local a=math.rad(fl*90+35)
            mp("PlBush",infraFolder,Vector3.new(1.5,1.5,1.5),CFrame.new(pos+Vector3.new(math.cos(a)*1.0,2.4,math.sin(a)*1.0)),SP,
               Color3.fromRGB(70+fl*16,116+fl*8,54),0)
        end
    end
    local function placaRua(pos,yaw)
        local f=CFrame.new(pos)*CFrame.Angles(0,math.rad(yaw),0)
        mp("SgPole",infraFolder,Vector3.new(0.35,9,0.35),f*CFrame.new(0,4.5,0),Enum.Material.Metal,Color3.fromRGB(70,68,66),0)
        mp("SgPlate",infraFolder,Vector3.new(7.0,1.6,0.2),f*CFrame.new(2.6,8.4,0),SP,Color3.fromRGB(44,86,142),0)
        mp("SgPlate2",infraFolder,Vector3.new(0.2,1.6,7.0),f*CFrame.new(0,7.0,2.6),SP,Color3.fromRGB(44,86,142),0)
    end
    -- Faixa de pedestre: barras transversais ao eixo da via
    local function faixaPedestre(pos,dir,larg)
        local d=Vector3.new(dir.X,0,dir.Z)
        if d.Magnitude<0.01 then return end
        d=d.Unit
        local right=Vector3.new(-d.Z,0,d.X)
        for b=-3,3 do
            local c=pos+right*(b*1.5)
            mp("Zebra",infraFolder,Vector3.new(0.95,0.55,larg),CFrame.lookAt(c,c+right),SP,Color3.fromRGB(238,234,224),0)
        end
    end
    local function pontoOnibus(pos,yaw)
        local f=CFrame.new(pos)*CFrame.Angles(0,math.rad(yaw),0)
        mp("StopFloor",infraFolder,Vector3.new(11,1.8,5),f*CFrame.new(0,-0.5,0),SP,Color3.fromRGB(190,186,178),0)
        for sx=-1,1,2 do
            mp("StopPost",infraFolder,Vector3.new(0.4,7.5,0.4),f*CFrame.new(sx*5.2,3.75,-2.1),Enum.Material.Metal,Color3.fromRGB(66,64,62),0)
            mp("StopPost",infraFolder,Vector3.new(0.4,7.5,0.4),f*CFrame.new(sx*5.2,3.75,2.1),Enum.Material.Metal,Color3.fromRGB(66,64,62),0)
        end
        mp("StopRoof",infraFolder,Vector3.new(12,0.4,6),f*CFrame.new(0,7.7,0),SP,Color3.fromRGB(58,72,84),0)
        mp("StopGlass",infraFolder,Vector3.new(11,6.6,0.25),f*CFrame.new(0,4.2,-2.4),GL,gC,0.42)
        mp("StopBench",infraFolder,Vector3.new(9,0.35,1.6),f*CFrame.new(0,1.9,-1.4),SP,Color3.fromRGB(146,106,68),0)
        for sx=-1,1,2 do
            mp("StopLeg",infraFolder,Vector3.new(0.35,1.6,1.6),f*CFrame.new(sx*3.8,1.0,-1.4),Enum.Material.Metal,Color3.fromRGB(66,64,62),0)
        end
        ml(infraFolder,(f*CFrame.new(0,7.2,0)).Position,Color3.fromRGB(226,236,255),18,0.7)
    end

    -- ═══════════════════════════════════════════════════════════════════
    -- PLANEJAMENTO URBANO — ruas primeiro, lotes depois
    -- ═══════════════════════════════════════════════════════════════════
    -- Ate a v0.8.7 as construcoes eram largadas em pontos soltos e so
    -- depois ligadas por estradas. Agora e o inverso, que e como um bairro
    -- de verdade se organiza:
    --   1. acha a zona urbana        4. divide a testada em lotes
    --   2. traca a avenida            5. uma casa por lote, FACHADA
    --   3. traca as transversais         voltada para a rua
    --
    -- ATENCAO: nada aqui toca no terreno. Nenhum FillBlock, nenhum
    -- flattenArea. Foi isso que travou o Studio na v0.7.0, nao o urbanismo
    -- em si. Terreno plano se resolve no heightmap (apply_coastal_shelf).


    -- A grama do Terrain e decoracao volumetrica: ela cresce ATRAVES de
    -- qualquer peca apoiada no chao, entao a calcada some no meio do mato.
    -- Nao da para pintar so a faixa da rua sem mexer em voxel (proibido,
    -- erro 5.2), entao a decoracao e desligada no mapa inteiro quando ha
    -- cidade. Para trazer de volta: workspace.Terrain.Decoration = true
    pcall(function()
        if Terrain.Decoration then
            Terrain.Decoration = false
            print("[Map Architect] Grama decorativa do Terrain desligada (cobria calcadas e ruas).")
            print("[Map Architect] Para reativar: workspace.Terrain.Decoration = true")
        end
    end)

    -- Orcamento de pecas do mapa inteiro. Fica aqui no topo porque nao serve
    -- so para parar de construir casa: a MALHA tambem se dimensiona por ele.
    -- Sem isso a metropole tracava 15 avenidas e 504 testadas, e o orcamento
    -- acabava com 87 casas — 417 lotes vazios, cidade fantasma. Rua tambem
    -- custa peca, entao malha grande demais gasta duas vezes.
    local ORCAMENTO=(BUILDING_PRESET=="metropole") and 62000 or 46000
    -- Custo MEDIDO por lote, contando a casa e o trecho de rua que a serve.
    -- Contar so a casa subestimava pela metade.
    local CUSTO_LOTE=(BUILDING_PRESET=="metropole") and 330 or 520
    local LOTES_ALVO=math.floor(ORCAMENTO/CUSTO_LOTE)

    local ROAD_W   = math.clamp(ROAD_WIDTH,8,22)
    local AVE_W    = ROAD_W+4
    -- A casa tem de 24 a 30 studs de fachada. Com LOT_W 34 (o valor antigo
    -- de "compacto") a casa saia MAIS LARGA que o lote: cerca viva por dentro
    -- da parede e vizinhas a 22 studs de distancia, encostando uma na outra.
    local LOT_W    = (LOT_SIZE=="compact" and 38) or (LOT_SIZE=="large" and 56) or 46
    local RECUO    = (LOT_SIZE=="large" and 15) or 11
    local CASA_MEIA= 12
    local CALCADA  = 4.1
    -- Profundidade que a casa ocupa PARA TRAS do centro do lote: fundo da
    -- casa + quintal (piscina, deck, horta) + cerca viva. O espacamento das
    -- transversais tem que contar isso, senao a rua de tras passa dentro do
    -- quintal da casa da frente.
    local FUNDO    = 26
    -- "auto" resolve pelo preset. O estilo muda o espacamento das
    -- transversais e o alinhamento dos lotes.
    -- Distancia de um ponto ao eixo de uma via (segmento, nao reta). Usada
    -- para rejeitar lote em cima de outra rua e para limpar a faixa urbana.
    local function distEixo(px,pz,a,b)
        local vx,vz=b.X-a.X,b.Z-a.Z
        local L=vx*vx+vz*vz
        local t=0
        if L>0 then t=math.clamp(((px-a.X)*vx+(pz-a.Z)*vz)/L,0,1) end
        local qx,qz=a.X+vx*t,a.Z+vz*t
        return math.sqrt((px-qx)*(px-qx)+(pz-qz)*(pz-qz))
    end
    -- METROPOLE: cidade e so cidade. Malha em grelha densa nas duas direcoes,
    -- vegetacao zerada no mapa inteiro (nao so na faixa loteavel) e mais
    -- pracas. O terreno correspondente vem do preset do frontend: sem ilha,
    -- sem agua, amplitude baixa.
    local METROPOLE=(BUILDING_PRESET=="metropole")
    -- O leito responde por mais da metade das pecas do mapa. Na metropole o
    -- terreno e plano por construcao, entao segmento longo nao perde nada e
    -- devolve orcamento para as casas.
    if METROPOLE then ROAD_SEG=17 end
    local ESTILO=ROAD_STYLE
    if METROPOLE then
        ESTILO="grid"
    elseif ESTILO=="auto" then
        ESTILO=(BUILDING_PRESET=="coastal_city" and "grid")
            or (BUILDING_PRESET=="mountain_village" and "organic") or "coastal"
    end

    -- ─── 1. ZONA URBANA ─────────────────────────────────────────────────
    -- Sem corte rigido de desnivel: um limite fixo dava "0 candidatas" em
    -- mapa acidentado e o bairro simplesmente nao aparecia. Aqui todo ponto
    -- que esteja inteiramente em terra entra na disputa, o desnivel so pesa
    -- na nota, e a tolerancia dos lotes e derivada da zona escolhida.
    local zc,zbest=nil,-math.huge
    local varridos=0
    for bx=minX+100,maxX-100,44 do
        for bz=minZ+100,maxZ-100,44 do
            local y,dr=pegada(bx,bz,58,1e9)
            if y then
                -- Na Metropole nao existe bonus costeiro: era ele que jogava
                -- a cidade para um canto do mapa, encostada num lago. Aqui a
                -- nota premia o CENTRO, que e onde um centro urbano fica.
                local nc=(not METROPOLE) and nearWater(bx,bz,170) and not nearWater(bx,bz,34) or false
                local alt=math.clamp((y-detectedMinY)/math.max(detectedHeight,1),0,1)
                local sc
                if METROPOLE then
                    local dCentro=(Vector2.new(bx,bz)-Vector2.new(CENTER.X,CENTER.Z)).Magnitude
                    sc=-dr*7-dCentro*0.6
                else
                    sc=-dr*7-alt*70+(nc and 95 or 0)+bhash(bx,bz,911)*14
                end
                if sc>zbest then zbest=sc; zc={x=bx,y=y,z=bz,nc=nc,dr=dr} end
            end
            varridos+=1
            if varridos%160==0 then task.wait() end
        end
    end

    local lotes={}
    local ruas={}
    local usados={}
    local function longe(x,z,d)
        for _,u in usados do
            if (Vector2.new(x,z)-Vector2.new(u.x,u.z)).Magnitude<d then return false end
        end
        return true
    end


    -- TOL acompanha o quao acidentado e o melhor terreno disponivel. Em mapa
    -- plano fica no minimo e o bairro sai alinhado; em mapa de montanha
    -- afrouxa o bastante para a cidade existir.
    local TOL=zc and math.clamp(zc.dr*0.85,7,26) or 10
    local TOL_VIA =zc and math.clamp(math.max(13,zc.dr*1.0),13,30) or 14
    local TOL_LOTE=zc and math.clamp(math.max(9,zc.dr*0.70),9,20)  or 10
    if zc then
        print(string.format("[Map Architect] Zona urbana em (%.0f, %.0f) — %s, desnivel %.1f (tolerancia %.1f).",
              zc.x,zc.z,zc.nc and "costeira" or "interior",zc.dr,TOL))

        -- ─── 2. AVENIDA PRINCIPAL ───────────────────────────────────────
        -- Escolhe a direcao em que o terreno construivel se estende por
        -- mais tempo. Sem isso a avenida entrava na agua ou na encosta.
        -- Um ponto ruim isolado (uma pedra, um talude) nao pode truncar a
        -- avenida inteira: so duas falhas seguidas encerram o traçado.
        local function alcance(dx,dz,limite)
            local d,falhas=0,0
            for s=24,limite,24 do
                if pegada(zc.x+dx*s,zc.z+dz*s,ROAD_W,TOL_VIA) then
                    d=s; falhas=0
                else
                    falhas+=1
                    if falhas>=2 then break end
                end
            end
            return d
        end
        local melhorA,melhorSoma=0,-1
        for a=0,170,15 do
            local ang=math.rad(a)
            local dx,dz=math.cos(ang),math.sin(ang)
            local soma=alcance(dx,dz,URBAN_RADIUS)+alcance(-dx,-dz,URBAN_RADIUS)
            if ESTILO=="coastal" then
                -- Avenida beira-mar: premia a direcao que acompanha a costa
                local litoral=0
                for s2=-URBAN_RADIUS,URBAN_RADIUS,40 do
                    if nearWater(zc.x+dx*s2,zc.z+dz*s2,90) then litoral+=1 end
                end
                soma+=litoral*16
            end
            if soma>melhorSoma then melhorSoma=soma; melhorA=ang end
        end
        local aveDir=Vector3.new(math.cos(melhorA),0,math.sin(melhorA))
        local aveNor=Vector3.new(-aveDir.Z,0,aveDir.X)
        -- Recentragem: a zona costeira ganha nota alta e cai na BORDA da
        -- ilha, entao a grelha so crescia para dentro e a cidade ficava num
        -- canto. Aqui o centro escorrega para o meio do trecho construivel,
        -- nos dois eixos, antes de tracar qualquer coisa.
        for _=1,2 do
            local f1=alcance(aveDir.X,aveDir.Z,URBAN_RADIUS)
            local t1=alcance(-aveDir.X,-aveDir.Z,URBAN_RADIUS)
            local f2=alcance(aveNor.X,aveNor.Z,URBAN_RADIUS)
            local t2=alcance(-aveNor.X,-aveNor.Z,URBAN_RADIUS)
            local dx=(f1-t1)/2
            local dz=(f2-t2)/2
            -- O ponto exato do meio pode cair num lago ou num talude. Em vez
            -- de desistir, recua o deslocamento ate achar apoio: mesmo meio
            -- passo ja tira a cidade da beirada.
            local movido=false
            for _,frac in {1.0,0.75,0.5,0.25} do
                local nx=zc.x+(aveDir.X*dx+aveNor.X*dz)*frac
                local nz=zc.z+(aveDir.Z*dx+aveNor.Z*dz)*frac
                local ny=pegada(nx,nz,ROAD_W*2,TOL_VIA*1.5)
                if ny and (math.abs(dx)+math.abs(dz))*frac>20 then
                    zc={x=nx,y=ny,z=nz,nc=nearWater(nx,nz,170),dr=zc.dr}
                    movido=true
                    break
                end
            end
            if not movido then break end
        end
        print(string.format("[Map Architect] Centro da malha em (%.0f, %.0f).",zc.x,zc.z))
        local frente=alcance(aveDir.X,aveDir.Z,URBAN_RADIUS)
        local tras  =alcance(-aveDir.X,-aveDir.Z,URBAN_RADIUS)
        if frente+tras<110 then
            print("[Map Architect] Zona urbana curta demais — usando raio minimo.")
            frente=math.max(frente,70); tras=math.max(tras,70)
        end
        local aveA=Vector3.new(zc.x-aveDir.X*tras,zc.y,zc.z-aveDir.Z*tras)
        local aveB=Vector3.new(zc.x+aveDir.X*frente,zc.y,zc.z+aveDir.Z*frente)
        table.insert(ruas,{a=aveA,b=aveB,dir=aveDir,nor=aveNor,w=AVE_W,principal=true})

        -- Alcance a partir de um ponto qualquer, nao so do centro da zona.
        -- Precisa disso para tracar as avenidas paralelas.
        local function alcanceDe(ox,oz,dx,dz,limite)
            local d,falhas=0,0
            for st=24,limite,24 do
                if pegada(ox+dx*st,oz+dz*st,ROAD_W,TOL_VIA) then
                    d=st; falhas=0
                else
                    falhas+=1
                    if falhas>=2 then break end
                end
            end
            return d
        end

        -- ─── 3. TRANSVERSAIS ────────────────────────────────────────────
        -- Uma a cada dois lotes de profundidade, saindo dos dois lados.
        -- Duas faixas de lote (ida e volta) mais a caixa da rua. Antes esta
        -- conta ignorava o quintal e o multiplicador do estilo "grid" ainda
        -- encurtava mais: num mapa plano saiam 18 transversais a cada 64
        -- studs, e a rua de tras cortava a piscina da casa da frente.
        local faixa=ROAD_W/2+CALCADA+RECUO+CASA_MEIA+FUNDO
        local passoT=(faixa*2+ROAD_W)
                     *((ESTILO=="grid" and 0.88) or (ESTILO=="organic" and 1.25) or 1.0)
        local comprimento=frente+tras

        -- ─── AVENIDAS PARALELAS ─────────────────────────────────────────
        -- Ate a v0.9.4 a malha era UMA avenida com transversais curtas: num
        -- mapa de 1024 studs a cidade ocupava um canto e o resto ficava vazio,
        -- com cara de zona rural. Agora sao varias avenidas paralelas com o
        -- mesmo passo das quadras, entao o bairro cresce nas duas direcoes
        -- ate onde o terreno permitir.
        -- Quantas avenidas paralelas cabem no orcamento. Cada avenida rende
        -- aproximadamente duas testadas por LOT_W de comprimento, e as
        -- transversais rendem outro tanto — dai o fator 2.
        local porVia=math.max(2,math.floor(comprimento/LOT_W)*2)
        -- Fator calibrado por medicao, nao por teoria: parte das testadas
        -- candidatas e rejeitada pelo terreno ou pelo recuo das outras vias,
        -- entao a malha pode ser tracada mais larga do que a conta ingenua diz.
        local viasCabem=math.max(1,math.floor(LOTES_ALVO*1.3/porVia))
        local tetoEstilo=(METROPOLE and 7) or (ESTILO=="grid" and 4) or (ESTILO=="organic" and 2) or 3
        local MAX_AVE=math.clamp(math.floor(viasCabem/2+0.5),1,tetoEstilo)
        print(string.format("[Map Architect] Orcamento comporta ~%d lotes: %d avenidas por lado.",LOTES_ALVO,MAX_AVE))
        for i=1,MAX_AVE do
            for lado=-1,1,2 do
                local off=i*passoT*lado
                local bx=zc.x+aveNor.X*off
                local bz=zc.z+aveNor.Z*off
                if pegada(bx,bz,ROAD_W,TOL_VIA) then
                    local f2=alcanceDe(bx,bz,aveDir.X,aveDir.Z,URBAN_RADIUS)
                    local t2=alcanceDe(bx,bz,-aveDir.X,-aveDir.Z,URBAN_RADIUS)
                    if f2+t2>=passoT then
                        table.insert(ruas,{
                            a=Vector3.new(bx-aveDir.X*t2,zc.y,bz-aveDir.Z*t2),
                            b=Vector3.new(bx+aveDir.X*f2,zc.y,bz+aveDir.Z*f2),
                            dir=aveDir,nor=aveNor,w=ROAD_W,principal=false})
                    end
                end
            end
            task.wait()
        end
        local nAve=#ruas

        -- ─── TRANSVERSAIS ───────────────────────────────────────────────
        -- Atravessam TODAS as avenidas, formando quadras fechadas.
        local nT=math.max(1,math.floor(comprimento/passoT))
        local larguraMalha=MAX_AVE*passoT+120
        for i=0,nT do
            local t=(i+0.5)/(nT+1)
            local base=aveA:Lerp(aveB,t)
            for lado=-1,1,2 do
                local alc=alcanceDe(base.X,base.Z,aveNor.X*lado,aveNor.Z*lado,larguraMalha)
                if alc>=60 then
                    local fim=Vector3.new(base.X+aveNor.X*lado*alc,base.Y,base.Z+aveNor.Z*lado*alc)
                    table.insert(ruas,{a=base,b=fim,dir=aveNor*lado,nor=aveDir,w=ROAD_W,principal=false,cruz=base})
                end
            end
            task.wait()
        end
        print(string.format("[Map Architect] Malha: %d avenidas (principal com %.0f studs) + %d transversais.",
              nAve,comprimento,#ruas-nAve))

        -- ─── 4. LOTES ───────────────────────────────────────────────────
        -- A testada de cada via e dividida em lotes de LOT_W. O centro da
        -- casa fica a (meia via + calcada + recuo + meia casa) do eixo.
        local off=ROAD_W/2+CALCADA+RECUO+CASA_MEIA
        for _,via in ruas do
            local vetor=via.b-via.a
            local comp=Vector3.new(vetor.X,0,vetor.Z).Magnitude
            local dir=Vector3.new(vetor.X,0,vetor.Z)
            if comp>LOT_W*1.5 then
                dir=dir.Unit
                local nor=Vector3.new(-dir.Z,0,dir.X)
                local offVia=(via.principal and (AVE_W/2) or (ROAD_W/2))+CALCADA+RECUO+CASA_MEIA
                local n=math.floor(comp/LOT_W)
                for i=0,n-1 do
                    local t0=(i+0.5)*LOT_W
                    if t0>26 and t0<comp-16 then
                        for lado=-1,1,2 do
                            -- Se o lote cai em cima de uma transversal, ele e
                            -- deslocado ao longo da testada em vez de ser
                            -- descartado: em esquina o que falta e o recuo,
                            -- nao o lote.
                            local px,pz,y,dr
                            for _,desloc in {0,LOT_W*0.42,-LOT_W*0.42} do
                                local t=t0+desloc
                                if t>20 and t<comp-12 then
                                    local base=via.a+dir*t
                                    -- No tracado organico o recuo varia por
                                    -- lote, o que quebra o alinhamento de
                                    -- regua sem perder a orientacao da fachada.
                                    local jit=(ESTILO=="organic") and (bhash(base.X,base.Z+lado,55)-0.5)*9 or 0
                                    local cx2=base.X+nor.X*lado*(offVia+jit)
                                    local cz2=base.Z+nor.Z*lado*(offVia+jit)
                                    -- A via de frente nao entra na conta: o
                                    -- proprio offVia ja garante o recuo dela.
                                    -- Das outras exige-se a pegada inteira,
                                    -- fundo do quintal incluido.
                                    local livre=true
                                    for _,v2 in ruas do
                                        if v2~=via and distEixo(cx2,cz2,v2.a,v2.b)<(v2.w/2+CALCADA+FUNDO) then
                                            livre=false
                                        end
                                    end
                                    if livre then
                                        local yy,dd=pegada(cx2,cz2,26,TOL_LOTE)
                                        if yy then px,pz,y,dr=cx2,cz2,yy,dd break end
                                    end
                                end
                            end
                            if y and longe(px,pz,LOT_W*0.82) then
                                -- A casa OLHA para a rua: a frente e +Z local,
                                -- que em Roblox e -LookVector. yaw = atan2 do
                                -- vetor casa->rua.
                                local paraRua=-nor*lado
                                local yaw=math.deg(math.atan2(paraRua.X,paraRua.Z))
                                local lote={x=px,y=y,z=pz,yaw=yaw,dr=dr,via=via,
                                            nc=nearWater(px,pz,120)}
                                table.insert(lotes,lote)
                                table.insert(usados,{x=px,z=pz})
                            end
                        end
                    end
                end
            end
        end
        print(string.format("[Map Architect] %d lotes com testada para a rua.",#lotes))

        -- ─── 5. AS VIAS ─────────────────────────────────────────────────
        for _,via in ruas do
            mkRoad(via.a,via.b,via.w)
            local comp=(Vector2.new(via.b.X,via.b.Z)-Vector2.new(via.a.X,via.a.Z)).Magnitude
            local passos=math.ceil(comp/12)
            for k=0,passos do
                local t=k/math.max(passos,1)
                limpar(via.a.X+(via.b.X-via.a.X)*t,via.a.Z+(via.b.Z-via.a.Z)*t,ROAD_W*0.85+5)
            end
            task.wait()
        end

        -- ─── 5b. LIMPEZA DA FAIXA URBANA ────────────────────────────────
        -- Limpar so a beira do leito deixava mata alta entre um quarteirao e
        -- outro: o mapa continuava com cara de floresta com ruas dentro.
        -- Aqui some tudo que estiver dentro da faixa loteavel de qualquer
        -- via. Fora dela a vegetacao fica, e e o que separa cidade de campo.
        local faixaLimpa=ROAD_W/2+CALCADA+RECUO+CASA_MEIA+FUNDO
        local removidos=0
        if METROPOLE then
            for _,pasta in {treesFolder,rocksFolder} do
                for _,obj in pasta:GetChildren() do obj:Destroy(); removidos+=1 end
            end
        else
            for _,pasta in {treesFolder,rocksFolder} do
                for _,obj in pasta:GetChildren() do
                    local op=nil
                    if obj:IsA("Model") then
                        local ok,piv=pcall(function() return obj:GetPivot() end)
                        if ok then op=piv.Position end
                    elseif obj:IsA("BasePart") then op=obj.Position end
                    if op then
                        for _,v2 in ruas do
                            if distEixo(op.X,op.Z,v2.a,v2.b)<faixaLimpa then
                                obj:Destroy(); removidos+=1; break
                            end
                        end
                    end
                end
            end
        end
        print(string.format("[Map Architect] Faixa urbana limpa: %d arvores e pedras removidas.",removidos))

        -- ─── 6. MOBILIARIO AO LONGO DAS CALCADAS ────────────────────────
        local nPostes=0
        for _,via in ruas do
            local vetor=via.b-via.a
            local comp=Vector3.new(vetor.X,0,vetor.Z).Magnitude
            if comp>40 then
                local dir=Vector3.new(vetor.X,0,vetor.Z).Unit
                local nor=Vector3.new(-dir.Z,0,dir.X)
                local yaw=math.deg(math.atan2(dir.X,dir.Z))
                local passo=42
                local n=math.floor(comp/passo)
                for i=1,n do
                    local base=via.a+dir*(i*passo)
                    local lado=(i%2==0) and 1 or -1
                    local px=base.X+nor.X*lado*(via.w/2+2.6)
                    local pz=base.Z+nor.Z*lado*(via.w/2+2.6)
                    local sr=surfaceAt(px,pz)
                    if sr and sr.Position.Y>WATER_Y+1 then
                        poste(Vector3.new(px,sr.Position.Y+0.5,pz),yaw,-lado)
                        nPostes+=1
                        if i%2==0 then
                            local bx=base.X+nor.X*(-lado)*(via.w/2+2.6)
                            local bz=base.Z+nor.Z*(-lado)*(via.w/2+2.6)
                            local br=surfaceAt(bx,bz)
                            if br and br.Position.Y>WATER_Y+1 then
                                if (i//2)%2==0 then
                                    banco(Vector3.new(bx,br.Position.Y+0.6,bz),yaw+90*lado)
                                else
                                    floreira(Vector3.new(bx,br.Position.Y+0.4,bz))
                                end
                            end
                        end
                        if i%3==0 then
                            local lx=base.X+nor.X*lado*(via.w/2+4.4)
                            local lz=base.Z+nor.Z*lado*(via.w/2+4.4)
                            local lr=surfaceAt(lx,lz)
                            if lr then lixeira(Vector3.new(lx,lr.Position.Y+0.4,lz)) end
                        end
                    end
                end
            end
            -- Faixa de pedestre e placa em cada cruzamento
            if via.cruz then
                local cr=surfaceAt(via.cruz.X,via.cruz.Z)
                if cr then
                    local d2=Vector3.new(via.b.X-via.a.X,0,via.b.Z-via.a.Z)
                    if d2.Magnitude>1 then
                        d2=d2.Unit
                        local pos=Vector3.new(via.cruz.X+d2.X*(AVE_W/2+6),cr.Position.Y+0.35,
                                              via.cruz.Z+d2.Z*(AVE_W/2+6))
                        faixaPedestre(pos,d2,via.w)
                        placaRua(Vector3.new(via.cruz.X+d2.X*(AVE_W/2+3)-d2.Z*(via.w/2+3.4),
                                             cr.Position.Y+0.3,
                                             via.cruz.Z+d2.Z*(AVE_W/2+3)+d2.X*(via.w/2+3.4)),
                                 math.deg(math.atan2(d2.X,d2.Z)))
                    end
                end
            end
            task.wait()
        end
        print(string.format("[Map Architect] Mobiliario urbano: %d postes com braco.",nPostes))
    else
        warn("[Map Architect] Nenhuma zona urbana viavel — usando colocacao avulsa.")
    end

    -- ═══ OCUPACAO DOS LOTES ═════════════════════════════════════════════
    -- Ordena: quem esta na avenida e perto do mar recebe os equipamentos.
    table.sort(lotes,function(a,b)
        local sa=(a.via.principal and 60 or 0)+(a.nc and 40 or 0)-a.dr*8
        local sb=(b.via.principal and 60 or 0)+(b.nc and 40 or 0)-b.dr*8
        return sa>sb
    end)

    local ocupado={}
    local hp=nil
    local function tomar(i) ocupado[i]=true end
    -- O `longe` do loteamento usa LOT_W*0.82 (~34 studs), que serve para
    -- casa mas nao para hotel (54 studs de fachada), praca (36) ou
    -- restaurante. Sem reservar a vizinhanca, praca e restaurante nasciam
    -- encostados no hotel — foi o amontoado que apareceu no mapa.
    -- ...mas com parcimonia: num mapa acidentado que so produziu 3 lotes, o
    -- raio do hotel engolia todos e o bairro ficava sem casa nenhuma. A
    -- reserva para de comer lote quando restam poucos livres.
    local function reservar(cx,cz,raio)
        local livres=0
        for j,_ in lotes do if not ocupado[j] then livres+=1 end end
        for j,Q in lotes do
            if livres<=4 then break end
            if not ocupado[j] and (Vector2.new(Q.x,Q.z)-Vector2.new(cx,cz)).Magnitude<raio then
                ocupado[j]=true; livres-=1
            end
        end
    end

    -- HOTEL: precisa de pegada grande (a piscina fica a 58 studs do centro)
    for i,L in lotes do
        if not ocupado[i] then
            local y=pegada(L.x,L.z,58,math.max(15,TOL*1.6))
            if y and (L.nc or hp==nil) then
                limpar(L.x,L.z,64)
                mkHotel(Vector3.new(L.x,y,L.z),L.yaw)
                tomar(i); reservar(L.x,L.z,72); hp={x=L.x,y=y,z=L.z}; buildingCount+=1
                print(string.format("[Map Architect] Hotel em (%.0f, %.0f, %.0f) de frente para a via.",L.x,y,L.z))
                break
            end
        end
    end

    -- Se nenhum lote comportou a pegada do hotel (58 studs de raio por causa
    -- da piscina), procura fora da malha e liga por uma via. Melhor um hotel
    -- na periferia do que mapa nenhum com hotel.
    if not hp then
        local melhor,mSc=nil,-math.huge
        for bx=minX+70,maxX-70,52 do
            for bz=minZ+70,maxZ-70,52 do
                local y,dr=pegada(bx,bz,58,1e9)
                if y then
                    local nc=nearWater(bx,bz,120) and not nearWater(bx,bz,20)
                    -- Penaliza altitude: sem isso o resgate escolhia o plato
                    -- do topo da montanha, que costuma ser o trecho mais
                    -- plano do mapa e o pior lugar para um hotel de praia.
                    local alt=math.clamp((y-detectedMinY)/math.max(detectedHeight,1),0,1)
                    local sc=-dr*8-alt*110+(nc and 90 or 0)
                    if sc>mSc then mSc=sc; melhor={x=bx,y=y,z=bz} end
                end
            end
            task.wait()
        end
        if melhor then
            limpar(melhor.x,melhor.z,64)
            local yawH=zc and math.deg(math.atan2(zc.x-melhor.x,zc.z-melhor.z))
                          or bhash(melhor.x,melhor.z,100)*360
            mkHotel(Vector3.new(melhor.x,melhor.y,melhor.z),yawH)
            hp=melhor; reservar(melhor.x,melhor.z,72); buildingCount+=1
            print(string.format("[Map Architect] Hotel fora da malha em (%.0f, %.0f, %.0f) — ligado por via.",
                  melhor.x,melhor.y,melhor.z))
            if zc then
                mkRoad(Vector3.new(melhor.x,melhor.y,melhor.z),Vector3.new(zc.x,zc.y,zc.z),ROAD_W)
            end
        end
    end

    -- Praca, restaurante e ponto de onibus so entram se ainda houver lote de
    -- sobra. Em mapa acidentado que produziu 3 testadas, os tres equipamentos
    -- consumiam tudo e o bairro ficava sem casa nenhuma — casa tem prioridade.
    local function sobraLotes()
        local n=0
        for j,_ in lotes do if not ocupado[j] then n+=1 end end
        return n
    end

    -- PRACA e RESTAURANTE nos lotes seguintes da avenida
    -- Uma praca no bairro comum; na metropole, uma a cada seis quarteiroes,
    -- que e o que faz a malha respirar em vez de virar so casa e asfalto.
    local pracasAlvo=METROPOLE and math.max(2,math.floor(#lotes/16)) or 1
    local pracas=0
    for i,L in lotes do
        if pracas>=pracasAlvo or sobraLotes()<=3 then break end
        if not ocupado[i] and (L.via.principal or METROPOLE) then
            local y=pegada(L.x,L.z,30,TOL_LOTE*1.3)
            if y then
                limpar(L.x,L.z,30)
                mkPlaza(Vector3.new(L.x,y,L.z)); tomar(i); reservar(L.x,L.z,44); pracas+=1
            end
        end
    end
    if pracas>0 then print(string.format("[Map Architect] %d pracas.",pracas)) end
    for i,L in lotes do
        if sobraLotes()<=3 then break end
        if not ocupado[i] and L.via.principal then
            local y=pegada(L.x,L.z,32,TOL_LOTE*1.3)
            if y then
                limpar(L.x,L.z,32)
                mkRest(Vector3.new(L.x,y,L.z),L.yaw); tomar(i); reservar(L.x,L.z,46); buildingCount+=1; break
            end
        end
    end
    -- PONTO DE ONIBUS na avenida
    for i,L in lotes do
        if sobraLotes()<=3 then break end
        if not ocupado[i] and L.via.principal then
            local sr=surfaceAt(L.x,L.z)
            if sr then
                pontoOnibus(Vector3.new(L.x,sr.Position.Y+0.4,L.z),L.yaw); tomar(i); reservar(L.x,L.z,22); break
            end
        end
    end

    -- CASAS: uma por lote, cada uma com sua propria combinacao de modulos
    -- Teto de casas. Em mapa plano o loteamento chega a produzir 48 testadas
    -- e o limite antigo (18) deixava 30 lotes vazios — o bairro ficava com
    -- buracos justamente onde o terreno era melhor.
    -- Com a malha em grelha o loteamento passou a produzir 59+ testadas. Se o
    -- teto continuasse em 38, o bairro ficaria MAIS espalhado que antes: mais
    -- rua, mesma quantidade de casa. O teto acompanha a malha.
    local maxCasas=math.max(3,math.floor((4+BUILDING_DENSITY*140)*(1-PRESERVE_NATURE*0.55)))
    -- Orcamento de pecas: cada casa custa de 400 a 1100 pecas conforme a
    -- combinacao sorteada, entao contar casas nao basta. Sem este teto um
    -- bairro grande de casas grandes passaria de 40 mil pecas.
    -- Quantas casas ganham detalhe cheio antes de o resto virar versao
    -- economica. Numa vila de 8 lotes todas sao detalhadas; num bairro de
    -- 128, as 20 do centro.
    local DETALHE_CHEIO=math.max(8,math.floor(20000/800))
    local pecas=0
    for _,d in buildingsFolder:GetDescendants() do
        if d:IsA("BasePart") then pecas+=1 end
    end
    local casas=0
    for i,L in lotes do
        if casas>=maxCasas or pecas>=ORCAMENTO then break end
        if not ocupado[i] then
            local y=pegada(L.x,L.z,26,TOL_LOTE)
            if y then
                limpar(L.x,L.z,30)
                -- A seed do lote e derivada da posicao: mesmo mapa, mesmas
                -- casas; mapas diferentes, bairro diferente.
                local hs=math.floor(L.x*7.13+L.z*3.77+BUILDING_SEED)%100000
                -- Detalhe cheio no miolo, versao economica na periferia. Sem
                -- isso o orcamento acabava com metade dos lotes ainda vazios.
                -- Como `lotes` esta ordenado por nota (avenida e costa
                -- primeiro), as primeiras casas sao as do centro.
                local casa=mkHouse(Vector3.new(L.x,y,L.z),L.yaw,hs,
                                   {w=LOT_W-6,d=RECUO+CASA_MEIA},
                                   casas>=DETALHE_CHEIO)
                if casa then
                    for _,d in casa:GetDescendants() do
                        if d:IsA("BasePart") then pecas+=1 end
                    end
                end
                tomar(i); buildingCount+=1; casas+=1
            end
        end
    end
    if pecas>=ORCAMENTO then
        print(string.format("[Map Architect] Orcamento de %d pecas atingido — %d lotes ficaram vazios.",
              ORCAMENTO,#lotes-casas))
    end
    print(string.format("[Map Architect] %d casas em %d lotes — cada uma com planta, telhado, acabamento e quintal proprios.",casas,#lotes))

    -- ═══ RESERVA: mapa sem zona urbana viavel ═══════════════════════════
    -- Mantem o comportamento antigo (pontos soltos ligados por estradas)
    -- para nao ficar sem nada num mapa muito acidentado.
    if buildingCount==0 then
        local spots={}
        for bx=minX+45,maxX-45,44 do
            for bz=minZ+45,maxZ-45,44 do
                local y,dr=pegada(bx,bz,40,26)
                if y then
                    local nc=nearWater(bx,bz,90) and not nearWater(bx,bz,8)
                    table.insert(spots,{x=bx,y=y,z=bz,sc=(nc and 95 or 0)-dr*8,nc=nc})
                end
            end
        end
        table.sort(spots,function(a,b) return a.sc>b.sc end)
        print(string.format("[Map Architect] Reserva: %d areas avulsas.",#spots))
        local col={}
        local function longe2(x,z,d)
            for _,u in col do if (Vector2.new(x,z)-Vector2.new(u.x,u.z)).Magnitude<d then return false end end
            return true
        end
        for _,s in spots do
            if #col==0 then
                limpar(s.x,s.z,62); mkHotel(Vector3.new(s.x,s.y,s.z),bhash(s.x,s.z,100)*360)
                table.insert(col,s); buildingCount+=1; hp={x=s.x,y=s.y,z=s.z}
            elseif #col<=math.floor(3+BUILDING_DENSITY*6) and longe2(s.x,s.z,66) then
                limpar(s.x,s.z,30)
                local hs=math.floor(s.x*7.13+s.z*3.77+BUILDING_SEED)%100000
                mkHouse(Vector3.new(s.x,s.y,s.z),bhash(s.x,s.z,301)*360,hs,nil)
                table.insert(col,s); buildingCount+=1
            end
        end
        for i=2,#col do
            mkRoad(Vector3.new(col[i-1].x,col[i-1].y+0.3,col[i-1].z),
                   Vector3.new(col[i].x,col[i].y+0.3,col[i].z),ROAD_W)
            task.wait()
        end
    end

    -- ═══ PIER ═══════════════════════════════════════════════════════════
    if hp then
        local achou=false
        for a=0,350,15 do
            if achou then break end
            local ang=math.rad(a)
            for d=20,110,10 do
                local px,pz=hp.x+math.cos(ang)*d,hp.z+math.sin(ang)*d
                local pr=surfaceAt(px,pz)
                if pr and pr.Position.Y>WATER_Y+1 and nearWater(px,pz,15) and not nearWater(px,pz,4) then
                    local wd=Vector3.new(0,0,1)
                    for wa=0,350,30 do
                        local waa=math.rad(wa)
                        local tr=surfaceAt(px+math.cos(waa)*22,pz+math.sin(waa)*22)
                        if tr and tr.Position.Y<=WATER_Y+1 then
                            wd=Vector3.new(math.cos(waa),0,math.sin(waa)); break
                        end
                    end
                    mkPier(Vector3.new(px,pr.Position.Y,pz),wd,WATER_Y)
                    buildingCount+=1; achou=true; break
                end
            end
        end
    end
    -- StreamingEnabled: o cliente passa a carregar so a regiao em volta do
    -- jogador em vez do mapa inteiro. Numa cidade espalhada por quase mil
    -- studs isso e a diferenca entre entrar no jogo em segundos ou esperar
    -- o mapa inteiro baixar. So e ligado quando ha peca suficiente para
    -- justificar, e o Output diz como desfazer.
    local totalPecas=0
    for _,pasta in {buildingsFolder,infraFolder} do
        for _,d in pasta:GetDescendants() do
            if d:IsA("BasePart") then totalPecas+=1 end
        end
    end
    print(string.format("[Map Architect] Construcoes: %d edificios, %d pecas na cidade, %d piscinas com agua de terreno.",
          buildingCount,totalPecas,piscinasCheias))
    print(string.format("[Map Architect] Custo de jogo: %d luzes dinamicas (teto %d), %d pecas sem sombra e sem consulta.",
          luzes,LUZ_ORCAMENTO,decorativas))
    if totalPecas>18000 then
        local ok=pcall(function()
            if not workspace.StreamingEnabled then
                workspace.StreamingEnabled=true
                workspace.StreamingTargetRadius=1024
                print("[Map Architect] StreamingEnabled ligado (mapa grande). Para desligar: workspace.StreamingEnabled = false")
            end
        end)
        if not ok then
            warn("[Map Architect] Nao consegui ligar StreamingEnabled — ligue a mao em Workspace se o jogo demorar a carregar.")
        end
    end
end

"""

app = FastAPI(title="Roblox Map Architect Heightmap API", version="1.2.0")
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
    amplitude: float = Field(default=0.55, ge=0.0, le=1.5)
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
    building_preset: Literal["resort", "coastal_city", "mountain_village", "metropole",
                             "forest_horror", "none"] = "none"
    # Floresta: quantas cabanas, e se a ambientacao vai ser de terror.
    # cabin_count = 0 e valido — floresta sem construcao nenhuma.
    cabin_count: int = Field(default=3, ge=0, le=12)
    horror_mood: bool = False
    # Trilha reta em relevo acidentado fica pendurada e denuncia o traçado.
    # Numa floresta densa a ausencia de caminho e o que assusta.
    trails: bool = False
    building_density: float = Field(default=0.5, ge=0.1, le=1.0)
    building_seed: int = 31415
    coastal_shelf: float = Field(default=0.0, ge=0.0, le=1.0)
    asset_hotel: str = Field(default="", max_length=64)
    asset_house: str = Field(default="", max_length=64)
    asset_restaurant: str = Field(default="", max_length=64)
    # Parametros de urbanismo. O index.html ja enviava estes campos desde a
    # v0.8.x, mas o backend nao os declarava e o pydantic os descartava em
    # silencio — por isso os controles de rua/lote nao surtiam efeito.
    road_style: Literal["auto", "coastal", "grid", "organic"] = "auto"
    lot_size: Literal["compact", "medium", "large"] = "medium"
    preserve_nature: float = Field(default=0.55, ge=0.0, le=1.0)
    road_width: int = Field(default=12, ge=8, le=22)
    urban_radius: int = Field(default=260, ge=140, le=480)


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
    # Nivel 0% tem que significar ZERO agua. Antes WATER_Y caia na altura
    # minima do terreno e o preenchimento ainda cobria a faixa de 8 studs
    # abaixo dela: com relevo de 18 studs, toda depressao virava lago. Era o
    # que enchia de agua o mapa da Metropole mesmo com "agua resultante 0%".
    include_water = mode == "full" and req.water_level > 0.005
    clear_only = mode == "clear"
    header = {
        "full": "Pos-importacao: agua, decoracao e spawn",
        "decorations": "Regeneracao somente da decoracao e spawn",
        "clear": "Limpeza somente da decoracao gerada",
        "nocity": "Parte 1 de 2: terreno, agua, decoracao e spawn (sem cidade)",
    }[mode]
    return f'''--[[ ROBLOX MAP ARCHITECT v1.2.0 FLORESTA
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
local ASSET_HOTEL = "{req.asset_hotel}"
local ASSET_HOUSE = "{req.asset_house}"
local ASSET_REST = "{req.asset_restaurant}"
local ROAD_STYLE = "{req.road_style}"
local LOT_SIZE = "{req.lot_size}"
local PRESERVE_NATURE = {req.preserve_nature:.4f}
local ROAD_WIDTH = {req.road_width}
local URBAN_RADIUS = {req.urban_radius}
local CABIN_COUNT = {req.cabin_count}
local HORROR_MOOD = {str(req.horror_mood).lower()}
local TRAILS = {str(req.trails).lower()}

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
    -- Com milhares de arvores, tirar consulta do tronco alivia o cliente sem
    -- perder colisao (o jogador continua batendo nele).
    if FOREST then trunk.CanQuery = false end
    trunk.Color = style == "palm" and Color3.fromRGB(126,88,50) or Color3.fromRGB(103,72,45)
    trunk.Size = style == "palm" and Vector3.new(2.0, trunkHeight, 2.0) or Vector3.new(2.4, trunkHeight, 2.4)

    local yaw = math.rad(hash01(x,z,210)*360)
    local leanX = style == "palm" and math.rad((hash01(x,z,211)-0.5)*8) or 0
    local leanZ = style == "palm" and math.rad((hash01(x,z,212)-0.5)*8) or 0
    -- O tronco subia ao longo da NORMAL da superficie. Em encosta a normal
    -- aponta inclinada, entao a base saia do chao e a arvore ficava no ar.
    -- Sobe na vertical e enterra um pouco, garantindo contato com o terreno.
    trunk.CFrame = CFrame.new(p + Vector3.new(0, trunk.Size.Y/2 - 1.2, 0)) * CFrame.Angles(leanX, yaw, leanZ)
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
            crown.CanTouch = false
            if FOREST then crown.CanQuery = false; crown.CastShadow = false end
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
        core.CanTouch = false
        if FOREST then core.CanQuery = false; core.CastShadow = false end
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
            bulbPart.CanTouch = false
            if FOREST then bulbPart.CanQuery = false; bulbPart.CastShadow = false end
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
            fruit.CanTouch = false
            if FOREST then fruit.CanQuery = false; fruit.CastShadow = false end
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
        crown.CanTouch = false
        if FOREST then crown.CanQuery = false; crown.CastShadow = false end
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
-- Malha de amostragem da vegetacao. Com STEP 20 e teto de 0.28 o maximo
-- possivel eram ~730 arvores num mapa de 1024 — arvore a cada 38 studs, que
-- e pomar, nao floresta. No modo floresta a malha fecha para 11 studs e o
-- teto sobe, o que permite passar de 5 mil arvores.
local FOREST = (BUILDING_PRESET == "forest_horror")
-- A malha aperta no nivel maximo. Com multiplicador fixo o slider saturava:
-- densidade 0.075 e 0.100 davam exatamente as mesmas 5.430 arvores, porque a
-- chance ja batia no teto. Agora o passo tambem responde a densidade.
local STEP = FOREST and ((TREE_DENSITY >= 0.09 and 7) or 9) or 20
for x = minX + STEP/2, maxX - STEP/2, STEP do
    for z = minZ + STEP/2, maxZ - STEP/2, STEP do
        local result = surfaceAt(x,z)
        if result then
            local p, normalY, material = result.Position, result.Normal.Y, result.Material
            local altitude = math.clamp((p.Y-detectedMinY)/detectedHeight,0,1)
            local closeToWater = nearWater(x,z,22)
            local veryCloseToWater = nearWater(x,z,12)

            local treeChance = FOREST and math.min(TREE_DENSITY * 12.0, 0.96)
                                       or math.min(TREE_DENSITY * 6.2, 0.28)
            -- A malha da floresta e 5x mais fina (9 studs contra 20), entao a
            -- MESMA densidade de pedra rende 5x mais pedras. Sem compensar,
            -- baixar o slider nao adiantava: o mapa continuava pedregoso.
            local rockChance = math.min(ROCK_DENSITY * 8.0, 0.16)
            if FOREST then rockChance *= 0.10 end
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

            local minNY = FOREST and math.min(TREE_MIN_NORMAL_Y,0.55) or TREE_MIN_NORMAL_Y
            local treeAllowed = TREE_MATERIALS[material] and normalY >= minNY and p.Y > WATER_Y + (FOREST and 1.5 or 5)
            if style == "palm" then
                -- Palmeira só em praia/faixa costeira relativamente plana.
                -- A checagem de altura da agua estava faltando aqui: esta regra
                -- sobrescreve treeAllowed por completo, entao a palmeira podia
                -- nascer dentro do lago.
                treeAllowed = (material == Enum.Material.Sand or material == Enum.Material.Ground or material == Enum.Material.Grass)
                    and material ~= Enum.Material.Water
                    and p.Y > WATER_Y + 4
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
''' + ('''
-- A cidade roda dentro de uma funcao, nao solta no chunk principal: o bloco
-- declara mais de 200 locais e o Lua/Luau tem limite de 200 por funcao. Foi o
-- validador que pegou isso — o script inteiro deixaria de compilar no Studio.
local function construirCidade()
if not (PLACE_BUILDINGS and BUILDING_PRESET ~= "none") then return end
''' + LUA_BUILD_LIB + LUA_FOREST_PLAN + LUA_URBAN_PLAN + '''
construirCidade()
''' if mode not in ("nocity", "clear") else "") + f'''

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


def build_city_lua(req: GenerateRequest, job_id: str) -> str:
    """Parte 2 de 2: SO a cidade.

    O script completo passou de 72 KB para ~113 KB com o urbanismo da v0.9.
    Para quem a Command Bar reclamar do tamanho, este par (setup_world_nocity
    + setup_city) faz exatamente a mesma coisa em duas colagens. O corte e no
    ponto que ja estava mapeado: `if PLACE_BUILDINGS`.

    Este script se vira sozinho — redetecta o terreno e reaproveita as pastas
    que a parte 1 criou, entao pode ser rodado de novo para so refazer a
    cidade sem reimportar nada.
    """
    map_x = req.resolution * 4
    map_z = req.resolution * 4
    prelude = f'''--[[ ROBLOX MAP ARCHITECT v1.2.0 FLORESTA
Parte 2 de 2: cidade (edificios, malha viaria, lotes e mobiliario urbano)
Job: {job_id}
Rode a parte 1 (setup_world_nocity) ANTES desta.
Pode ser executado varias vezes: refaz so a cidade.
]]
local Terrain = workspace.Terrain
local VOXEL = 4
local MAP_SIZE_X = {map_x}
local MAP_SIZE_Y = {req.import_vertical_size}
local MAP_SIZE_Z = {map_z}
local CENTER = Vector3.new({req.import_center_x}, {req.import_center_y}, {req.import_center_z})
local WATER_LEVEL = {req.water_level:.6f}
local PLACE_BUILDINGS = true
local BUILDING_PRESET = "{req.building_preset}"
local BUILDING_DENSITY = {req.building_density:.4f}
local BUILDING_SEED = {req.building_seed}
local ASSET_HOTEL = "{req.asset_hotel}"
local ASSET_HOUSE = "{req.asset_house}"
local ASSET_REST = "{req.asset_restaurant}"
local ROAD_STYLE = "{req.road_style}"
local LOT_SIZE = "{req.lot_size}"
local PRESERVE_NATURE = {req.preserve_nature:.4f}
local ROAD_WIDTH = {req.road_width}
local URBAN_RADIUS = {req.urban_radius}
local CABIN_COUNT = {req.cabin_count}
local HORROR_MOOD = {str(req.horror_mood).lower()}
local TRAILS = {str(req.trails).lower()}

local minX = CENTER.X - MAP_SIZE_X/2
local minZ = CENTER.Z - MAP_SIZE_Z/2
local maxX = CENTER.X + MAP_SIZE_X/2
local maxZ = CENTER.Z + MAP_SIZE_Z/2

local generated = workspace:FindFirstChild("GeneratedMap")
if not generated then
    generated = Instance.new("Folder"); generated.Name = "GeneratedMap"; generated.Parent = workspace
end
local function pasta(nome)
    local f = generated:FindFirstChild(nome)
    if not f then f = Instance.new("Folder"); f.Name = nome; f.Parent = generated end
    return f
end
local treesFolder = pasta("Trees")
local rocksFolder = pasta("Rocks")
local buildingsFolder = pasta("Buildings")
local infraFolder = pasta("Infrastructure")
-- Re-execucao: limpa so a cidade, preserva arvores, pedras e spawn
buildingsFolder:ClearAllChildren()
infraFolder:ClearAllChildren()

local raycastParams = RaycastParams.new()
raycastParams.FilterType = Enum.RaycastFilterType.Exclude
raycastParams.FilterDescendantsInstances = {{generated}}
raycastParams.IgnoreWater = false

local terrainOnly = RaycastParams.new()
terrainOnly.FilterType = Enum.RaycastFilterType.Include
terrainOnly.FilterDescendantsInstances = {{Terrain}}
terrainOnly.IgnoreWater = true

local detectedMinY, detectedMaxY, detectedSamples = math.huge, -math.huge, 0
for x = minX, maxX, math.max(16, math.floor(MAP_SIZE_X/32)) do
    for z = minZ, maxZ, math.max(16, math.floor(MAP_SIZE_Z/32)) do
        local res = workspace:Raycast(Vector3.new(x, CENTER.Y + MAP_SIZE_Y*4, z),
                                      Vector3.new(0, -MAP_SIZE_Y*8, 0), terrainOnly)
        if res and res.Instance == Terrain then
            detectedMinY = math.min(detectedMinY, res.Position.Y)
            detectedMaxY = math.max(detectedMaxY, res.Position.Y)
            detectedSamples = detectedSamples + 1
        end
    end
end
if detectedSamples == 0 then
    error("[Map Architect] Nenhum terreno encontrado. Importe o heightmap e rode a parte 1 antes desta.")
end
local detectedHeight = math.max(detectedMaxY - detectedMinY, VOXEL)
local WATER_Y = detectedMinY + detectedHeight * WATER_LEVEL
print(string.format("[Map Architect] Terreno: %d amostras, de %.1f a %.1f, agua em %.1f.",
      detectedSamples, detectedMinY, detectedMaxY, WATER_Y))

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
local buildingCount = 0

-- Mesma razao do script completo: mais de 200 locais nao cabem num chunk.
local function construirCidade()
if not (PLACE_BUILDINGS and BUILDING_PRESET ~= "none") then return end
'''
    return prelude + LUA_BUILD_LIB + LUA_FOREST_PLAN + LUA_URBAN_PLAN + "\nconstruirCidade()\n"


def build_test_buildings_lua(req: GenerateRequest, job_id: str) -> str:
    """Bancada de DEBUG: plataforma plana com uma de cada construcao.

    Usa a MESMA biblioteca do mapa (LUA_BUILD_LIB). Ate a v0.8.7 a bancada
    tinha uma copia congelada das construcoes da v0.7 e mostrava uma casa que
    nao existia mais no mapa — inutil justamente para o que ela serve.

    Aqui ficam lado a lado oito casas com seeds diferentes: e o jeito mais
    rapido de ver se a composicao esta mesmo produzindo casas diferentes.
    """
    return f'''-- ROBLOX MAP ARCHITECT v1.2.0 - BANCADA DE CONSTRUCOES (DEBUG)
-- Job {job_id}
-- Cole na Command Bar. Nao precisa de heightmap nem de terreno.

local old = workspace:FindFirstChild("BuildingTestBench")
if old then old:Destroy() end
local bench = Instance.new("Folder"); bench.Name = "BuildingTestBench"; bench.Parent = workspace
local buildingsFolder = Instance.new("Folder"); buildingsFolder.Name = "Buildings"; buildingsFolder.Parent = bench
local infraFolder = Instance.new("Folder"); infraFolder.Name = "Infra"; infraFolder.Parent = bench

local BUILDING_SEED = {req.building_seed}
local ASSET_HOTEL, ASSET_HOUSE, ASSET_REST = "", "", ""
local CABIN_COUNT, HORROR_MOOD, TRAILS = 0, false, false
local BUILDING_PRESET, WATER_LEVEL = "bench", 0
local BASE_Y = 0
local WATER_Y = -500

-- Chao falso: devolve o mesmo formato que o Raycast devolveria, entao a
-- biblioteca roda sem alteracao nenhuma.
local function surfaceAt(x, z)
    return {{ Position = Vector3.new(x, BASE_Y, z),
             Normal = Vector3.new(0, 1, 0),
             Material = Enum.Material.Grass }}
end

local function label(txt, pos)
    local part = Instance.new("Part"); part.Anchored = true; part.CanCollide = false
    part.Size = Vector3.new(1,1,1); part.Transparency = 1; part.Position = pos; part.Parent = bench
    local bb = Instance.new("BillboardGui"); bb.Size = UDim2.new(0,300,0,46); bb.AlwaysOnTop = true
    bb.StudsOffset = Vector3.new(0,2,0); bb.Parent = part
    local tl = Instance.new("TextLabel"); tl.Size = UDim2.new(1,0,1,0); tl.BackgroundTransparency = 0.35
    tl.BackgroundColor3 = Color3.fromRGB(18,20,24); tl.TextColor3 = Color3.fromRGB(240,240,240)
    tl.TextScaled = true; tl.Font = Enum.Font.GothamBold; tl.Text = txt; tl.Parent = bb
end

local PW, PD = 900, 340
local plat = Instance.new("Part"); plat.Name = "Platform"; plat.Anchored = true
plat.Size = Vector3.new(PW,4,PD); plat.CFrame = CFrame.new(0,BASE_Y-2,0)
plat.Material = Enum.Material.Grass; plat.Color = Color3.fromRGB(112,148,88); plat.Parent = bench
local ref = Instance.new("Part"); ref.Anchored = true; ref.Size = Vector3.new(2,5,1)
ref.CFrame = CFrame.new(-PW/2+18,BASE_Y+2.5,90); ref.Material = Enum.Material.Neon
ref.Color = Color3.fromRGB(255,80,80); ref.Parent = bench
label("REFERENCIA: 5 studs = altura de um player", Vector3.new(-PW/2+18,BASE_Y+9,90))

if true then
''' + LUA_BUILD_LIB + LUA_FOREST_PLAN + f'''
    -- OITO CASAS, OITO SEEDS: se duas sairem iguais, a composicao falhou.
    for i = 0, 7 do
        local hs = {req.building_seed} + i * 977
        mkHouse(Vector3.new(-PW/2 + 95 + i*100, BASE_Y, -60), 180, hs, {{w=46, d=23}})
        label("CASA seed "..hs, Vector3.new(-PW/2 + 95 + i*100, BASE_Y + 30, -60))
    end
    mkHotel(Vector3.new(-260, BASE_Y, 105), 180)
    label("HOTEL", Vector3.new(-260, BASE_Y + 46, 105))
    mkRest(Vector3.new(-60, BASE_Y, 105), 180)
    label("RESTAURANTE", Vector3.new(-60, BASE_Y + 16, 105))
    mkPlaza(Vector3.new(80, BASE_Y, 105))
    label("PRACA", Vector3.new(80, BASE_Y + 12, 105))
    mkRoad(Vector3.new(-PW/2+40, BASE_Y, 20), Vector3.new(PW/2-40, BASE_Y, 20), 16)
    label("AVENIDA 16 studs", Vector3.new(0, BASE_Y + 10, 20))
    -- Tres cabanas com o mesmo desenho e abandono crescente: e o jeito de ver
    -- o que o parametro faz sem gerar tres mapas.
    for i = 0, 2 do
        local ab = 0.2 + i * 0.35
        mkCabin(Vector3.new(220 + i * 90, BASE_Y, 105), 180, {req.building_seed} + i * 313, ab)
        label(string.format("CABANA abandono %.0f%%", ab * 100), Vector3.new(220 + i * 90, BASE_Y + 22, 105))
    end
end

local lighting = game:GetService("Lighting")
lighting.ClockTime = 14
lighting.Brightness = 2.4
lighting.Ambient = Color3.fromRGB(120,124,132)
lighting.OutdoorAmbient = Color3.fromRGB(138,142,150)

local n = 0
for _, d in bench:GetDescendants() do if d:IsA("BasePart") then n = n + 1 end end
print(string.format("[Map Architect] Bancada criada: %d pecas em workspace.BuildingTestBench", n))
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
   Se a Command Bar reclamar do tamanho, use o par em duas colagens:
     6a. setup_world_nocity_{job_id}.lua   (terreno, agua, decoracao, spawn)
     6b. setup_city_{job_id}.lua           (cidade; pode rodar de novo sozinho)
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
        result, water_filter = postprocess_terrain(result, base, req.water_level, req.coastal_shelf)

    if erosion_cfg is None:
        result, water_filter = postprocess_terrain(result, base, req.water_level, req.coastal_shelf)

    job_id = uuid.uuid4().hex[:16]
    stem = f"heightmap_{job_id}"
    png_path = OUTPUT_DIR / f"{stem}.png"
    preview_path = OUTPUT_DIR / f"{stem}_preview.png"
    colormap_path = OUTPUT_DIR / f"{stem}_colormap.png"
    lua_path = OUTPUT_DIR / f"setup_world_{job_id}.lua"
    decorations_lua_path = OUTPUT_DIR / f"regenerate_decorations_{job_id}.lua"
    clear_lua_path = OUTPUT_DIR / f"clear_decorations_{job_id}.lua"
    test_buildings_path = OUTPUT_DIR / f"test_buildings_{job_id}.lua"
    world_nocity_path = OUTPUT_DIR / f"setup_world_nocity_{job_id}.lua"
    city_path = OUTPUT_DIR / f"setup_city_{job_id}.lua"
    instructions_path = OUTPUT_DIR / f"instructions_{job_id}.txt"
    package_path = OUTPUT_DIR / f"map_package_{job_id}.zip"
    meta_path = OUTPUT_DIR / f"{stem}.json"

    # ── Base para mapa plano sem agua ──────────────────────────────────
    # Com amplitude 0.01 o terreno ocupa 1% dos 16 bits: o PNG sai preto e,
    # pior, a laje importada fica com 2,6 studs de espessura — uma folha.
    # Esticar para a faixa cheia nao serve, porque isso multiplicaria o
    # relevo e traria de volta o terraco de voxel. A saida e DESLOCAR: o
    # relevo continua com 2,6 studs, mas apoiado sobre 90 studs de corpo.
    # So se aplica quando nao ha agua nenhuma; com agua, a posicao relativa
    # da lamina depende da faixa e mexer nela quebraria o nivel pedido.
    # ── PISO MINIMO: terreno com espessura ZERO e buraco sem fundo ──────
    # O import do Roblox preenche do fundo da regiao ate a altura do pixel.
    # Onde o heightmap vale 0 nao se cria voxel nenhum: fica um buraco por
    # onde o jogador cai para fora do mundo. Isso acontecia sempre que o
    # relevo encostava no zero — no preset Terror eram 885 pixels, 1,35% do
    # mapa. Antes so o mapa plano era protegido, e por outro motivo.
    #
    # Piso de 12% da altura importada = ~31 studs de rocha sob o ponto mais
    # baixo. O relevo e preservado: a operacao e um DESLOCAMENTO, e so
    # comprime se nao houver espaco no topo.
    export = result
    span = float(result.max() - result.min())
    PISO_MINIMO = 0.12
    BASE_PLANA = 0.35
    alvo = BASE_PLANA if (req.water_level <= 0.005 and span < 0.20) else PISO_MINIMO
    if float(result.min()) < alvo:
        export = result - float(result.min()) + alvo
        topo = float(export.max())
        if topo > 1.0:
            # Sem espaco: comprime o relevo em vez de cortar o topo (cortar
            # criaria um plato plano no cume, que se ve de longe).
            export = alvo + (export - alvo) * ((1.0 - alvo) / (topo - alvo))
        export = np.clip(export, 0.0, 1.0)
        print(f"[gen] piso minimo: base em {alvo:.2f} "
              f"({alvo*req.import_vertical_size:.0f} studs de rocha sob o ponto mais baixo), "
              f"relevo {span*req.import_vertical_size:.1f} studs")

    # A paleta e escolhida pelo `preset`, mas quem sabe que tipo de mapa e
    # este e o `building_preset`: o frontend manda preset "floresta"/"terror"
    # e a tabela de paletas nao conhece esses nomes — cairia no fallback e
    # traria a praia de areia de volta.
    paleta = req.preset.lower()
    if req.building_preset in ("forest_horror", "metropole"):
        paleta = req.building_preset
    save_png16(png_path, export)
    save_preview(preview_path, export, req.water_level, paleta)
    save_colormap(colormap_path, export, req.water_level, paleta)
    lua_path.write_text(build_setup_lua(req, job_id, "full"), encoding="utf-8")
    decorations_lua_path.write_text(build_setup_lua(req, job_id, "decorations"), encoding="utf-8")
    clear_lua_path.write_text(build_setup_lua(req, job_id, "clear"), encoding="utf-8")
    test_buildings_path.write_text(build_test_buildings_lua(req, job_id), encoding="utf-8")
    # Par alternativo para quem a Command Bar reclamar do script inteiro
    world_nocity_path.write_text(build_setup_lua(req, job_id, "nocity"), encoding="utf-8")
    city_path.write_text(build_city_lua(req, job_id), encoding="utf-8")
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
        "script_kb": {
            "setup_world": round(lua_path.stat().st_size / 1024, 1),
            "setup_world_nocity": round(world_nocity_path.stat().st_size / 1024, 1),
            "setup_city": round(city_path.stat().st_size / 1024, 1),
        },
        "files": {
            "heightmap": f"/files/{png_path.name}",
            "preview": f"/files/{preview_path.name}",
            "colormap": f"/files/{colormap_path.name}",
            "setup_lua": f"/files/{lua_path.name}",
            "regenerate_decorations_lua": f"/files/{decorations_lua_path.name}",
            "clear_decorations_lua": f"/files/{clear_lua_path.name}",
            "test_buildings_lua": f"/files/{test_buildings_path.name}",
            "setup_world_nocity_lua": f"/files/{world_nocity_path.name}",
            "setup_city_lua": f"/files/{city_path.name}",
            "instructions": f"/files/{instructions_path.name}",
            "package": f"/files/{package_path.name}",
            "metadata": f"/files/{meta_path.name}",
        },
    }
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in (png_path, preview_path, colormap_path, lua_path, decorations_lua_path, clear_lua_path, test_buildings_path, world_nocity_path, city_path, instructions_path, meta_path):
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
