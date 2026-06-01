"""
menubar_icon.py
Generate menu bar icons for the F.R.I.D.A.Y. menubar app.

Two sources, in order of preference:

  1. User-provided PNG at ~/Library/Application Support/friday/menubar.png.
     If present, it's auto-scaled to the menu bar height and used for the
     online icon. The paused icon is the same image at ~55% opacity; the
     offline icon is at ~25% opacity. The user may override either with
     menubar-paused.png / menubar-offline.png in the same directory.

  2. Fallback: render the text "F.R.I.D.A.Y." in orange via AppKit (no PIL).

Cached variants live in ~/Library/Caches/friday/menubar/. The cache is
invalidated when the user's source PNG mtime is newer than the cache.

PyObjC is already a transitive dep of rumps — no new requirements.
"""

from __future__ import annotations

import os
from pathlib import Path

from AppKit import (
    NSAttributedString,
    NSBezierPath,
    NSBitmapImageRep,
    NSColor,
    NSCompositingOperationSourceOver,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSGraphicsContext,
    NSImage,
    NSImageInterpolationHigh,
    NSMakeRect,
)
from Foundation import NSDictionary, NSPoint, NSSize

_PNG_FILE_TYPE = 4  # NSBitmapImageFileType.NSBitmapImageFileTypePNG

# Target menu bar logical height; macOS draws 22pt status bar items.
_MENU_BAR_HEIGHT_PT = 22.0

# Opacity for paused/offline variants when derived from a single user icon.
_DIM_ALPHA = {"paused": 0.55, "offline": 0.25}

# Fallback text-rendering colors (used only if no user PNG provided).
_TEXT_COLORS = {
    "online":    (1.00, 0.42, 0.00),
    "paused":    (1.00, 0.42, 0.00, 0.55),
    "offline":   (0.55, 0.55, 0.55),
    "error":     (1.00, 0.23, 0.19),
    "listening": (1.00, 0.20, 0.20),
}
_TEXT = "F.R.I.D.A.Y."
_FONT_POINT_SIZE = 13.0

USER_ICON_DIR  = Path.home() / "Library" / "Application Support" / "friday"
USER_ICON_PATH = USER_ICON_DIR / "menubar.png"

CACHE_DIR = Path.home() / "Library" / "Caches" / "friday" / "menubar"


# ── PNG I/O helpers ─────────────────────────────────────────────────────

def _write_image_png(img: NSImage, out_path: Path) -> None:
    tiff = img.TIFFRepresentation()
    rep = NSBitmapImageRep.imageRepWithData_(tiff)
    png = rep.representationUsingType_properties_(_PNG_FILE_TYPE, NSDictionary.dictionary())
    out_path.parent.mkdir(parents=True, exist_ok=True)
    png.writeToFile_atomically_(str(out_path), True)


def _scaled_to_height(src: NSImage, height_pt: float) -> NSImage:
    sz = src.size()
    if sz.height <= 0:
        return src
    scale = height_pt / sz.height
    new = NSSize(sz.width * scale, height_pt)
    out = NSImage.alloc().initWithSize_(new)
    out.lockFocus()
    src.drawInRect_fromRect_operation_fraction_(
        NSMakeRect(0, 0, new.width, new.height),
        NSMakeRect(0, 0, sz.width, sz.height),
        NSCompositingOperationSourceOver,
        1.0,
    )
    out.unlockFocus()
    return out


def rounded_square_crop(src: NSImage, radius_ratio: float = 0.2237) -> NSImage:
    """Center-crop to a square and mask with a rounded rectangle.
    `radius_ratio` ≈ 0.2237 matches the macOS app-icon (squircle) curvature.
    """
    sz = src.size()
    side = min(sz.width, sz.height)
    out = NSImage.alloc().initWithSize_(NSSize(side, side))
    out.lockFocus()
    ctx = NSGraphicsContext.currentContext()
    if ctx is not None:
        ctx.setImageInterpolation_(NSImageInterpolationHigh)
    radius = side * radius_ratio
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        NSMakeRect(0, 0, side, side), radius, radius,
    ).addClip()
    dst_x = (side - sz.width) / 2.0
    dst_y = (side - sz.height) / 2.0
    src.drawInRect_fromRect_operation_fraction_(
        NSMakeRect(dst_x, dst_y, sz.width, sz.height),
        NSMakeRect(0, 0, sz.width, sz.height),
        NSCompositingOperationSourceOver,
        1.0,
    )
    out.unlockFocus()
    return out


