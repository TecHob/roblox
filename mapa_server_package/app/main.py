from __future__ import annotations

import io
import json
import time
import uuid
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
    stats,
)

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
OUTPUT_DIR = ROOT_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Roblox Map Architect Heightmap API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://precisao-inova-r-mapa.netlify.app",
        "http://localhost",
        "http://127.0.0.1",
    ],
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
    response: Literal["json", "png", "preview"] = "json"


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
    meta_path = OUTPUT_DIR / f"{stem}.json"

    save_png16(png_path, result)
    save_preview(preview_path, result, req.water_level)

    metadata = {
        "id": job_id,
        "seconds": round(time.perf_counter() - started, 4),
        "terrain": asdict(cfg),
        "erosion": req.erosion,
        "erosion_config": asdict(erosion_cfg) if erosion_cfg else None,
        "stats": stats(result, req.water_level),
        "water_filter": water_filter,
        "files": {
            "heightmap": f"/files/{png_path.name}",
            "preview": f"/files/{preview_path.name}",
            "metadata": f"/files/{meta_path.name}",
        },
    }
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    if req.response == "png":
        return FileResponse(png_path, media_type="image/png", filename=png_path.name)
    if req.response == "preview":
        return FileResponse(preview_path, media_type="image/png", filename=preview_path.name)
    return JSONResponse(metadata)


@app.get("/files/{filename}")
def get_file(filename: str):
    safe_name = Path(filename).name
    path = OUTPUT_DIR / safe_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")
    media_type = "image/png" if path.suffix.lower() == ".png" else "application/json"
    return FileResponse(path, media_type=media_type, filename=path.name)
