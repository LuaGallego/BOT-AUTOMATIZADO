from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Optional

import discord
from PIL import Image, ImageDraw, ImageFont


@dataclass
class WelcomeCardStyle:
    width: int = 1200
    height: int = 500
    bg_top: tuple[int, int, int] = (18, 18, 22)
    bg_bottom: tuple[int, int, int] = (8, 8, 10)
    accent: tuple[int, int, int] = (255, 200, 0)     # amarelo
    text: tuple[int, int, int] = (245, 245, 245)
    subtext: tuple[int, int, int] = (205, 205, 205)


def _gradient_background(w: int, h: int, top_rgb, bottom_rgb) -> Image.Image:
    img = Image.new("RGB", (w, h), top_rgb)
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / (h - 1) if h > 1 else 1
        r = int(top_rgb[0] * (1 - t) + bottom_rgb[0] * t)
        g = int(top_rgb[1] * (1 - t) + bottom_rgb[1] * t)
        b = int(top_rgb[2] * (1 - t) + bottom_rgb[2] * t)
        draw.line([(0, y), (w, y)], fill=(r, g, b))
    return img.convert("RGBA")


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = []
    if bold:
        candidates += [
            r"C:\Windows\Fonts\arialbd.ttf",
            r"C:\Windows\Fonts\bahnschrift.ttf",
        ]
    candidates += [
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\segoeui.ttf",
    ]

    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            pass
    return ImageFont.load_default()


def _circle_crop(im: Image.Image, size: int) -> Image.Image:
    im = im.convert("RGBA").resize((size, size))
    mask = Image.new("L", (size, size), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.ellipse((0, 0, size - 1, size - 1), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    return out


async def make_welcome_card(
    member: discord.Member,
    subtitle_name: str,
    survivor_line: str,
    style: Optional[WelcomeCardStyle] = None,
) -> io.BytesIO:
    style = style or WelcomeCardStyle()
    W, H = style.width, style.height

    # Fundo (gradiente)
    img = _gradient_background(W, H, style.bg_top, style.bg_bottom)
    draw = ImageDraw.Draw(img)

    # Avatar
    avatar_asset = member.display_avatar.replace(size=256, static_format="png")
    avatar_bytes = await avatar_asset.read()
    avatar_img = Image.open(io.BytesIO(avatar_bytes))
    avatar = _circle_crop(avatar_img, 210)

    # Anel de destaque
    ring_size = 230
    ring = Image.new("RGBA", (ring_size, ring_size), (0, 0, 0, 0))
    rdraw = ImageDraw.Draw(ring)
    rdraw.ellipse((0, 0, ring_size - 1, ring_size - 1), fill=(*style.accent, 255))
    rdraw.ellipse((10, 10, ring_size - 11, ring_size - 11), fill=(0, 0, 0, 0))

    # Posicionamento (centro superior)
    cx = W // 2
    img.paste(ring, (cx - ring_size // 2, 55), ring)
    img.paste(avatar, (cx - 105, 65), avatar)

    # Fontes
    font_title = _load_font(72, bold=True)
    font_name = _load_font(36, bold=True)
    font_small = _load_font(22, bold=False)

    # Textos (centralizados)
    title = "BEM-VINDO(A)"
    tw = draw.textlength(title, font=font_title)
    draw.text((cx - tw / 2, 290), title, font=font_title, fill=style.text)

    name = subtitle_name.upper()
    nw = draw.textlength(name, font=font_name)
    draw.text((cx - nw / 2, 365), name, font=font_name, fill=style.accent)

    sw = draw.textlength(survivor_line, font=font_small)
    draw.text((cx - sw / 2, 420), survivor_line, font=font_small, fill=style.subtext)

    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG", optimize=True)
    out.seek(0)
    return out