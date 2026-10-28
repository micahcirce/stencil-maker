#!/usr/bin/env python3
"""Generate synthetic test images with known stencil problems (no third-party art).

  islands.png  one-color: letters with counters (B, O, A, R, D), concentric rings,
               an eye, a square frame, and a line too thin to cut
  face.png     five-color flat illustration, blurred + noisy + JPEG-compressed to imitate
               an AI-generated raster (anti-aliased edges, compression blends)
"""
import io, os, sys
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

def bold_font(size):
    for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/Library/Fonts/Arial Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"):
        if os.path.exists(f):
            return ImageFont.truetype(f, size)
    return ImageFont.load_default(size=size)

def islands(path):
    im = Image.new("RGB", (1200, 800), "white")
    d = ImageDraw.Draw(im)
    d.text((40, 20), "BOO RAD", fill="black", font=bold_font(220))
    for r in (150, 110, 70):
        d.ellipse([200 - r, 560 - r, 200 + r, 560 + r], outline="black", width=22)
    d.ellipse([480, 450, 800, 670], fill="black")
    d.ellipse([570, 490, 710, 630], fill="white")
    d.ellipse([610, 530, 670, 590], fill="black")
    d.line([(880, 420), (1150, 780)], fill="black", width=3)
    d.rectangle([900, 420, 1150, 560], outline="black", width=18)
    im.save(path)

def face(path):
    W = H = 1024
    im = Image.new("RGB", (W, H), "#f4efe4")
    d = ImageDraw.Draw(im)
    d.ellipse([162, 162, 862, 862], fill="#e8b23a")
    d.ellipse([300, 330, 440, 470], fill="#ffffff"); d.ellipse([584, 330, 724, 470], fill="#ffffff")
    d.ellipse([345, 375, 405, 435], fill="#2a2a2a"); d.ellipse([629, 375, 689, 435], fill="#2a2a2a")
    d.ellipse([360, 385, 378, 403], fill="#ffffff")
    d.arc([320, 480, 704, 760], start=20, end=160, fill="#2a2a2a", width=28)
    d.ellipse([230, 560, 330, 640], fill="#d9573b"); d.ellipse([694, 560, 794, 640], fill="#d9573b")
    d.ellipse([162, 162, 862, 862], outline="#2a2a2a", width=20)
    im = im.filter(ImageFilter.GaussianBlur(1.2))
    a = np.asarray(im).astype(np.int16) + np.random.default_rng(1).normal(0, 6, (H, W, 3)).astype(np.int16)
    im = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
    b = io.BytesIO(); im.save(b, "JPEG", quality=55)
    Image.open(b).save(path)

if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "."
    os.makedirs(out, exist_ok=True)
    islands(os.path.join(out, "islands.png"))
    face(os.path.join(out, "face.png"))
    print("wrote", os.path.join(out, "islands.png"), os.path.join(out, "face.png"))
