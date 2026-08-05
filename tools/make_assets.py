"""Generate the README's SVG visuals, light and dark.

Hand-written SVG rather than a diagramming tool: the output is small, crisp at any
zoom, diffable in review, and needs no toolchain to rebuild. GitHub ignores
``prefers-color-scheme`` inside an SVG referenced by ``<img>``, so every diagram is
emitted twice and the README picks one with ``<picture>``.

    python tools/make_assets.py            # writes assets/*.svg
    python tools/make_assets.py --check    # fails if the files on disk are stale

Numbers here are measured, not decorative. See CHANGELOG 2.0.2 for where the
600s -> 32s -> 1.1s recovery figures come from.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from assets._primitives import ASSETS, DARK, LIGHT
from assets.arbitration import arbitration
from assets.architecture import architecture
from assets.hero import hero
from assets.kvm import kvm
from assets.latency import latency
from assets.optionsplus import optionsplus
from assets.social import social
from assets.terminal import terminal

#: Diagrams that need a light and a dark variant, selected with <picture>.
THEMED = {
    "kvm": kvm,
    "optionsplus": optionsplus,
    "arbitration": arbitration,
    "latency": latency,
    "architecture": architecture,
}
#: Product cards: they carry their own dark background, so one file serves both
#: themes and the 400 KB keyboard vector is embedded once, not twice.
CARDS = {"hero": hero, "social-preview": social, "terminal": terminal}


def build() -> dict[Path, str]:
    out: dict[Path, str] = {}
    for name, fn in THEMED.items():
        for suffix, palette in (("light", LIGHT), ("dark", DARK)):
            out[ASSETS / f"{name}-{suffix}.svg"] = fn(palette) + "\n"
    for name, fn in CARDS.items():
        out[ASSETS / f"{name}.svg"] = fn() + "\n"
    return out


def _render_social_png() -> None:
    """GitHub's social-preview uploader takes PNG/JPG, not SVG.

    Optional: needs rsvg-convert (``brew install librsvg``). The committed PNG is
    what matters, so a machine without it just skips this step.
    """
    source = ASSETS / "social-preview.svg"
    target = ASSETS / "social-preview.png"
    tool = shutil.which("rsvg-convert")
    if tool is None:
        print("skipping social-preview.png: rsvg-convert not installed", file=sys.stderr)
        return
    subprocess.run(
        [tool, "-w", "1280", "-h", "640", str(source), "-o", str(target)],
        check=True,
        capture_output=True,
    )
    print(f"wrote {target.relative_to(ASSETS.parent)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if any file is stale")
    args = parser.parse_args()

    ASSETS.mkdir(exist_ok=True)
    stale = []
    for path, content in build().items():
        if args.check:
            if not path.exists() or path.read_text("utf-8") != content:
                stale.append(path.name)
            continue
        path.write_text(content, "utf-8")
        print(f"wrote {path.relative_to(ASSETS.parent)}")
    if stale:
        print("stale, re-run tools/make_assets.py: " + ", ".join(sorted(stale)), file=sys.stderr)
        return 1
    if not args.check:
        _render_social_png()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
