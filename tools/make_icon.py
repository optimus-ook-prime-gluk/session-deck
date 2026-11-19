"""Builds the Session Deck icon files from the Gemini-made artwork (offline, Pillow).

Source: gemini-logo-source.webp (2x2 sheet of concepts generated with Google Gemini);
the chosen design is the top-left tile (fanned session cards with a coral live dot).
Outputs: logo.png (256 px, transparent corners), logo-512.png, logo.ico (16-256 px).
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
SRC = HERE / "gemini-logo-source.webp"
OUT = HERE.parent / "assets"
TILE = (101, 101, 898, 898)      # measured edges of the top-left tile in the 2000x2000 sheet
ART_CENTER = (500, 492)          # centre of the cards artwork (bounds 186..814 x 212..772)
ART_SIDE = 690                   # crop side: cards fill ~91% of the icon (source tile padding was ~21%)
RADIUS = 0.2                     # corner radius as a fraction of the icon size


def tile_image(size=1024):
    im = Image.open(SRC).convert("RGBA")
    cx, cy = ART_CENTER
    h = ART_SIDE // 2
    im = im.crop((cx - h, cy - h, cx + h, cy + h)).resize((size, size), Image.LANCZOS)
    scale = 4
    mask = Image.new("L", (size * scale, size * scale), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size * scale - 1, size * scale - 1],
                                           radius=int(size * scale * RADIUS), fill=255)
    im.putalpha(mask.resize((size, size), Image.LANCZOS))
    return im


def glyph_image(size=1024):
    """The cards on their own (no navy tile), with a thin navy outline, filling the whole square.
    Used for taskbar-size frames, where a dark tile vanishes against a dark taskbar."""
    from PIL import ImageChops
    im = Image.open(SRC).convert("RGBA")
    x0, y0, x1, y1 = 186 - 30, 212 - 30, 814 + 30, 772 + 30      # artwork bounds + halo room
    im = im.crop((x0, y0, x1, y1))
    w, h = im.size
    navy = (21, 31, 50)
    rgb = im.convert("RGB")
    diff = ImageChops.difference(rgb, Image.new("RGB", (w, h), navy)).convert("L")
    fg = diff.point(lambda v: 255 if v > 16 else 0).filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MinFilter(3))
    halo = fg.filter(ImageFilter.MaxFilter(13)).filter(ImageFilter.MaxFilter(13))          # ~12px outline
    out = Image.new("RGBA", (w, h), navy + (0,))
    out.putalpha(halo)
    out = Image.composite(im, out, fg)
    out.putalpha(ImageChops.lighter(halo, fg))
    bbox = out.getbbox()
    out = out.crop(bbox)
    side = max(out.size)
    sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    sq.paste(out, ((side - out.width) // 2, (side - out.height) // 2))
    return sq.resize((size, size), Image.LANCZOS)


def bold_small(size=1024):
    """Hand-drawn, bolder take on the same design for taskbar sizes (<= 32 px):
    a visible (lighter navy) tile, a bigger front card, thicker coral fan outlines."""
    S = 2048                                   # draw big, downsample for smooth edges
    k = S / 1024
    TILE_C, EDGE_C = (36, 50, 82, 255), (66, 84, 124, 255)
    CORAL, WHITE, GREY, NAVY = (255, 122, 89, 255), (255, 255, 255, 255), (64, 70, 84, 255), (36, 50, 82, 255)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=EDGE_C)
    e = int(26 * k)
    d.rounded_rectangle([e, e, S - 1 - e, S - 1 - e], radius=int(S * 0.22) - e, fill=TILE_C)

    card = [int(282 * k), int(236 * k), int(742 * k), int(860 * k)]   # front card box
    pivot = (512 * k, 900 * k)
    rad = int(64 * k)

    def layer(draw_fn, angle):
        lay = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        draw_fn(ImageDraw.Draw(lay))
        return lay.rotate(angle, resample=Image.BICUBIC, center=pivot)

    for ang in (21, -21):    # coral outline fan cards (left, right)
        img.alpha_composite(layer(lambda dd: dd.rounded_rectangle(card, radius=rad, outline=CORAL, width=int(62 * k)), ang))
    img.alpha_composite(layer(lambda dd: dd.rounded_rectangle(card, radius=rad, fill=GREY), -8))
    d = ImageDraw.Draw(img)
    g = int(40 * k)          # navy gap around the front card
    d.rounded_rectangle([card[0] - g, card[1] - g, card[2] + g, card[3] + g], radius=rad + g, fill=NAVY)
    d.rounded_rectangle(card, radius=rad, fill=WHITE)
    d.rounded_rectangle([int(330 * k), int(668 * k), int(694 * k), int(812 * k)], radius=int(34 * k), fill=CORAL)
    d.ellipse([int(612 * k), int(300 * k), int(700 * k), int(388 * k)], fill=CORAL)
    # keep everything inside the tile
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=255)
    img.putalpha(ImageChops_multiply(img.getchannel("A"), mask))
    return img.resize((size, size), Image.LANCZOS)


def ImageChops_multiply(a, b):
    from PIL import ImageChops
    return ImageChops.multiply(a, b)


def small(img, sz):
    out = img.resize((sz, sz), Image.LANCZOS)
    if sz <= 48:   # keep the thin card outlines readable at taskbar sizes
        out = out.filter(ImageFilter.UnsharpMask(radius=0.8, percent=90, threshold=1))
    return out


if __name__ == "__main__":
    big = tile_image(1024)
    big.resize((512, 512), Image.LANCZOS).save(OUT / "logo-512.png")
    big.resize((256, 256), Image.LANCZOS).save(OUT / "logo.png")
    glyph = bold_small(1024)
    glyph.resize((256, 256), Image.LANCZOS).save(OUT / "logo-small.png")
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    GLYPH_MAX = 32        # frames up to this size (taskbar, title bar) use the bold small-size version

    def frames_for(tile_img, glyph_img):
        return [small(glyph_img if s <= GLYPH_MAX else tile_img, s) for s in sizes]

    def save_ico(frames, name):
        frames[-1].save(OUT / name, sizes=[(s, s) for s in sizes], append_images=frames[:-1])

    save_ico(frames_for(big, glyph), "logo.ico")

    def with_dot(img, col):
        b = img.copy()
        d = ImageDraw.Draw(b)
        r, cx, cy, ring = 200, 1024 - 210, 210, 48
        d.ellipse([cx - r - ring, cy - r - ring, cx + r + ring, cy + r + ring], fill=(255, 255, 255, 255))
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)
        return b

    # taskbar variants with an alert dot (amber = needs you, green = finished)
    for name, col in (("wait", (250, 178, 25, 255)), ("done", (12, 163, 12, 255))):
        save_ico(frames_for(with_dot(big, col), with_dot(glyph, col)), "logo-%s.ico" % name)
    strip = Image.new("RGBA", (sum(sizes[:7]) + 10 * 8 + 128, 140), (243, 241, 236, 255))
    x = 10
    for s in (16, 20, 24, 32, 48, 64, 128):
        strip.alpha_composite(small(big, s), (x, 6))
        x += s + 10
    dark = Image.new("RGBA", strip.size, (32, 32, 32, 255))
    x = 10
    for s in (16, 20, 24, 32, 48, 64, 128):
        dark.alpha_composite(small(big, s), (x, 6))
        x += s + 10
    both = Image.new("RGBA", (strip.width, strip.height * 2))
    both.paste(strip, (0, 0)); both.paste(dark, (0, strip.height))
    both.save(HERE / "logo-preview.png")   # local check only (git-ignored)
    print("ok")
