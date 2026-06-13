"""Generate friday.ico (multi-size) for the Windows exe and installer.
Same orange 'F' badge the tray draws at runtime."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).parent / "friday.ico"


def badge(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = max(1, size // 32)
    d.ellipse([pad, pad, size - pad, size - pad], fill="#ff8c1a")
    try:
        font = ImageFont.truetype("arialbd.ttf", int(size * 0.62))
    except Exception:
        font = ImageFont.load_default()
    d.text((size / 2, size / 2 - size * 0.03), "F", fill="white",
           font=font, anchor="mm")
    return img


if __name__ == "__main__":
    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs = [badge(s) for s in sizes]
    imgs[-1].save(OUT, format="ICO", sizes=[(s, s) for s in sizes],
                  append_images=imgs[:-1])
    print(f"Wrote {OUT}")
