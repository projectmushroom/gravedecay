#!/usr/bin/env python3
"""Generate local UI assets from pinned Font Logos SVGs (no network/build deps)."""
import argparse
import copy
import json
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def outputs():
    symbols = []
    catalog = "clients/apple/GravedecayKit/Sources/GravedecayKit/Resources/OSLogos.xcassets"
    yield f"{catalog}/Contents.json", json.dumps({"info": {"author": "xcode", "version": 1}}, indent=2) + "\n"
    for path in sorted((ROOT / "assets/os-logos").glob("*.svg")):
        source = ET.parse(path).getroot()
        viewbox = source.get("viewBox", "0 0 {} {}".format(
            source.get("width", "512").removesuffix("px"), source.get("height", "512").removesuffix("px")))
        svg = ET.Element("svg", xmlns="http://www.w3.org/2000/svg", viewBox=viewbox)

        def clean(element):
            tag = element.tag.split("}")[-1]
            if tag not in ("g", "path", "circle", "ellipse", "rect", "polygon", "polyline"):
                return None
            allowed = {"d", "transform", "fill-rule", "clip-rule", "display", "style",
                       "cx", "cy", "r", "rx", "ry", "x", "y", "width", "height", "points"}
            result = ET.Element(tag, {k: v for k, v in element.attrib.items() if k in allowed})
            for child in element:
                item = clean(child)
                if item is not None:
                    result.append(item)
            return result

        for child in source:
            item = clean(child)
            if item is not None:
                svg.append(item)
        data = ET.tostring(svg, encoding="unicode") + "\n"
        yield f"clients/omarchy/os-logos/{path.name}", data
        yield f"{catalog}/os-{path.stem}.imageset/{path.name}", data
        yield f"{catalog}/os-{path.stem}.imageset/Contents.json", json.dumps({
            "images": [{"filename": path.name, "idiom": "universal"}],
            "info": {"author": "xcode", "version": 1},
            "properties": {"preserves-vector-representation": True, "template-rendering-intent": "template"},
        }, indent=2) + "\n"
        symbol = copy.deepcopy(svg)
        symbol.tag = "symbol"
        symbol.attrib.pop("xmlns")
        symbol.set("id", "os-" + path.stem)
        symbols.append(ET.tostring(symbol, encoding="unicode"))
    yield "dashboard/static/os-logos.svg", '<svg xmlns="http://www.w3.org/2000/svg" style="display:none" aria-hidden="true">' + "".join(symbols) + "</svg>\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for name, data in outputs():
        path = ROOT / name
        if args.check:
            if not path.exists() or path.read_text() != data:
                raise SystemExit(f"Stale generated OS logos: {name}; run {Path(__file__).name}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(data)
