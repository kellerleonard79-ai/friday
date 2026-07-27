"""Generate friday.icns for Friday.app — the same orange 'F' badge the menu
bar draws at runtime.

Builds a .iconset directory and hands it to iconutil, which is the only
supported way to produce a .icns Finder will render at every size.
"""

import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent
ICONSET = HERE / "friday.iconset"
OUT = HERE / "friday.icns"

# macOS wants each size at 1x and 2x. The 2x of size N is the NxN image
# rendered at 2N pixels, named <N>x<N>@2x.
SIZES = [16, 32, 128, 256, 512]

_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Bold.ttf",
]


def _font(px: int):
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, px)
            except Exception:
                continue
    return ImageFont.load_default()


def badge(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # macOS app icons are inset from the canvas rather than bleeding to the
    # edge, which is why this pad is far larger than the Windows .ico's.
    pad = max(1, round(size * 0.06))
    d.ellipse([pad, pad, size - pad, size - pad], fill="#ff8c1a")
    d.text((size / 2, size / 2 - size * 0.03), "F", fill="white",
           font=_font(int(size * 0.62)), anchor="mm")
    return img


def main() -> int:
    if shutil.which("iconutil") is None:
        print("iconutil not found — this script only runs on macOS.",
              file=sys.stderr)
        return 1

    if ICONSET.exists():
        shutil.rmtree(ICONSET)
    ICONSET.mkdir(parents=True)

    for s in SIZES:
        badge(s).save(ICONSET / f"icon_{s}x{s}.png")
        badge(s * 2).save(ICONSET / f"icon_{s}x{s}@2x.png")

    subprocess.run(["iconutil", "-c", "icns", str(ICONSET), "-o", str(OUT)],
                   check=True)
    shutil.rmtree(ICONSET)
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