def circular_crop(src: NSImage) -> NSImage:
    """Center-crop the source into a square and mask it with a circle.
    Transparent corners; output is side × side where side = min(width, height).
    """
    sz = src.size()
    side = min(sz.width, sz.height)
    out = NSImage.alloc().initWithSize_(NSSize(side, side))
    out.lockFocus()
    ctx = NSGraphicsContext.currentContext()
    if ctx is not None:
        ctx.setImageInterpolation_(NSImageInterpolationHigh)
    # Clip to a perfect circle inscribed in the output square
    NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(0, 0, side, side)).addClip()
    # Center-crop: draw the source so its center aligns with the circle's center
    dst_x = (side - sz.width) / 2.0
    dst_y = (side - sz.height) / 2.0
    src.drawInRect_fromRect_operation_fraction_(
        NSMakeRect(dst_x, dst_y, sz.width, sz.height),
        NSMakeRect(0, 0, sz.width, sz.height),
        NSCompositingOperationSourceOver,
        1.0,
    )
    out.unlockFocus()
    return out


def _dimmed(src: NSImage, alpha: float) -> NSImage:
    sz = src.size()
    out = NSImage.alloc().initWithSize_(sz)
    out.lockFocus()
    src.drawInRect_fromRect_operation_fraction_(
        NSMakeRect(0, 0, sz.width, sz.height),
        NSMakeRect(0, 0, sz.width, sz.height),
        NSCompositingOperationSourceOver,
        alpha,
    )
    out.unlockFocus()
    return out


def _with_record_dot(src: NSImage) -> NSImage:
    """Composite a small red "recording" dot onto the bottom-right corner.
    Used as the menu bar's "Friday is listening" indicator."""
    sz = src.size()
    out = NSImage.alloc().initWithSize_(sz)
    out.lockFocus()
    src.drawInRect_fromRect_operation_fraction_(
        NSMakeRect(0, 0, sz.width, sz.height),
        NSMakeRect(0, 0, sz.width, sz.height),
        NSCompositingOperationSourceOver,
        1.0,
    )
    dot_diameter = min(sz.width, sz.height) * 0.42
    dot_x = sz.width - dot_diameter
    dot_y = 0.0
    # White stroke ring so the dot reads against any background colour.
    NSColor.whiteColor().set()
    NSBezierPath.bezierPathWithOvalInRect_(
        NSMakeRect(
            dot_x - 1.0, dot_y - 1.0,
            dot_diameter + 2.0, dot_diameter + 2.0,
        ),
    ).fill()
    NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 0.20, 0.20, 1.0).set()
    NSBezierPath.bezierPathWithOvalInRect_(
        NSMakeRect(dot_x, dot_y, dot_diameter, dot_diameter),
    ).fill()
    out.unlockFocus()
    return out


# ── Text fallback ───────────────────────────────────────────────────────

def _attributed(text: str, color: tuple) -> NSAttributedString:
    rgba = color if len(color) == 4 else (*color, 1.0)
    ns_color = NSColor.colorWithSRGBRed_green_blue_alpha_(*rgba)
    font = NSFont.boldSystemFontOfSize_(_FONT_POINT_SIZE)
    attrs = NSDictionary.dictionaryWithObjects_forKeys_(
        [font, ns_color],
        [NSFontAttributeName, NSForegroundColorAttributeName],
    )
    return NSAttributedString.alloc().initWithString_attributes_(text, attrs)


def _render_text(out_path: Path, color: tuple) -> None:
    attr = _attributed(_TEXT, color)
    size = attr.size()
    w, h = int(size.width + 4), int(size.height + 2)
    img = NSImage.alloc().initWithSize_((w, h))
    img.lockFocus()
    attr.drawAtPoint_(NSPoint(2, 1))
    img.unlockFocus()
    _write_image_png(img, out_path)


# ── Public API ──────────────────────────────────────────────────────────

def _cache_is_stale(state: str, source_mtime: float | None) -> bool:
    p = CACHE_DIR / f"{state}.png"
    if not p.exists():
        return True
    if source_mtime is None:
        return False
    return p.stat().st_mtime < source_mtime


