from __future__ import annotations

import argparse
import json
from pathlib import Path

from xml_pipeline import analyze_xml


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze Android UI XML and extract actionable buttons")
    parser.add_argument("--xml", required=True, help="Path to uidump.xml")
    parser.add_argument("--screen-w", type=int, required=True, help="Screen width")
    parser.add_argument("--screen-h", type=int, required=True, help="Screen height")
    parser.add_argument("--orientation", default="landscape", help="landscape or portrait")
    args = parser.parse_args()

    xml_text = Path(args.xml).read_text(encoding="utf-8", errors="ignore")
    result = analyze_xml(xml_text, screen_w=args.screen_w, screen_h=args.screen_h, orientation=args.orientation)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
