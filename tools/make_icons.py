"""Generate every app icon from the single source artwork.

Source: ``crosscopy.jpg`` (2048x2048 rounded dark tile with a glowing document).
Outputs:
  * ``desktop/assets/crosscopy.png``  (256px tray/window icon)
  * ``desktop/assets/crosscopy.ico``  (multi-resolution Windows icon)
  * ``mobile/android/app/src/main/res/mipmap-*/ic_launcher.png``        (legacy)
  * ``mobile/android/app/src/main/res/mipmap-*/ic_launcher_foreground.png`` (adaptive)
  * ``mobile/.../mipmap-anydpi-v26/ic_launcher.xml`` + background colour

Run with:  desktop/.venv/Scripts/python tools/make_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "crosscopy.jpg"
RES = ROOT / "mobile" / "android" / "app" / "src" / "main" / "res"

# tile radius as a fraction of the tile width, measured from the artwork
CORNER_RATIO = 0.225

LEGACY_SIZES = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}
ADAPTIVE_SIZES = {"mdpi": 108, "hdpi": 162, "xhdpi": 216, "xxhdpi": 324, "xxxhdpi": 432}


def _span(values: list[int], threshold: int = 35) -> tuple[int, int] | None:
    hits = [index for index, value in enumerate(values) if value < threshold]
    return (hits[0], hits[-1]) if hits else None


def tile_bbox(image: Image.Image) -> tuple[int, int, int, int]:
    """Crop away the flat outer margin *and* the drop shadow around the tile.

    A plain difference mask catches the shadow too, which would leave a grey
    ring inside the rounded icon. The tile is much darker than both the page and
    its own shadow, so scanning the centre row and column for very dark pixels
    finds the real edges.
    """
    luma = image.convert("L")
    width, height = image.size
    middle_row = list(luma.crop((0, height // 2, width, height // 2 + 1)).tobytes())
    middle_col = list(luma.crop((width // 2, 0, width // 2 + 1, height)).tobytes())
    rows = _span(middle_row)
    columns = _span(middle_col)
    if rows is None or columns is None:  # pragma: no cover - artwork fallback
        return (0, 0, *image.size)
    return (rows[0], columns[0], rows[1] + 1, columns[1] + 1)


def rounded(image: Image.Image, size: int) -> Image.Image:
    """Resize to a square and round the corners to match the artwork."""
    canvas = image.convert("RGBA").resize((size, size), Image.LANCZOS)
    scale = 4  # supersample the mask so the corners stay smooth
    mask = Image.new("L", (size * scale, size * scale), 0)
    radius = int(size * CORNER_RATIO) * scale
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size * scale - 1, size * scale - 1), radius=radius, fill=255
    )
    mask = mask.resize((size, size), Image.LANCZOS)
    canvas.putalpha(mask)
    return canvas


def main() -> None:
    source = Image.open(SOURCE)
    cropped = source.crop(tile_bbox(source))
    master = cropped.convert("RGBA").resize((1024, 1024), Image.LANCZOS)
    # Sample the tile's own dark background, well inside the rounded edge.
    raw = cropped.convert("RGB").tobytes()
    background = min(zip(raw[0::3], raw[1::3], raw[2::3]), key=sum)
    hex_background = "#{:02X}{:02X}{:02X}".format(*background)
    print(f"tile {cropped.size} background {hex_background}")

    assets = ROOT / "desktop" / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    tray = rounded(master, 256)
    tray.save(assets / "crosscopy.png")
    # A large master keeps Windows from up-scaling in the Alt-Tab switcher.
    rounded(master, 256).save(
        assets / "crosscopy.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print("wrote", assets / "crosscopy.png", assets / "crosscopy.ico")

    for density, size in LEGACY_SIZES.items():
        folder = RES / f"mipmap-{density}"
        folder.mkdir(parents=True, exist_ok=True)
        rounded(master, size).save(folder / "ic_launcher.png")
        print("wrote", folder / "ic_launcher.png", f"{size}px")

    for density, size in ADAPTIVE_SIZES.items():
        folder = RES / f"mipmap-{density}"
        folder.mkdir(parents=True, exist_ok=True)
        # Full-bleed foreground: the launcher mask crops the tile edges, which
        # keeps the glyph centred and avoids a visible square inside the mask.
        master.resize((size, size), Image.LANCZOS).save(folder / "ic_launcher_foreground.png")
        print("wrote", folder / "ic_launcher_foreground.png", f"{size}px")

    (RES / "values").mkdir(parents=True, exist_ok=True)
    (RES / "values" / "colors.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\r\n'
        "<resources>\r\n"
        f'    <color name="ic_launcher_background">{hex_background}</color>\r\n'
        "</resources>\r\n",
        encoding="utf-8",
    )

    anydpi = RES / "mipmap-anydpi-v26"
    anydpi.mkdir(parents=True, exist_ok=True)
    (anydpi / "ic_launcher.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\r\n'
        '<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">\r\n'
        '    <background android:drawable="@color/ic_launcher_background" />\r\n'
        '    <foreground android:drawable="@mipmap/ic_launcher_foreground" />\r\n'
        "</adaptive-icon>\r\n",
        encoding="utf-8",
    )
    print("wrote adaptive icon xml + colors.xml")


if __name__ == "__main__":
    main()
