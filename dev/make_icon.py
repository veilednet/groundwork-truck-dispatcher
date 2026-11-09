"""Draws media/icon.png and media/icon.ico: dark rounded square, amber bar, white "TD" (veilednet style)."""
import os
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "media")
os.makedirs(OUT, exist_ok=True)
S = 512
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((16, 16, S - 16, S - 16), radius=96, fill="#15171c", outline="#2e333e", width=8)
d.rounded_rectangle((64, 372, S - 64, 404), radius=16, fill="#f2a93b")
font = None
for name in ("bahnschrift.ttf", "segoeuib.ttf", "arialbd.ttf"):
    try:
        font = ImageFont.truetype(os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", name), 250)
        if name == "bahnschrift.ttf":
            font.set_variation_by_name("Bold")
        break
    except Exception:
        continue
d.text((S / 2, 222), "TD", font=font, fill="#eceef2", anchor="mm")
img.save(os.path.join(OUT, "icon.png"))
img.save(os.path.join(OUT, "icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(OUT)
