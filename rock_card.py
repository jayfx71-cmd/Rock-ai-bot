"""ROCK AI signal card (dark glass look with the robot). BUY = green glows, SELL = red glows."""
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

A = Path(__file__).parent if (Path(__file__).parent / "robot.png").exists() else Path(__file__).parent / "assets"   # files can sit in the repo root or in assets/
W, H = 1536, 1024
THEMES = {
    "SELL": dict(main=(255, 59, 85), light=(255, 122, 140), dark=(120, 18, 38), shift=0),
    "BUY": dict(main=(34, 224, 122), light=(125, 255, 176), dark=(10, 100, 54), shift=85),
}
WHITE, GREY, SL_COL, TP_COL = (255, 255, 255), (178, 184, 194), (255, 91, 112), (95, 245, 208)


def font(name, size):
    return ImageFont.truetype(str(A / f"Inter-{name}.otf"), size)


def rgba(c, a=255):
    return (c[0], c[1], c[2], int(a))


def glow(base, draw_fn, color, radius, strength=1.0):
    """Draw shapes on a layer, blur it, and add it under/over the picture as a soft glow."""
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(layer), color)
    layer = layer.filter(ImageFilter.GaussianBlur(radius))
    if strength != 1.0:
        al = layer.getchannel("A").point(lambda v: min(255, int(v * strength)))
        layer.putalpha(al)
    base.alpha_composite(layer)


def spaced_width(text, f, sp):
    return sum(f.getlength(ch) for ch in text) + sp * (len(text) - 1)


def spaced(d, xy, text, f, fill, sp=0, anchor="l"):
    """Letter-spaced text. xy = (x, y centre). anchor l / m / r."""
    x, y = xy
    wdt = spaced_width(text, f, sp)
    x = x - (wdt / 2 if anchor == "m" else (wdt if anchor == "r" else 0))
    for ch in text:
        d.text((x, y), ch, font=f, fill=fill, anchor="lm")
        x += f.getlength(ch) + sp


def vgrad(size, top, bottom):
    w, h = size
    t = np.linspace(0, 1, h)[:, None, None]
    arr = np.array(top, np.float32) * (1 - t) + np.array(bottom, np.float32) * t
    return Image.fromarray(np.repeat(arr, w, axis=1).astype(np.uint8), "RGBA")


def rrect_mask(size, box, r):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle(box, r, fill=255)
    return m


def hue_shift(img, shift):
    """Turn only the RED parts of the robot green (glows, eyes, lights). Greys, whites and blues stay as they are."""
    if not shift:
        return img
    rgb, a = img.convert("RGB"), img.getchannel("A")
    hsv = np.array(rgb.convert("HSV")).astype(np.float32)
    deg = hsv[..., 0] * 360.0 / 255.0
    dist = np.minimum(deg, 360.0 - deg)                      # distance from pure red
    close = np.clip((75.0 - dist) / 40.0, 0, 1)
    sat = np.clip((hsv[..., 1] / 255.0 - 0.2) / 0.3, 0, 1)
    w = (close * sat)[..., None]
    moved = hsv.copy()
    moved[..., 0] = (moved[..., 0] + shift) % 256
    moved_rgb = np.array(Image.fromarray(moved.astype(np.uint8), "HSV").convert("RGB")).astype(np.float32)
    out = np.array(rgb).astype(np.float32) * (1 - w) + moved_rgb * w
    res = Image.fromarray(out.astype(np.uint8), "RGB")
    res.putalpha(a)
    return res


