#!/usr/bin/env python3
"""Derive transparent desktop-pet assets from the authoritative source image.

The source image (``assets/pet.jpg``) is never modified.  The derived PNGs live
under ``desktop_pet/assets/`` and are regenerated deterministically by this
script.

The source is a JPEG with a light studio background.  We build a foreground
mask by flood-filling background-like pixels from the image border, then feather
the mask by one pixel.  The character's enclosed white eyes/highlights are not
connected to the border, so they are preserved.
"""
from __future__ import annotations

import argparse
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "assets" / "pet.jpg"
OUT_DIR = Path(__file__).resolve().parent / "assets"

# Framing crop around the character and its detached droplets.  This removes
# the studio floor and the unrelated background blob without touching the
# character pixels.  Coordinates are in the 2752x1536 source image.
CHARACTER_CROP = (850, 40, 1980, 1300)


def _background_like(rgb: np.ndarray) -> np.ndarray:
    """Return a boolean mask of pixels that look like the studio background.

    The background is a light neutral gradient.  We sample the border of the
    framing crop and classify a pixel as background when it is close to any
    border sample, plus a low-saturation/high-brightness rule.  The character
    is a saturated blue and is far from those samples.
    """
    arr = rgb.astype(np.float32) / 255.0
    h, w = arr.shape[:2]
    border = np.concatenate([
        arr[0, :, :], arr[h - 1, :, :], arr[:, 0, :], arr[:, w - 1, :],
    ], axis=0)
    # Keep a compact, deterministic sample set.
    step = max(1, len(border) // 64)
    samples = border[::step][:64]
    dist = np.sqrt(((arr[:, :, None, :] - samples[None, None, :, :]) ** 2).sum(axis=3))
    close_to_border = dist.min(axis=2) < 0.075

    mx = arr.max(axis=2)
    mn = arr.min(axis=2)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    neutral = (sat < 0.10) & (mx > 0.78)

    # The character has a few detached pale-blue droplets under its body.
    # They are close to the background colour, so protect blue-ish pixels in
    # that local band.  The connected-component pass later drops the floor,
    # which is also blue-ish but touches the bottom edge.
    yy, xx = np.mgrid[0:h, 0:w]
    droplet_band = ((yy > h * 0.82) & (yy < h * 0.99)
                    & (xx > w * 0.10) & (xx < w * 0.50))
    blueish = (arr[:, :, 2] - arr[:, :, 0] > 0.035) & (sat > 0.10) & (mx > 0.55)
    background = close_to_border | neutral
    background &= ~(droplet_band & blueish)
    return background


def build_mask(image: Image.Image) -> Image.Image:
    rgb = np.asarray(image.convert("RGB"))
    bg = _background_like(rgb)
    # 255 = candidate foreground, 0 = candidate background.
    mask = np.where(bg, 0, 255).astype(np.uint8)
    mask_img = Image.fromarray(mask, mode="L")

    # Flood fill the connected background from all four corners.  A separate
    # fill per corner handles the floor/wall boundary and any vignette.
    fill = mask_img.copy()
    w, h = fill.size
    seeds = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
    for seed in seeds:
        if fill.getpixel(seed) == 0:
            ImageDraw.floodfill(fill, seed, 128, thresh=8)
    # Any 0 pixels connected to a border seed are now 128.  Foreground is
    # everything that is not 128; keep enclosed background-like holes (eyes).
    arr = np.asarray(fill)
    fg = np.where(arr == 128, 0, 255).astype(np.uint8)

    # Remove tiny isolated specks outside the character and the studio floor.
    # The character is the largest connected component; the detached droplets
    # are smaller components above the bottom edge.  Label at quarter scale so
    # the operation is fast even on the 2752px source.
    alpha = Image.fromarray(fg, mode="L")
    alpha = alpha.filter(ImageFilter.MedianFilter(3))
    keep = _keep_character_components(alpha)
    alpha = Image.composite(alpha, Image.new("L", alpha.size, 0), keep)
    alpha = alpha.filter(ImageFilter.GaussianBlur(0.8))
    return alpha


def _decontaminate(rgba: Image.Image) -> Image.Image:
    """Remove the light studio background from anti-aliased edge pixels.

    The source is a JPEG composite.  Feathering the mask alone leaves a pale
    halo on dark desktops.  For each partially transparent pixel we invert the
    alpha composite against the sampled background colour, which recovers the
    foreground colour instead of blending toward white.
    """
    arr = np.asarray(rgba).astype(np.float32)
    rgb = arr[:, :, :3]
    a = (arr[:, :, 3:4] / 255.0).clip(0, 1)
    border = np.concatenate([
        rgb[0, :, :], rgb[-1, :, :], rgb[:, 0, :], rgb[:, -1, :],
    ], axis=0)
    bg = np.median(border, axis=0)
    fg = (rgb - (1.0 - a) * bg) / np.maximum(a, 0.08)
    fg = np.clip(fg, 0, 255)
    fg[a[:, :, 0] < 0.02] = 0
    out = np.dstack([fg, arr[:, :, 3]]).astype(np.uint8)
    return Image.fromarray(out, mode="RGBA")


def _keep_character_components(alpha: Image.Image) -> Image.Image:
    """Keep the main character plus nearby droplet components, drop the floor."""
    scale = 4
    small = alpha.resize((max(1, alpha.width // scale), max(1, alpha.height // scale)),
                         Image.NEAREST)
    arr = np.asarray(small) > 127
    h, w = arr.shape
    labels = np.zeros((h, w), dtype=np.int32)
    components: list[dict] = []
    current = 0
    for y in range(h):
        for x in range(w):
            if not arr[y, x] or labels[y, x]:
                continue
            current += 1
            q = deque([(x, y)])
            labels[y, x] = current
            pts = []
            while q:
                cx, cy = q.popleft()
                pts.append((cx, cy))
                for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                    if 0 <= nx < w and 0 <= ny < h and arr[ny, nx] and not labels[ny, nx]:
                        labels[ny, nx] = current
                        q.append((nx, ny))
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            components.append({
                "id": current,
                "area": len(pts),
                "cx": sum(xs) / len(xs),
                "cy": sum(ys) / len(ys),
                "x0": min(xs), "x1": max(xs),
                "y0": min(ys), "y1": max(ys),
            })
    if not components:
        return Image.new("L", alpha.size, 0)
    main = max(components, key=lambda c: c["area"])
    kept = {main["id"]}
    for c in components:
        if c is main:
            continue
        # Detached droplets: small, near the character, above the floor band.
        if c["area"] >= 2 and c["cy"] < h * 0.96:
            if abs(c["cx"] - main["cx"]) < w * 0.45 and abs(c["cy"] - main["cy"]) < h * 0.45:
                kept.add(c["id"])
    mask = np.isin(labels, list(kept)).astype(np.uint8) * 255
    keep_img = Image.fromarray(mask, mode="L").resize(alpha.size, Image.NEAREST)
    keep_img = keep_img.filter(ImageFilter.MaxFilter(3))
    return keep_img


def derive(source: Path = SOURCE, out_dir: Path = OUT_DIR) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    source_image = Image.open(source).convert("RGB")
    image = source_image.crop(CHARACTER_CROP)
    alpha = build_mask(image)
    rgba = image.convert("RGBA")
    rgba.putalpha(alpha)
    rgba = _decontaminate(rgba)

    bbox = rgba.getbbox()
    if bbox is None:
        raise RuntimeError("foreground mask is empty")
    # Add a small transparent margin so effects can orbit the character.
    pad = 24
    left, top, right, bottom = bbox
    left = max(0, left - pad)
    top = max(0, top - pad)
    right = min(rgba.width, right + pad)
    bottom = min(rgba.height, bottom + pad)
    cropped = rgba.crop((left, top, right, bottom))

    # Keep a high-resolution master and a display-sized asset.  Both preserve
    # the original pixels; only the alpha channel is added.
    master = out_dir / "pet_master.png"
    display = out_dir / "pet_idle.png"
    icon = out_dir / "pet_icon.png"
    cropped.save(master, "PNG", optimize=True)

    target_h = 320
    scale = target_h / cropped.height
    target_w = max(1, round(cropped.width * scale))
    resized = cropped.resize((target_w, target_h), Image.LANCZOS)
    resized.save(display, "PNG", optimize=True)

    icon_img = cropped.copy()
    icon_img.thumbnail((96, 96), Image.LANCZOS)
    icon_img.save(icon, "PNG", optimize=True)

    return {
        "source": str(source),
        "master": str(master),
        "display": str(display),
        "icon": str(icon),
        "source_size": f"{source_image.width}x{source_image.height}",
        "crop_size": f"{image.width}x{image.height}",
        "master_size": f"{cropped.width}x{cropped.height}",
        "display_size": f"{resized.width}x{resized.height}",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args()
    result = derive(args.source, args.out)
    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
