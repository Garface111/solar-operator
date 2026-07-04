#!/usr/bin/env python3
"""Render icon.svg to PNG at 16/32/48/128px using cairosvg."""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
SVG = HERE / "icon.svg"


def main() -> None:
    try:
        import cairosvg
    except ImportError:
        print("cairosvg not found — installing…")
        # --break-system-packages for Debian/Ubuntu externally-managed environments
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "cairosvg"],
            capture_output=True,
        )
        if result.returncode != 0:
            subprocess.check_call([
                sys.executable, "-m", "pip", "install",
                "cairosvg", "--break-system-packages",
            ])
        import cairosvg  # noqa: F811

    for size in [16, 32, 48, 128]:
        out = HERE / f"icon{size}.png"
        cairosvg.svg2png(
            url=str(SVG),
            write_to=str(out),
            output_width=size,
            output_height=size,
        )
        print(f"  → {out.name} ({size}×{size})")


if __name__ == "__main__":
    main()
