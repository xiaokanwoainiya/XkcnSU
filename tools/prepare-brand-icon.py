#!/usr/bin/env python3
"""Prepare supplied monochrome artwork for Android's adaptive icon safe area."""
import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    image = ImageOps.exif_transpose(Image.open(args.source)).convert("L")
    alpha = ImageOps.invert(image).point(lambda value: 0 if value < 12 else value)
    bounds = alpha.getbbox()
    if bounds is None:
        raise ValueError("The icon image contains no visible artwork")
    alpha = alpha.crop(bounds)
    height = 248
    width = round(alpha.width * height / alpha.height)
    alpha = alpha.resize((width, height), Image.Resampling.LANCZOS)
    foreground = Image.new("RGBA", (432, 432))
    mark = Image.new("RGBA", alpha.size, (0, 0, 0, 0))
    mark.putalpha(alpha)
    foreground.alpha_composite(mark, ((432 - width) // 2, (432 - height) // 2))
    destination = root / "manager/app/src/main/res/drawable-xxxhdpi/ic_launcher_imported.png"
    destination.parent.mkdir(parents=True, exist_ok=True)
    foreground.save(destination, optimize=True)
    print("Imported raster is separate; the default XKCNSU vector remains authoritative.")

    preview = Image.new("RGB", (768, 288), "#e8ebed")
    for index, shape in enumerate(("circle", "rounded", "splash")):
        icon = Image.new("RGBA", foreground.size, "white")
        icon.alpha_composite(foreground)
        icon = icon.crop((72, 72, 360, 360)).resize((192, 192), Image.Resampling.LANCZOS)
        mask = Image.new("L", icon.size)
        draw = ImageDraw.Draw(mask)
        if shape == "rounded":
            draw.rounded_rectangle((0, 0, 191, 191), radius=40, fill=255)
        else:
            draw.ellipse((0, 0, 191, 191), fill=255)
        preview.paste(icon, (32 + 256 * index, 48), mask)
    output = root / "out/brand-icon"
    output.mkdir(parents=True, exist_ok=True)
    preview.save(output / "icon-preview.png", optimize=True)
    print(f"Android icon: {destination}")
    print(f"Artwork: {width} x {height} pixels inside a 432-pixel adaptive layer")


if __name__ == "__main__":
    main()
