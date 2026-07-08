#!/usr/bin/env python3
"""Roblox Map Architect - Heightmap PoC with hydraulic erosion.

Replicates the frontend Perlin/ridged height formula from public/index.html,
then applies deterministic droplet-based hydraulic erosion.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from PIL import Image
try:
    from numba import njit
except ImportError:
    def njit(*args, **kwargs):
        def deco(fn): return fn
        return deco


class Perlin:
    def __init__(self, seed: int = 42):
        pm = list(range(256))
        s = int(seed)
        for i in range(255, 0, -1):
            s = (s * 16807) % 2147483647
            j = s % (i + 1)
            pm[i], pm[j] = pm[j], pm[i]
        self.p = np.array([pm[i & 255] for i in range(512)], dtype=np.int32)

    @staticmethod
    def fade(t):
        return t * t * t * (t * (t * 6 - 15) + 10)

    @staticmethod
    def lerp(a, b, t):
        return a + t * (b - a)

    @staticmethod
    def grad(h, x, y):
        g = h & 3
        u = np.where(g < 2, x, y)
        v = np.where(g < 2, y, x)
        return np.where((g & 1) != 0, -u, u) + np.where((g & 2) != 0, -v, v)

    def noise_grid(self, x, y):
        xf0 = np.floor(x)
        yf0 = np.floor(y)
        X = xf0.astype(np.int32) & 255
        Y = yf0.astype(np.int32) & 255
        xf = x - xf0
        yf = y - yf0
        u = self.fade(xf)
        v = self.fade(yf)
        p = self.p
        a = p[X] + Y
        b = p[X + 1] + Y
        x1 = self.lerp(self.grad(p[a], xf, yf), self.grad(p[b], xf - 1, yf), u)
        x2 = self.lerp(self.grad(p[a + 1], xf, yf - 1), self.grad(p[b + 1], xf - 1, yf - 1), u)
        return self.lerp(x1, x2, v)

    def fbm_grid(self, x, y, octaves=6, lacunarity=2.0, gain=0.5):
        total = np.zeros_like(x, dtype=np.float64)
        amp = 1.0
        freq = 1.0
        max_amp = 0.0
        for _ in range(octaves):
            total += self.noise_grid(x * freq, y * freq) * amp
            max_amp += amp
            amp *= gain
            freq *= lacunarity
        return total / max_amp

    def ridged_grid(self, x, y, octaves=4, lacunarity=2.0, gain=0.5):
        total = np.zeros_like(x, dtype=np.float64)
        amp = 1.0
        freq = 1.0
        max_amp = 0.0
        for _ in range(octaves):
            n = self.noise_grid(x * freq, y * freq)
            n = 1.0 - np.abs(n * 2.0)
            n *= n
            total += n * amp
            max_amp += amp
            amp *= gain
            freq *= lacunarity
        return total / max_amp


@dataclass
class TerrainConfig:
    seed: int = 42
    resolution: int = 256
    amplitude: float = 0.55
    water_level: float = 0.20
    octaves: int = 6
    lacunarity: float = 2.0
    gain: float = 0.5
    mountain_threshold: float = 0.60
    mountain_amplitude: float = 0.40
    island_mode: bool = False
    volcano: bool = False


@dataclass
class ErosionConfig:
    droplets: int = 18000
    max_steps: int = 40
    inertia: float = 0.05
    sediment_capacity_factor: float = 4.0
    min_sediment_capacity: float = 0.01
    deposit_speed: float = 0.30
    erode_speed: float = 0.30
    evaporate_speed: float = 0.01
    gravity: float = 4.0
    start_speed: float = 1.0
    start_water: float = 1.0
    erosion_radius: int = 3


def generate_base(cfg: TerrainConfig) -> np.ndarray:
    r = cfg.resolution
    yy, xx = np.mgrid[0:r, 0:r]
    nx = xx / r
    ny = yy / r
    sx = nx * 8.0
    sy = ny * 8.0
    p = Perlin(cfg.seed)
    h = (p.fbm_grid(sx, sy, cfg.octaves, cfg.lacunarity, cfg.gain) + 1.0) * 0.5
    ridge = p.ridged_grid(sx * 0.5, sy * 0.5, 4, cfg.lacunarity, cfg.gain)
    mask = ridge > cfg.mountain_threshold
    h[mask] += (ridge[mask] - cfg.mountain_threshold) * cfg.mountain_amplitude
    h *= cfg.amplitude
    if cfg.island_mode:
        cx = nx - 0.5
        cy = ny - 0.5
        falloff = np.maximum(0.0, 1.0 - (np.sqrt(cx * cx + cy * cy) * 2.0) ** 1.5)
        h *= falloff
    if cfg.volcano:
        cx = nx - 0.5
        cy = ny - 0.5
        center = np.sqrt(cx * cx + cy * cy) < 0.08
        h[center & (h > 0.75)] *= 0.4
    return np.clip(h, 0.0, 1.0).astype(np.float64)


@njit(cache=True)
def _sample_height_gradient_numba(h, x, y):
    xi = int(x)
    yi = int(y)
    ox = x - xi
    oy = y - yi
    h00 = h[yi, xi]
    h10 = h[yi, xi + 1]
    h01 = h[yi + 1, xi]
    h11 = h[yi + 1, xi + 1]
    height = h00 * (1 - ox) * (1 - oy) + h10 * ox * (1 - oy) + h01 * (1 - ox) * oy + h11 * ox * oy
    grad_x = (h10 - h00) * (1 - oy) + (h11 - h01) * oy
    grad_y = (h01 - h00) * (1 - ox) + (h11 - h10) * ox
    return height, grad_x, grad_y


@njit(cache=True)
def _hydraulic_erosion_numba(source, droplets, max_steps, inertia,
                              sediment_capacity_factor, min_sediment_capacity,
                              deposit_speed, erode_speed, evaporate_speed,
                              gravity, start_speed, start_water,
                              erosion_radius, seed):
    h = source.copy()
    n = h.shape[0]
    np.random.seed(seed + 9137)

    max_brush = (erosion_radius * 2 + 1) ** 2
    off_x = np.zeros(max_brush, dtype=np.int32)
    off_y = np.zeros(max_brush, dtype=np.int32)
    weights = np.zeros(max_brush, dtype=np.float64)
    count = 0
    total_w = 0.0
    for oy in range(-erosion_radius, erosion_radius + 1):
        for ox in range(-erosion_radius, erosion_radius + 1):
            d = math.sqrt(ox * ox + oy * oy)
            if d < erosion_radius:
                w = 1.0 - d / erosion_radius
                off_x[count] = ox
                off_y[count] = oy
                weights[count] = w
                total_w += w
                count += 1
    for i in range(count):
        weights[i] /= total_w

    for _ in range(droplets):
        x = 1.0 + np.random.random() * (n - 3.0)
        y = 1.0 + np.random.random() * (n - 3.0)
        dir_x = 0.0
        dir_y = 0.0
        speed = start_speed
        water = start_water
        sediment = 0.0

        for _step in range(max_steps):
            old_x = x
            old_y = y
            old_height, grad_x, grad_y = _sample_height_gradient_numba(h, x, y)
            dir_x = dir_x * inertia - grad_x * (1.0 - inertia)
            dir_y = dir_y * inertia - grad_y * (1.0 - inertia)
            length = math.sqrt(dir_x * dir_x + dir_y * dir_y)
            if length < 1e-12:
                angle = np.random.random() * math.tau
                dir_x = math.cos(angle)
                dir_y = math.sin(angle)
            else:
                dir_x /= length
                dir_y /= length
            x += dir_x
            y += dir_y
            if x < 1 or x >= n - 2 or y < 1 or y >= n - 2:
                break

            new_height, _, _ = _sample_height_gradient_numba(h, x, y)
            delta = new_height - old_height
            capacity = max(-delta * speed * water * sediment_capacity_factor, min_sediment_capacity)

            if sediment > capacity or delta > 0:
                amount = delta if delta > 0 else (sediment - capacity) * deposit_speed
                amount = min(amount, sediment)
                sediment -= amount
                xi = int(old_x)
                yi = int(old_y)
                ox = old_x - xi
                oy = old_y - yi
                h[yi, xi] += amount * (1 - ox) * (1 - oy)
                h[yi, xi + 1] += amount * ox * (1 - oy)
                h[yi + 1, xi] += amount * (1 - ox) * oy
                h[yi + 1, xi + 1] += amount * ox * oy
            else:
                amount = min((capacity - sediment) * erode_speed, -delta)
                cx = int(old_x)
                cy = int(old_y)
                removed = 0.0
                for i in range(count):
                    px = cx + off_x[i]
                    py = cy + off_y[i]
                    if 0 <= px < n and 0 <= py < n:
                        take = min(h[py, px], amount * weights[i])
                        h[py, px] -= take
                        removed += take
                sediment += removed

            speed = math.sqrt(max(0.0, speed * speed + (-delta) * gravity))
            water *= 1.0 - evaporate_speed
            if water < 0.01:
                break
    return h


def hydraulic_erosion(source: np.ndarray, cfg: ErosionConfig, seed: int) -> np.ndarray:
    h = _hydraulic_erosion_numba(
        source, cfg.droplets, cfg.max_steps, cfg.inertia,
        cfg.sediment_capacity_factor, cfg.min_sediment_capacity,
        cfg.deposit_speed, cfg.erode_speed, cfg.evaporate_speed,
        cfg.gravity, cfg.start_speed, cfg.start_water,
        cfg.erosion_radius, seed,
    )
    return np.clip(h, 0.0, 1.0)


def normalize_preserving_range(h: np.ndarray, reference: np.ndarray) -> np.ndarray:
    # Keep the same overall amplitude envelope as the original map.
    lo, hi = float(reference.min()), float(reference.max())
    h_lo, h_hi = float(h.min()), float(h.max())
    if h_hi - h_lo < 1e-9:
        return np.full_like(h, lo)
    result = (h - h_lo) / (h_hi - h_lo)
    return np.clip(lo + result * (hi - lo), 0.0, 1.0)


def save_png16(path: Path, h: np.ndarray):
    arr = np.round(np.clip(h, 0, 1) * 65535).astype(np.uint16)
    Image.fromarray(arr, mode='I;16').save(path)


def save_preview(path: Path, h: np.ndarray, water_level: float):
    # Colored diagnostic preview; grayscale PNG remains the Roblox import artifact.
    water = h < water_level
    gray = np.round(np.clip(h, 0, 1) * 255).astype(np.uint8)
    rgb = np.stack([gray, gray, gray], axis=-1)
    rgb[water, 0] = (gray[water] * 0.10).astype(np.uint8)
    rgb[water, 1] = (gray[water] * 0.45 + 35).astype(np.uint8)
    rgb[water, 2] = (gray[water] * 0.55 + 70).astype(np.uint8)
    Image.fromarray(rgb, mode='RGB').save(path)


def stats(h: np.ndarray, water_level: float):
    gy, gx = np.gradient(h)
    slope = np.sqrt(gx * gx + gy * gy)
    return {
        'min': round(float(h.min()), 6),
        'max': round(float(h.max()), 6),
        'mean': round(float(h.mean()), 6),
        'std': round(float(h.std()), 6),
        'water_area_percent': round(float((h < water_level).mean() * 100), 3),
        'mean_slope': round(float(slope.mean()), 7),
        'max_slope': round(float(slope.max()), 7),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--resolution', type=int, default=256)
    parser.add_argument('--preset', choices=['rpg', 'tropical', 'custom'], default='rpg')
    parser.add_argument('--output', type=Path, default=Path('output'))
    args = parser.parse_args()

    presets = {
        'rpg': dict(amplitude=0.55, water_level=0.20, island_mode=False),
        'tropical': dict(amplitude=0.70, water_level=0.30, island_mode=True),
        'custom': dict(amplitude=0.70, water_level=0.30, island_mode=False),
    }
    terrain = TerrainConfig(seed=args.seed, resolution=args.resolution, **presets[args.preset])
    levels = {
        'light': ErosionConfig(droplets=max(4000, args.resolution * 25), erode_speed=0.18, deposit_speed=0.22, erosion_radius=2),
        'medium': ErosionConfig(droplets=max(9000, args.resolution * 55), erode_speed=0.30, deposit_speed=0.30, erosion_radius=3),
        'strong': ErosionConfig(droplets=max(16000, args.resolution * 90), erode_speed=0.42, deposit_speed=0.38, erosion_radius=3),
    }

    args.output.mkdir(parents=True, exist_ok=True)
    report = {'terrain_config': asdict(terrain), 'runs': {}}

    t0 = time.perf_counter()
    base = generate_base(terrain)
    base_time = time.perf_counter() - t0
    stem = f'{args.preset}_seed_{args.seed}_{args.resolution}'
    save_png16(args.output / f'{stem}_original.png', base)
    save_preview(args.output / f'{stem}_original_preview.png', base, terrain.water_level)
    report['runs']['original'] = {'seconds': round(base_time, 3), 'stats': stats(base, terrain.water_level)}

    for name, erosion_cfg in levels.items():
        t0 = time.perf_counter()
        eroded = hydraulic_erosion(base, erosion_cfg, terrain.seed)
        eroded = normalize_preserving_range(eroded, base)
        elapsed = time.perf_counter() - t0
        save_png16(args.output / f'{stem}_erosion_{name}.png', eroded)
        save_preview(args.output / f'{stem}_erosion_{name}_preview.png', eroded, terrain.water_level)
        report['runs'][name] = {
            'seconds': round(elapsed, 3),
            'erosion_config': asdict(erosion_cfg),
            'stats': stats(eroded, terrain.water_level),
        }

    with (args.output / f'{stem}_report.json').open('w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