def _build_from_user_icon() -> dict[str, str]:
    src_mtime = USER_ICON_PATH.stat().st_mtime
    src = NSImage.alloc().initWithContentsOfFile_(str(USER_ICON_PATH))
    if src is None or src.size().width <= 0:
        return {}
    # Circular crop first (preserves resolution), then scale to bar height.
    scaled = _scaled_to_height(circular_crop(src), _MENU_BAR_HEIGHT_PT)

    out: dict[str, str] = {}
    # Per-state user overrides take precedence over the dimmed/decorated variants.
    overrides = {
        "paused":    USER_ICON_DIR / "menubar-paused.png",
        "offline":   USER_ICON_DIR / "menubar-offline.png",
        "listening": USER_ICON_DIR / "menubar-listening.png",
    }
    for state in ("online", "paused", "offline", "error", "listening"):
        cache_path = CACHE_DIR / f"{state}.png"
        override = overrides.get(state)
        effective_mtime = src_mtime
        if override and override.exists():
            effective_mtime = max(src_mtime, override.stat().st_mtime)
        if not _cache_is_stale(state, effective_mtime):
            out[state] = str(cache_path)
            continue

        if state == "online":
            _write_image_png(scaled, cache_path)
        elif state == "error":
            # No per-state override semantics for error; reuse online.
            _write_image_png(scaled, cache_path)
        elif state == "listening":
            if override and override.exists():
                override_img = NSImage.alloc().initWithContentsOfFile_(str(override))
                if override_img is not None and override_img.size().width > 0:
                    _write_image_png(
                        _scaled_to_height(circular_crop(override_img), _MENU_BAR_HEIGHT_PT),
                        cache_path,
                    )
                else:
                    _write_image_png(_with_record_dot(scaled), cache_path)
            else:
                _write_image_png(_with_record_dot(scaled), cache_path)
        else:
            if override and override.exists():
                override_img = NSImage.alloc().initWithContentsOfFile_(str(override))
                if override_img is not None and override_img.size().width > 0:
                    _write_image_png(
                        _scaled_to_height(circular_crop(override_img), _MENU_BAR_HEIGHT_PT),
                        cache_path,
                    )
                else:
                    _write_image_png(_dimmed(scaled, _DIM_ALPHA[state]), cache_path)
            else:
                _write_image_png(_dimmed(scaled, _DIM_ALPHA[state]), cache_path)
        out[state] = str(cache_path)
    return out


def _build_from_text() -> dict[str, str]:
    out: dict[str, str] = {}
    for state, color in _TEXT_COLORS.items():
        p = CACHE_DIR / f"{state}.png"
        if not p.exists():
            _render_text(p, color)
        out[state] = str(p)
    return out


def ensure_icons() -> dict[str, str]:
    """Return {state: absolute path to PNG}.

    Prefers a user-provided PNG at USER_ICON_PATH; falls back to rendered text.
    """
    if USER_ICON_PATH.exists():
        result = _build_from_user_icon()
        if result:
            return result
    return _build_from_text()


def regenerate() -> dict[str, str]:
    """Force regeneration (e.g. after updating the user icon mid-session)."""
    for p in CACHE_DIR.glob("*.png"):
        p.unlink()
    return ensure_icons()


def using_user_icon() -> bool:
    return USER_ICON_PATH.exists()


def ensure_favicon(out_path: Path, size: int = 128) -> Path | None:
    """Generate a circular favicon from the user PNG into `out_path`.

    Returns the path on success, None if the user PNG is missing. Regenerates
    only if the source is newer than the favicon (or the favicon is missing).
    """
    if not USER_ICON_PATH.exists():
        return None
    src_mtime = USER_ICON_PATH.stat().st_mtime
    if out_path.exists() and out_path.stat().st_mtime >= src_mtime:
        return out_path
    src = NSImage.alloc().initWithContentsOfFile_(str(USER_ICON_PATH))
    if src is None or src.size().width <= 0:
        return None
    cropped = circular_crop(src)
    sized = _scaled_to_height(cropped, float(size))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _write_image_png(sized, out_path)
    return out_path


if __name__ == "__main__":
    USER_ICON_DIR.mkdir(parents=True, exist_ok=True)
    print(f"User-icon location: {USER_ICON_PATH}")
    print(f"  exists: {USER_ICON_PATH.exists()}")
    paths = regenerate()
    for state, path in paths.items():
        print(f"  {state:8s} → {path}")
