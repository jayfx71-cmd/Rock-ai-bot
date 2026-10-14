"""ROCK AI day recap card: shows only the winning trades of the day (real ones, nothing invented)."""
import numpy as np
from PIL import Image, ImageDraw
import rock_card as rc
from rock_card import font, glow, spaced, spaced_width, vgrad, rrect_mask, rgba, hue_shift, WHITE, GREY, W, H, A


def render_recap(day_label, wins, path="recap.png", be_count=0):
    th = rc.THEMES["BUY"]
    main, light, dark = th["main"], th["light"], th["dark"]
    img = Image.new("RGBA", (W, H), (5, 6, 9, 255))
    glow(img, lambda d, c: (d.ellipse((-260, 700, 420, 1250), fill=rgba(c, 70)), d.ellipse((1180, 760, 1800, 1260), fill=rgba(c, 62))), main, 90)
    glow(img, lambda d, c: d.polygon([(40, 0), (420, 0), (240, 330), (0, 330)], fill=(120, 150, 210, 60)), None, 60)
    card = (58, 76, 1480, 930)
    img.paste(vgrad((W, H), (18, 21, 29, 235), (7, 8, 12, 245)), (0, 0), rrect_mask((W, H), card, 48))
    glow(img, lambda d, c: d.rounded_rectangle(card, 48, outline=rgba(c, 150), width=5), main, 14, 0.45)
    ImageDraw.Draw(img).rounded_rectangle(card, 48, outline=(200, 210, 230, 95), width=2)
    robot = hue_shift(Image.open(A / "robot.png").convert("RGBA"), th["shift"]).crop((0, 0, 631, 515))
    al = np.array(robot.getchannel("A")).astype(np.float32)
    al *= np.clip((515 - np.arange(515)) / 70.0, 0, 1)[:, None]          # fade the bottom so the list sits clean below
    robot.putalpha(Image.fromarray(al.astype(np.uint8), "L"))
    img.alpha_composite(robot, (835, 80))
    glow(img, lambda d, c: d.text((915, 249), "R", font=font("Black", 50), fill=rgba(c), anchor="mm"), light, 7, 1.2)
    ImageDraw.Draw(img).text((915, 249), "R", font=font("Black", 46), fill=rgba(light), anchor="mm")
    glow(img, lambda dd, c: dd.text((155, 168), "R", font=font("Black", 92), fill=rgba(c), anchor="mm"), main, 9, 1.0)
    d = ImageDraw.Draw(img)
    d.text((155, 168), "R", font=font("Black", 88), fill=rgba(light), anchor="mm")
    d.line([(128, 214), (150, 200), (160, 207), (186, 183)], fill=rgba(WHITE, 230), width=4, joint="curve")
    spaced(d, (228, 156), "ROCK AI", font("SemiBold", 38), WHITE, 6)
    spaced(d, (230, 194), "AI TRADING SIGNALS", font("Regular", 18), GREY, 5)
    d.line([(128, 233), (830, 233)], fill=(44, 47, 55, 255), width=2)
    spaced(d, (1427, 151), "DAY RECAP", font("Bold", 34), rgba(light), 4, "r")
    spaced(d, (1427, 195), day_label.upper(), font("Medium", 21), WHITE, 1.5, "r")

    # big number tile
    bx = (118, 287, 795, 458)
    glow(img, lambda dd, c: dd.rounded_rectangle(bx, 30, outline=rgba(c), width=8), main, 16, 1.1)
    img.paste(vgrad((W, H), rgba(main, 105), rgba(dark, 135)), (0, 0), rrect_mask((W, H), bx, 30))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(bx, 30, outline=rgba(light, 235), width=3)
    n = len(wins)
    glow(img, lambda dd, c: dd.text((190, 372), str(n), font=font("Black", 130), fill=rgba(c), anchor="lm"), light, 10, 1.1)
    d = ImageDraw.Draw(img)
    d.text((190, 372), str(n), font=font("Black", 130), fill=WHITE, anchor="lm")
    numw = font("Black", 130).getlength(str(n))
    d.text((190 + numw + 28, 345), "WINNING", font=font("Bold", 38), fill=WHITE, anchor="lm")
    d.text((190 + numw + 28, 392), "TRADES TODAY", font=font("Medium", 28), fill=(214, 218, 226), anchor="lm")
    d.text((128, 517), "XAU/USD", font=font("Bold", 38), fill=WHITE, anchor="lm")
    d.ellipse((322, 515, 330, 523), fill=rgba(GREY))
    spaced(d, (362, 518), "GOLD SCALPS", font("Medium", 25), (214, 218, 226), 1.5)

    if be_count:
        d.text((128, 556), f"+ {be_count} trade{'s' if be_count != 1 else ''} protected at break-even (partial wins)", font=font("Medium", 21), fill=GREY, anchor="lm")
    # list of winners (up to 12, two columns)
    shown = wins[:12]
    ly0 = 590
    for k, t in enumerate(shown):
        col, row = divmod(k, 6)
        x = 98 + col * 470
        y = ly0 + row * 42
        d.rounded_rectangle((x, y, x + 440, y + 36), 13, fill=(14, 17, 22, 255), outline=(40, 44, 52, 255), width=1)
        d.ellipse((x + 16, y + 12, x + 28, y + 24), fill=rgba(main))
        txt = f"#{t['n']:02d}   {t['side']}   {t['session'].title()}   1:{t['ratio']:g}"
        d.text((x + 44, y + 19), txt, font=font("Medium", 21), fill=WHITE, anchor="lm")
        d.text((x + 424, y + 19), "WIN", font=font("Bold", 20), fill=rgba(light), anchor="rm")
    if len(wins) > 12:
        d.text((100, 852), f"+ {len(wins) - 12} more winning trades", font=font("Medium", 20), fill=GREY, anchor="lm")
    if not wins:
        d.text((98, 640), "No winning trades today.", font=font("Medium", 28), fill=GREY, anchor="lm")
    fy = 883
    for x0, x1 in ((135, 405), (1135, 1405)):
        glow(img, lambda dd, c: dd.line([(x0, fy), (x1, fy)], fill=rgba(c), width=3), main, 4, 1.0)
        ImageDraw.Draw(img).line([(x0, fy), (x1, fy)], fill=rgba(main), width=2)
    d = ImageDraw.Draw(img)
    spaced(d, (768, fy), "EXECUTE FAST   •   TRADE SMARTER", font("Regular", 21), (225, 228, 235), 5, "m")
    img.convert("RGB").save(path, quality=92)
    return path


if __name__ == "__main__":
    ws = [dict(n=i, side="BUY" if i % 2 else "SELL", session=["MORNING", "AFTERNOON", "EVENING"][i % 3], ratio=[2.0, 3.0][i % 2]) for i in range(1, 15)]
    render_recap("Tue 13 Oct", ws, "recap_test.png")