def render(tr, path="scalp.png"):
    th = THEMES[tr["side"]]
    main, light, dark = th["main"], th["light"], th["dark"]
    img = Image.new("RGBA", (W, H), (5, 6, 9, 255))

    # background: soft vignette, accent glow bottom corners, cool light top-left
    glow(img, lambda d, c: (d.ellipse((-260, 700, 420, 1250), fill=rgba(c, 70)), d.ellipse((1180, 760, 1800, 1260), fill=rgba(c, 62))), main, 90)
    glow(img, lambda d, c: d.polygon([(40, 0), (420, 0), (240, 330), (0, 330)], fill=(120, 150, 210, 60)), None, 60)

    # glass card
    card = (58, 76, 1480, 930)
    body = vgrad((W, H), (18, 21, 29, 235), (7, 8, 12, 245))
    img.paste(body, (0, 0), rrect_mask((W, H), card, 48))
    sheen = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sheen).polygon([(60, 90), (640, 80), (430, 520), (60, 520)], fill=(255, 255, 255, 14))
    sheen.putalpha(Image.composite(sheen.getchannel("A"), Image.new("L", (W, H), 0), rrect_mask((W, H), card, 48)))
    img.alpha_composite(sheen)
    glow(img, lambda d, c: d.rounded_rectangle(card, 48, outline=rgba(c, 150), width=5), main, 14, 0.45)
    ImageDraw.Draw(img).rounded_rectangle(card, 48, outline=(200, 210, 230, 95), width=2)

    # robot (hue-shifted to green for BUY) + our mark on the helmet
    robot = hue_shift(Image.open(A / "robot.png").convert("RGBA"), th["shift"])
    img.alpha_composite(robot, (835, 80))
    glow(img, lambda d, c: d.text((915, 249), "R", font=font("Black", 50), fill=rgba(c, 255), anchor="mm"), light, 7, 1.2)
    ImageDraw.Draw(img).text((915, 249), "R", font=font("Black", 46), fill=rgba(light), anchor="mm")

    d = ImageDraw.Draw(img)
    # header: logo + name
    glow(img, lambda dd, c: dd.text((155, 168), "R", font=font("Black", 92), fill=rgba(c), anchor="mm"), main, 9, 1.0)
    d = ImageDraw.Draw(img)
    d.text((155, 168), "R", font=font("Black", 88), fill=rgba(light), anchor="mm")
    d.line([(128, 214), (150, 200), (160, 207), (186, 183)], fill=rgba(WHITE, 230), width=4, joint="curve")
    spaced(d, (228, 156), "ROCK AI", font("SemiBold", 38), WHITE, 6)
    spaced(d, (230, 194), "AI TRADING SIGNALS", font("Regular", 18), GREY, 5)
    d.line([(128, 233), (830, 233)], fill=(44, 47, 55, 255), width=2)

    # top right
    num = f"#{tr['n']:02d}"
    f34 = font("Bold", 36)
    tail = " / 126"
    xr = 1427
    d.text((xr, 151), tail, font=f34, fill=rgba(GREY), anchor="rm")
    d.text((xr - f34.getlength(tail), 151), num, font=f34, fill=rgba(light), anchor="rm")
    spaced(d, (xr, 195), f"{tr['session']} SESSION", font("Medium", 21), WHITE, 1.5, "r")
    sx = xr - spaced_width(f"{tr['session']} SESSION", font("Medium", 21), 1.5) - 28
    glow(img, lambda dd, c: dd.ellipse((sx - 8, 187, sx + 8, 203), fill=rgba(c)), light, 6, 1.0)
    d = ImageDraw.Draw(img)
    d.ellipse((sx - 7, 188, sx + 7, 202), fill=rgba(WHITE))
    d.line([(1215, 233), (1430, 233)], fill=(44, 47, 55, 255), width=2)

    # BUY / SELL button
    bx = (118, 287, 795, 458)
    btn = vgrad((W, H), rgba(main, 105), rgba(dark, 135))
    glow(img, lambda dd, c: dd.rounded_rectangle(bx, 30, outline=rgba(c, 255), width=8), main, 16, 1.1)
    img.paste(btn, (0, 0), rrect_mask((W, H), bx, 30))
    hi = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(hi).rounded_rectangle((126, 292, 787, 350), 26, fill=(255, 255, 255, 14))
    img.alpha_composite(hi)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(bx, 30, outline=rgba(light, 235), width=3)
    tri = [(222, 333), (335, 333), (278, 418)] if tr["side"] == "SELL" else [(222, 418), (335, 418), (278, 333)]
    glow(img, lambda dd, c: dd.polygon(tri, fill=rgba(c)), light, 12, 1.2)
    d = ImageDraw.Draw(img)
    d.polygon(tri, fill=rgba(light))
    word = tr["side"]
    glow(img, lambda dd, c: dd.text((385, 376), word, font=font("Black", 128), fill=rgba(c), anchor="lm"), light, 10, 1.1)
    d = ImageDraw.Draw(img)
    d.text((385, 376), word, font=font("Black", 128), fill=WHITE, anchor="lm")

    # instrument + ratio
    d.text((128, 517), "XAU/USD", font=font("Bold", 38), fill=WHITE, anchor="lm")
    d.ellipse((322, 515, 330, 523), fill=rgba(GREY))
    spaced(d, (362, 518), "GOLD SCALP", font("Medium", 25), (214, 218, 226), 1.5)
    pill = (575, 487, 822, 553)
    glow(img, lambda dd, c: dd.rounded_rectangle(pill, 33, outline=rgba(c), width=5), main, 10, 1.0)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(pill, 33, fill=rgba(dark, 70), outline=rgba(main), width=3)
    d.text(((pill[0] + pill[2]) / 2, 521), f"RR 1:{tr['ratio']:g}", font=font("Bold", 33), fill=rgba(light), anchor="mm")

    # three cards
    risk = tr["risk"]
    spec = [((98, 605, 528, 832), "ENTRY", f"{tr['entry']:.2f}", WHITE, None, (200, 206, 220), "cross"),
            ((558, 605, 985, 832), "STOP LOSS", f"{tr['sl']:.2f}", SL_COL, f"-${risk:.2f}", SL_COL, "shield"),
            ((1013, 605, 1437, 832), "TAKE PROFIT", f"{tr['tp']:.2f}", TP_COL, f"+${risk * tr['ratio']:.2f}", TP_COL, "target")]
    for box, label, value, vcol, sub, ecol, icon in spec:
        glow(img, lambda dd, c: dd.rounded_rectangle(box, 30, outline=rgba(c, 255), width=5), ecol if icon != "cross" else main, 14, 0.8 if icon != "cross" else 0.55)
        cb = vgrad((W, H), (16, 19, 26, 238), (6, 7, 10, 245))
        img.paste(cb, (0, 0), rrect_mask((W, H), box, 30))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle(box, 30, outline=rgba(ecol, 190 if icon != "cross" else 110), width=2)
        x0, y0 = box[0], box[1]
        spaced(d, (x0 + 50, y0 + 50), label, font("Medium", 25), (214, 218, 226), 3)
        ix, iy = box[2] - 48, y0 + 50
        ic = rgba(ecol, 235)
        if icon == "cross":
            d.ellipse((ix - 14, iy - 14, ix + 14, iy + 14), outline=ic, width=2)
            d.line([(ix - 22, iy), (ix + 22, iy)], fill=ic, width=2); d.line([(ix, iy - 22), (ix, iy + 22)], fill=ic, width=2)
        elif icon == "shield":
            d.polygon([(ix, iy - 20), (ix + 17, iy - 12), (ix + 15, iy + 8), (ix, iy + 20), (ix - 15, iy + 8), (ix - 17, iy - 12)], outline=ic, width=2)
            d.line([(ix - 6, iy + 2), (ix + 8, iy - 8)], fill=ic, width=2)
        else:
            for rr in (18, 10):
                d.ellipse((ix - rr, iy - rr, ix + rr, iy + rr), outline=ic, width=2)
            d.ellipse((ix - 3, iy - 3, ix + 3, iy + 3), fill=ic)
            d.line([(ix, iy), (ix + 20, iy - 20)], fill=ic, width=2)
        d.text((x0 + 50, y0 + 118), value, font=font("SemiBold", 58), fill=rgba(vcol), anchor="lm")
        if sub:
            d.text((x0 + 46, y0 + 183), sub, font=font("Medium", 31), fill=rgba(ecol), anchor="lm")
        else:
            d.rounded_rectangle((x0 + 45, y0 + 155, x0 + 267, y0 + 203), 22, fill=(32, 35, 43, 255))
            spaced(d, (x0 + 156, y0 + 179), "MARKET PRICE", font("Medium", 22), (205, 210, 220), 3, "m")

    # footer
    fy = 883
    for x0, x1 in ((135, 405), (1135, 1405)):
        glow(img, lambda dd, c: dd.line([(x0, fy), (x1, fy)], fill=rgba(c), width=3), main, 4, 1.0)
        ImageDraw.Draw(img).line([(x0, fy), (x1, fy)], fill=rgba(main), width=2)
    d = ImageDraw.Draw(img)
    for k in range(3):
        d.line([(430 + k * 14, fy + 8), (444 + k * 14, fy - 8)], fill=rgba(main), width=3)
        d.line([(1070 + k * 14, fy - 8), (1084 + k * 14, fy + 8)], fill=rgba(main), width=3)
    spaced(d, (768, fy), "EXECUTE FAST   •   TRADE SMARTER", font("Regular", 21), (225, 228, 235), 5, "m")

    img.convert("RGB").save(path, quality=92)
    return path


if __name__ == "__main__":
    import sys
    base = dict(n=7, session="EVENING", side="SELL", entry=3983.59, sl=3985.77, tp=3977.05, ratio=3.0, risk=2.18)
    render(base, "sell.png")
    render(dict(base, side="BUY", sl=3981.41, tp=3990.13, session="MORNING"), "buy.png")
