"""Rebuild desktop branding from the original DoubleSharp artwork.

Requires Pillow (already in backend/requirements.txt). Only maintained icon
assets are written; catalog compilation belongs to the native build.
"""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "app/src/assets"
CATALOG = ROOT / "tauri/assets/voicebox.icon/Assets"
ICONS = ROOT / "tauri/src-tauri/icons"


def main():
    # The supplied wordmark has a square 240px symbol at its left edge.
    mark = Image.open(ASSETS / "doublesharp-logo.png").convert("RGBA").crop((0, 0, 240, 240))
    mark.save(ASSETS / "doublesharp-mark.png")

    foreground = Image.new("RGBA", (1024, 1024))
    foreground.alpha_composite(mark.resize((760, 760), Image.Resampling.LANCZOS), (132, 78))
    draw = ImageDraw.Draw(foreground)
    for x, height, color in zip(
        (459, 483, 507, 531, 555),
        (22, 38, 52, 38, 22),
        ("#22d3ee", "#81b8ff", "#be98ff", "#81b8ff", "#22d3ee"),
    ):
        draw.rounded_rectangle((x, 858 - height // 2, x + 10, 858 + height // 2), radius=5, fill=color)
    foreground.save(CATALOG / "DoubleSharp.png")

    # Solid face, transparent corners: no white matte on the desktop or Dock.
    # Supersample the enclosure so its alpha edge remains smooth at every size.
    tile = Image.new("RGBA", (2048, 2048))
    ImageDraw.Draw(tile).rounded_rectangle(
        (80, 80, 1968, 1968), radius=432, fill="#15181e", outline="#30353f", width=3
    )
    icon = tile.resize((1024, 1024), Image.Resampling.LANCZOS)
    icon.alpha_composite(foreground)
    icon.save(CATALOG / "Voicebox.png")
    icon.resize((256, 256), Image.Resampling.LANCZOS).save(ASSETS / "voicebox-ds-icon.png")

    for filename, size in {
        "32x32.png": 32,
        "64x64.png": 64,
        "128x128.png": 128,
        "128x128@2x.png": 256,
        "icon.png": 512,
        "StoreLogo.png": 50,
        **{f"Square{size}x{size}Logo.png": size for size in (30, 44, 71, 89, 107, 142, 150, 284, 310)},
    }.items():
        icon.resize((size, size), Image.Resampling.LANCZOS).save(ICONS / filename)
    icon.save(ICONS / "icon.icns", format="ICNS")
    icon.save(ICONS / "icon.ico", format="ICO", sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
    print("Updated DoubleSharp desktop PNG, macOS ICNS, Windows ICO, and catalog artwork.")


if __name__ == "__main__":
    main()
