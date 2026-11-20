"""Draws the integration's brand icons (padlock + Bluetooth rune) into custom_components/.../brand/."""
from pathlib import Path
from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "custom_components" / "moes_ble_lock" / "brand"
S = 1024  # draw large, downscale for smooth edges


def draw(body: str, rune: str) -> Image.Image:
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    # shackle
    w = 92
    d.rounded_rectangle((262, 90, 762, 640), radius=250, outline=body, width=w)
    # body
    d.rounded_rectangle((150, 430, 874, 960), radius=110, fill=body)
    # Bluetooth rune, centred in the body
    cx, cy, h = 512, 695, 380
    t, b = cy - h / 2, cy + h / 2
    q = h / 4
    r = 95  # how far the arrow tips reach right
    pts = [(cx - r, cy - q), (cx + r, cy + q), (cx, b), (cx, t), (cx + r, cy - q), (cx - r, cy + q)]
    d.line(pts, fill=rune, width=48, joint="curve")
    for x, y in (pts[0], pts[-1]):
        d.ellipse((x - 24, y - 24, x + 24, y + 24), fill=rune)
    return im


OUT.mkdir(parents=True, exist_ok=True)
for name, body, rune in (("", "#1E88E5", "#FFFFFF"), ("dark_", "#64B5F6", "#0D2A45")):
    big = draw(body, rune)
    big.resize((256, 256), Image.LANCZOS).save(OUT / f"{name}icon.png", optimize=True)
    big.resize((512, 512), Image.LANCZOS).save(OUT / f"{name}icon@2x.png", optimize=True)
print("wrote", sorted(p.name for p in OUT.iterdir()))
