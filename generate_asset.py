#!/usr/bin/env python3
"""
generate_asset.py
-----------------
Standalone OpenRouter image generation script that writes a cropped, alpha-ready
asset to disk along with its final dimensions. Designed for quick reuse in new
projects and agents.

What it does
1) Sends a text prompt to OpenRouter image models via chat/completions.
2) Extracts the image from the response (data URL / base64).
3) Preserves native alpha if present; otherwise applies chroma-key removal.
4) Crops to the first non-empty pixel bounds (no padding/margins).
5) Downscales the cropped image only if it exceeds the max bounding box.
6) Writes the PNG to disk (required).

Usage
  python generate_asset.py --width 256 --height 128 --description "a spaceinvader seen from above" --output assets/ship.png

Notes
- Width/height are used as a MAX bounding box only (not exact output size).
- Output image always has alpha (native or chroma-key derived).
- Requires OPENROUTER_API_KEY in env (or .env in current directory).
"""

from __future__ import annotations

import argparse
import base64
import colorsys
import json
import os
import re
import sys
from io import BytesIO
from typing import Any, Dict, Optional

from openai import OpenAI
from PIL import Image, ImageOps


DATA_URL_RE = re.compile(r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+")


def _load_env_var(key: str, env_path: str = ".env") -> Optional[str]:
    if os.environ.get(key):
        return os.environ[key]
    if not os.path.exists(env_path):
        return None
    with open(env_path, "r") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            value = value.strip().strip('"').strip("'")
            if name == key:
                os.environ[key] = value
                return value
    return None


def _model_dump(obj: Any) -> Any:
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    return obj


def _find_first_data_url(obj: Any) -> Optional[str]:
    if isinstance(obj, str):
        match = DATA_URL_RE.search(obj)
        if match:
            return match.group(0)
        return None
    if isinstance(obj, dict):
        for value in obj.values():
            found = _find_first_data_url(value)
            if found:
                return found
    if isinstance(obj, list):
        for item in obj:
            found = _find_first_data_url(item)
            if found:
                return found
    return None


def _find_first_b64_json(obj: Any) -> Optional[str]:
    if isinstance(obj, dict):
        if "b64_json" in obj and isinstance(obj["b64_json"], str):
            return obj["b64_json"]
        for value in obj.values():
            found = _find_first_b64_json(value)
            if found:
                return found
    if isinstance(obj, list):
        for item in obj:
            found = _find_first_b64_json(item)
            if found:
                return found
    return None


def _extract_image_payload(response: Any) -> Dict[str, str]:
    data = _model_dump(response)
    if isinstance(data, dict):
        image_data = data.get("data")
        if isinstance(image_data, list) and image_data:
            first = image_data[0]
            if isinstance(first, dict):
                if isinstance(first.get("b64_json"), str):
                    return {"kind": "b64_json", "data": first["b64_json"]}
                if isinstance(first.get("url"), str):
                    raise ValueError("URL responses are not supported in this script.")

    data_url = _find_first_data_url(data)
    if data_url:
        return {"kind": "data_url", "data": data_url}

    b64_json = _find_first_b64_json(data)
    if b64_json:
        return {"kind": "b64_json", "data": b64_json}

    raise ValueError("No base64 image payload found in response.")


def _decode_payload(payload: Dict[str, str]) -> bytes:
    kind = payload["kind"]
    data = payload["data"]
    if kind == "data_url":
        _, b64_data = data.split(",", 1)
        return base64.b64decode(b64_data)
    if kind == "b64_json":
        return base64.b64decode(data)
    raise ValueError(f"Unsupported payload kind: {kind}")


def _sample_border_colors(img: Image.Image) -> list[tuple[int, int, int]]:
    rgb = img.convert("RGB")
    width, height = rgb.size
    step = max(1, min(width, height) // 20)
    samples = []
    for x in range(0, width, step):
        samples.append(rgb.getpixel((x, 0)))
        samples.append(rgb.getpixel((x, height - 1)))
    for y in range(0, height, step):
        samples.append(rgb.getpixel((0, y)))
        samples.append(rgb.getpixel((width - 1, y)))
    return samples


def _median_color(samples: list[tuple[int, int, int]]) -> tuple[int, int, int]:
    if not samples:
        return (0, 255, 0)
    return (
        int(sorted(c[0] for c in samples)[len(samples) // 2]),
        int(sorted(c[1] for c in samples)[len(samples) // 2]),
        int(sorted(c[2] for c in samples)[len(samples) // 2]),
    )


def _is_greenish(color: tuple[int, int, int], min_delta: int = 25) -> bool:
    r, g, b = color
    return g >= r + min_delta and g >= b + min_delta


def _pick_key_color(img: Image.Image) -> tuple[int, int, int]:
    candidate = _median_color(_sample_border_colors(img))
    return candidate if _is_greenish(candidate) else (0, 255, 0)


def _hue_distance(a: float, b: float) -> float:
    diff = abs(a - b)
    return min(diff, 360 - diff)


def _apply_chroma_key(img: Image.Image, key_color: tuple[int, int, int]) -> Image.Image:
    rgb = img.convert("RGB")
    key_h, key_s, key_v = colorsys.rgb_to_hsv(
        key_color[0] / 255.0, key_color[1] / 255.0, key_color[2] / 255.0
    )
    key_hue = key_h * 360.0

    hue_cut = 35.0
    hue_soft = 55.0
    sat_cut = 0.35
    val_cut = 0.2
    despill_cut = 70.0

    pixels = list(rgb.getdata())
    keyed = []
    for r, g, b in pixels:
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        hue = h * 360.0
        hue_diff = _hue_distance(hue, key_hue)

        alpha = 255
        if hue_diff <= hue_cut and s >= sat_cut and v >= val_cut:
            alpha = 0
        elif hue_diff <= hue_soft and s >= sat_cut * 0.6 and v >= val_cut:
            alpha = int(255 * (hue_diff - hue_cut) / (hue_soft - hue_cut))
            alpha = max(0, min(255, alpha))

        if alpha > 0 and hue_diff <= despill_cut and g > r and g > b:
            excess = g - max(r, b)
            g = int(max(0, g - excess * 0.5))

        keyed.append((r, g, b, alpha))

    rgba = Image.new("RGBA", rgb.size)
    rgba.putdata(keyed)
    return rgba


def generate_and_process(model: str, description: str, max_size: tuple[int, int]) -> Dict[str, Any]:
    api_key = _load_env_var("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not found in environment or .env.")

    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
    )

    prompt = (
        f"{description.strip()}, single subject, centered, "
        "solid chroma green background (#00FF00), no transparency, "
        "crisp edges, no text, no UI, no drop shadows."
    )

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        extra_body={"modalities": ["image", "text"]},
    )

    payload = _extract_image_payload(response)
    image_bytes = _decode_payload(payload)
    img = Image.open(BytesIO(image_bytes))

    mode = img.mode
    has_alpha = mode in ("RGBA", "LA") or (mode == "P" and "transparency" in img.info)
    working = img.convert("RGBA") if mode != "RGBA" else img.copy()
    if has_alpha:
        alpha = working.split()[3]
        min_a, max_a = alpha.getextrema()
        if not (min_a < 255 and max_a > 0):
            has_alpha = False

    if not has_alpha:
        key_color = _pick_key_color(img)
        working = _apply_chroma_key(img, key_color)

    alpha = working.split()[3]
    mask = alpha.point(lambda a: 255 if a > 5 else 0)
    bbox = mask.getbbox()
    if bbox:
        working = working.crop(bbox)

    if working.size[0] > max_size[0] or working.size[1] > max_size[1]:
        working = ImageOps.contain(working, max_size, method=Image.Resampling.LANCZOS)

    out_buffer = BytesIO()
    working.save(out_buffer, format="PNG")
    out_bytes = out_buffer.getvalue()
    width, height = working.size
    return {
        "width": width,
        "height": height,
        "resolution": f"{width}x{height}",
        "image_bytes": out_bytes,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a cropped, alpha-ready asset via OpenRouter.")
    parser.add_argument("--width", type=int, required=True, help="Max bounding box width (<=1024)")
    parser.add_argument("--height", type=int, required=True, help="Max bounding box height (<=1024)")
    parser.add_argument("--description", type=str, required=True, help="Description of the asset to be generated")
    parser.add_argument("--model", type=str, default="google/gemini-2.5-flash-image", help="OpenRouter model slug")
    parser.add_argument(
        "--output",
        type=str,
        required=True,
        help="PNG output path (required).",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.width <= 0 or args.height <= 0:
        raise ValueError("Width and height must be positive integers.")
    if args.width > 1024 or args.height > 1024:
        raise ValueError("Width and height must be <= 1024.")

    result = generate_and_process(args.model, args.description, (args.width, args.height))
    output_path = args.output
    with open(output_path, "wb") as f:
        f.write(result["image_bytes"])

    print(
        json.dumps(
            {
                "path": output_path,
                "width": result["width"],
                "height": result["height"],
                "resolution": result["resolution"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
