from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


@dataclass
class UiNode:
    node_id: str
    class_name: str
    package: str
    x1: int
    y1: int
    x2: int
    y2: int
    center_x: int
    center_y: int
    clickable: bool
    enabled: bool
    actionable: bool


def parse_bool(raw: str | None) -> bool:
    return str(raw).lower() == "true"


def parse_bounds(raw: str | None) -> tuple[int, int, int, int] | None:
    if not raw:
        return None
    m = BOUNDS_RE.fullmatch(raw.strip())
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def make_node_id(class_name: str, package: str, bounds: tuple[int, int, int, int], order: int) -> str:
    source = f"{class_name}|{package}|{bounds[0]},{bounds[1]},{bounds[2]},{bounds[3]}|{order}"
    return hashlib.sha1(source.encode("utf-8")).hexdigest()[:16]


def extract_nodes_from_xml(xml_text: str) -> list[UiNode]:
    root = ET.fromstring(xml_text)
    nodes: list[UiNode] = []
    order = 0
    for elem in root.iter("node"):
        bounds = parse_bounds(elem.attrib.get("bounds"))
        if bounds is None:
            continue

        clickable = parse_bool(elem.attrib.get("clickable"))
        enabled = parse_bool(elem.attrib.get("enabled"))
        actionable = clickable and enabled

        x1, y1, x2, y2 = bounds
        center_x = (x1 + x2) // 2
        center_y = (y1 + y2) // 2
        class_name = elem.attrib.get("class", "")
        package = elem.attrib.get("package", "")
        node_id = make_node_id(class_name, package, bounds, order)
        order += 1

        nodes.append(
            UiNode(
                node_id=node_id,
                class_name=class_name,
                package=package,
                x1=x1,
                y1=y1,
                x2=x2,
                y2=y2,
                center_x=center_x,
                center_y=center_y,
                clickable=clickable,
                enabled=enabled,
                actionable=actionable,
            )
        )

    return nodes


def dedupe_nodes(nodes: list[UiNode]) -> list[UiNode]:
    seen: set[tuple[int, int, int, int, bool, bool]] = set()
    result: list[UiNode] = []
    for n in nodes:
        key = (n.x1, n.y1, n.x2, n.y2, n.clickable, n.enabled)
        if key in seen:
            continue
        seen.add(key)
        result.append(n)
    return result


def filter_actionable_nodes(nodes: list[UiNode]) -> list[UiNode]:
    return [n for n in nodes if n.actionable]


def normalize(v: int, max_v: int) -> float:
    if max_v <= 0:
        return 0.0
    return round(v / max_v, 3)


def build_page_id(actionable_nodes: list[UiNode], screen_w: int, screen_h: int, orientation: str) -> str:
    normalized = []
    for n in actionable_nodes:
        normalized.append(
            (
                normalize(n.x1, screen_w),
                normalize(n.y1, screen_h),
                normalize(n.x2, screen_w),
                normalize(n.y2, screen_h),
            )
        )
    normalized.sort()
    signature = {
        "o": orientation,
        "w": int(screen_w),
        "h": int(screen_h),
        "n": normalized,
        "c": len(actionable_nodes),
    }
    raw = json.dumps(signature, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def to_button_dicts(actionable_nodes: list[UiNode]) -> list[dict[str, Any]]:
    buttons = []
    for n in actionable_nodes:
        buttons.append(
            {
                "button_id": n.node_id,
                "bounds": {"x1": n.x1, "y1": n.y1, "x2": n.x2, "y2": n.y2},
                "center": {"x": n.center_x, "y": n.center_y},
                "state": {"clickable": n.clickable, "enabled": n.enabled, "actionable": n.actionable},
                "class": n.class_name,
                "package": n.package,
            }
        )
    return buttons


def crop_actionable_buttons(
    screenshot_path: Path,
    actionable_nodes: list[UiNode],
    output_dir: Path,
) -> list[dict[str, Any]]:
    import cv2

    image = cv2.imread(str(screenshot_path), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"failed to read screenshot: {screenshot_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    h, w = image.shape[:2]
    crops: list[dict[str, Any]] = []

    for idx, n in enumerate(actionable_nodes):
        x1 = max(0, min(n.x1, w - 1))
        y1 = max(0, min(n.y1, h - 1))
        x2 = max(1, min(n.x2, w))
        y2 = max(1, min(n.y2, h))
        if x2 <= x1 or y2 <= y1:
            continue

        patch = image[y1:y2, x1:x2]
        file_name = f"btn_{idx:03d}_{n.node_id}.png"
        out_path = output_dir / file_name
        cv2.imwrite(str(out_path), patch)
        crops.append(
            {
                "button_id": n.node_id,
                "crop_path": str(out_path),
                "bounds": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
            }
        )

    return crops


def analyze_xml(
    xml_text: str,
    screen_w: int,
    screen_h: int,
    orientation: str,
) -> dict[str, Any]:
    raw_nodes = extract_nodes_from_xml(xml_text)
    deduped_nodes = dedupe_nodes(raw_nodes)
    actionable = filter_actionable_nodes(deduped_nodes)
    page_id = build_page_id(actionable, screen_w, screen_h, orientation)
    return {
        "page_id": page_id,
        "total_nodes": len(deduped_nodes),
        "actionable_nodes": len(actionable),
        "buttons": to_button_dicts(actionable),
    }
