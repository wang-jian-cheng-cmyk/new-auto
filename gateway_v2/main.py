from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from xml_pipeline import (
    analyze_xml,
    build_page_id,
    crop_actionable_buttons,
    dedupe_nodes,
    extract_nodes_from_xml,
    filter_actionable_nodes,
)


BASE_DIR = Path(__file__).resolve().parent
TMP_DIR = BASE_DIR / "tmp"
CROPS_DIR = BASE_DIR / "crops"
SYSTEM_PROMPT = (BASE_DIR / "system_prompt.txt").read_text(encoding="utf-8")
MODEL = os.getenv("MODEL", "openai/gpt-5.2")

PAGE_LIBRARY_PATH = BASE_DIR / "page_library.json"
SKILL_LIBRARY_PATH = BASE_DIR / "skill_library.json"
LEARN_QUEUE_PATH = BASE_DIR / "learn_queue.json"

LOG_LEVEL = os.getenv("GATEWAY_V2_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [gateway_v2] %(message)s",
)
LOGGER = logging.getLogger("gateway_v2")

TMP_DIR.mkdir(exist_ok=True)
CROPS_DIR.mkdir(exist_ok=True)

if PAGE_LIBRARY_PATH.exists():
    PAGE_LIBRARY = json.loads(PAGE_LIBRARY_PATH.read_text(encoding="utf-8"))
else:
    PAGE_LIBRARY = {"pages": {}}

if SKILL_LIBRARY_PATH.exists():
    SKILL_LIBRARY = json.loads(SKILL_LIBRARY_PATH.read_text(encoding="utf-8"))
else:
    SKILL_LIBRARY = {"skills": []}

if LEARN_QUEUE_PATH.exists():
    LEARN_QUEUE = json.loads(LEARN_QUEUE_PATH.read_text(encoding="utf-8"))
else:
    LEARN_QUEUE: list[dict] = []


class HistoryItem(BaseModel):
    action: str = "wait"
    intent: str = "observe_state"
    x: int = 0
    y: int = 0
    wait_ms: int = 1000
    result: str = "unknown"
    effect: str = "unknown"
    reason: str = ""
    timestamp_ms: int = 0


class UiNodeItem(BaseModel):
    node_id: str
    x1: int
    y1: int
    x2: int
    y2: int
    center_x: int
    center_y: int
    class_name: str = Field(alias="class")
    package_name: str = Field(alias="package")
    clickable: bool
    enabled: bool
    actionable: bool = False
    source: str = "unknown"


class DecideRequestV2(BaseModel):
    session_id: str
    timestamp_ms: int
    current_goal_id: str
    screen_w: int
    screen_h: int
    orientation: str = "landscape"
    history: list[HistoryItem] = Field(default_factory=list)
    ui_nodes: list[UiNodeItem] = Field(default_factory=list)
    screenshot_file_path: str | None = None


class DecideResponseV2(BaseModel):
    action: Literal["click", "wait"]
    intent: str
    x: int
    y: int
    wait_ms: int = Field(ge=300, le=5000)
    goal_id: str
    reason: str
    skill_id: str = ""
    step_index: int = -1


@dataclass
class SessionContext:
    active_skill_id: str = ""
    active_steps: deque[dict] = field(default_factory=deque)
    active_step_index: int = 0
    model_cooldown_until_ms: int = 0
    recent_page_ids: deque[str] = field(default_factory=lambda: deque(maxlen=6))


SESSION_STORE: dict[str, SessionContext] = {}
LEARN_SESSION_BUFFER: dict[str, list[dict]] = {}
LEARN_QUEUE_LOCK = asyncio.Lock()

app = FastAPI(title="New Auto Gateway V2", version="0.2.0")


def log_event(event: str, **kwargs: Any) -> None:
    payload = {"event": event, **kwargs}
    try:
        LOGGER.info(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        LOGGER.info("%s %s", event, kwargs)


@app.on_event("startup")
async def on_startup() -> None:
    log_event(
        "startup",
        model=MODEL,
        pages=len(PAGE_LIBRARY.get("pages", {})),
        skills=len(SKILL_LIBRARY.get("skills", [])),
        queue=len(LEARN_QUEUE),
    )


@app.get("/health")
def health() -> dict[str, Any]:
    pending = len([x for x in LEARN_QUEUE if x.get("status") in {"pending", "failed"}])
    log_event("health_check", pending=pending)
    return {
        "ok": True,
        "model": MODEL,
        "pages": len(PAGE_LIBRARY.get("pages", {})),
        "skills": len(SKILL_LIBRARY.get("skills", [])),
        "learn_queue_pending": pending,
    }


@app.get("/page_library")
def page_library() -> dict[str, Any]:
    return PAGE_LIBRARY


@app.get("/skill_library")
def skill_library() -> dict[str, Any]:
    return SKILL_LIBRARY


@app.get("/learn_queue_v2")
def learn_queue_v2() -> dict[str, Any]:
    return {"items": LEARN_QUEUE[-100:]}


@app.post("/v2/analyze_xml")
async def analyze_xml_endpoint(
    xml_file: UploadFile = File(...),
    screen_w: int = Form(...),
    screen_h: int = Form(...),
    orientation: str = Form("landscape"),
    screenshot_file: UploadFile | None = File(default=None),
    persist_page: bool = Form(True),
) -> dict[str, Any]:
    request_id = uuid.uuid4().hex[:12]
    started = time.time()
    log_event("analyze_xml_start", request_id=request_id, orientation=orientation, screen_w=screen_w, screen_h=screen_h)
    xml_text = (await xml_file.read()).decode("utf-8", errors="ignore")
    if not xml_text.strip():
        log_event("analyze_xml_error", request_id=request_id, error="empty_xml")
        raise HTTPException(status_code=422, detail={"error_code": "empty_xml", "request_id": request_id})

    report = analyze_xml(xml_text, screen_w=screen_w, screen_h=screen_h, orientation=orientation)
    page_id = report["page_id"]

    crops = []
    if screenshot_file is not None:
        screenshot_name = TMP_DIR / f"frame-{request_id}.png"
        screenshot_name.write_bytes(await screenshot_file.read())
        nodes = extract_nodes_from_xml(xml_text)
        nodes = dedupe_nodes(nodes)
        actionable = filter_actionable_nodes(nodes)
        page_crop_dir = CROPS_DIR / page_id
        crops = crop_actionable_buttons(screenshot_name, actionable, page_crop_dir)
        log_event(
            "analyze_xml_crops",
            request_id=request_id,
            page_id=page_id,
            actionable_nodes=len(actionable),
            crop_count=len(crops),
        )

    if persist_page:
        PAGE_LIBRARY.setdefault("pages", {})[page_id] = {
            "updated_at_ms": int(time.time() * 1000),
            "screen": {"width": screen_w, "height": screen_h, "orientation": orientation},
            "buttons": report["buttons"],
        }
        persist_page_library()
        log_event("analyze_xml_persisted", request_id=request_id, page_id=page_id, buttons=len(report["buttons"]))

    log_event(
        "analyze_xml_done",
        request_id=request_id,
        page_id=page_id,
        total_nodes=report["total_nodes"],
        actionable_nodes=report["actionable_nodes"],
        elapsed_ms=int((time.time() - started) * 1000),
    )

    return {
        "ok": True,
        "request_id": request_id,
        "page_id": page_id,
        "total_nodes": report["total_nodes"],
        "actionable_nodes": report["actionable_nodes"],
        "buttons": report["buttons"],
        "button_crops": crops,
    }


@app.post("/learn_v2")
async def learn_v2(request: Request) -> dict[str, Any]:
    request_id = uuid.uuid4().hex[:12]
    started = time.time()
    form = await request.form()
    try:
        session_id = str(form.get("session_id", "device-local"))
        goal_id = str(form.get("goal_id", "daily_loop"))
        description = str(form.get("description", "")).strip()
        action_type = str(form.get("action_type", "click"))
        intent = str(form.get("intent", "observe_state"))
        skill_tags = parse_tags(str(form.get("skill_tags", "")))
        scene_tags = parse_tags(str(form.get("scene_tags", "")))
        x = int(form.get("x", "0"))
        y = int(form.get("y", "0"))
        wait_ms = int(form.get("wait_ms", "1200"))
        sequence_done = str(form.get("sequence_done", "false")).lower() == "true"
        before_file = form.get("before_file")
        after_file = form.get("after_file")
    except Exception as exc:
        LOGGER.exception("learn_v2_parse_failed request_id=%s", request_id)
        raise HTTPException(status_code=422, detail={"error_code": "learn_parse_failed", "error_message": str(exc)}) from exc

    if before_file is None or after_file is None:
        log_event("learn_v2_error", request_id=request_id, error="learn_missing_screenshots")
        raise HTTPException(status_code=422, detail={"error_code": "learn_missing_screenshots"})

    before_name = TMP_DIR / sanitize_filename(f"learn-before-{request_id}.png")
    after_name = TMP_DIR / sanitize_filename(f"learn-after-{request_id}.png")
    before_name.write_bytes(await before_file.read())
    after_name.write_bytes(await after_file.read())

    task = {
        "task_id": request_id,
        "status": "pending",
        "retry_count": 0,
        "updated_at_ms": int(time.time() * 1000),
        "request": {
            "session_id": session_id,
            "goal_id": goal_id,
            "description": description,
            "action_type": action_type,
            "intent": intent,
            "skill_tags": skill_tags,
            "scene_tags": scene_tags,
            "x": x,
            "y": y,
            "wait_ms": wait_ms,
            "sequence_done": sequence_done,
            "before_path": str(before_name),
            "after_path": str(after_name),
        },
    }
    LEARN_QUEUE.append(task)
    persist_learn_queue()
    log_event(
        "learn_v2_queued",
        request_id=request_id,
        goal_id=goal_id,
        intent=intent,
        sequence_done=sequence_done,
        queue_size=len(LEARN_QUEUE),
    )
    await process_learn_queue()

    item = next((x for x in LEARN_QUEUE if x.get("task_id") == request_id), task)
    log_event(
        "learn_v2_done",
        request_id=request_id,
        status=item.get("status"),
        retry_count=item.get("retry_count", 0),
        elapsed_ms=int((time.time() - started) * 1000),
    )
    return {
        "ok": item.get("status") == "done",
        "task_id": request_id,
        "status": item.get("status"),
        "message": item.get("message", "queued"),
    }


@app.post("/decide_v2", response_model=DecideResponseV2)
async def decide_v2(request: Request) -> DecideResponseV2:
    request_id = uuid.uuid4().hex[:12]
    started = time.time()
    try:
        req = await parse_decide_v2_request(request)
        session = SESSION_STORE.setdefault(req.session_id, SessionContext())

        log_event(
            "decide_v2_start",
            request_id=request_id,
            session_id=req.session_id,
            goal_id=req.current_goal_id,
            orientation=req.orientation,
            screen_w=req.screen_w,
            screen_h=req.screen_h,
            history_count=len(req.history),
            ui_nodes_count=len(req.ui_nodes),
        )

        if req.orientation != "landscape" or req.screen_w <= req.screen_h:
            resp = fallback_wait(req.current_goal_id, "only_landscape_supported")
            log_event("decide_v2_done", request_id=request_id, strategy="orientation_guard", action=resp.action, reason=resp.reason)
            return resp

        level1_nodes = [n for n in req.ui_nodes if n.clickable and n.enabled]
        level2_nodes = [n for n in req.ui_nodes if (not n.clickable) and n.enabled]
        nodes_for_page = level1_nodes if level1_nodes else level2_nodes
        page_id = page_id_from_nodes(nodes_for_page, req.screen_w, req.screen_h, req.orientation)
        session.recent_page_ids.append(page_id)
        log_event(
            "decide_v2_node_split",
            request_id=request_id,
            page_id=page_id,
            level1_count=len(level1_nodes),
            level2_count=len(level2_nodes),
            page_source=("level1" if level1_nodes else "level2"),
        )

        persist_page_if_new(page_id, req)

        skill_step = next_skill_step(req.current_goal_id, session)
        if skill_step is not None:
            log_event(
                "decide_v2_done",
                request_id=request_id,
                strategy="active_skill",
                page_id=page_id,
                action=skill_step.action,
                skill_id=skill_step.skill_id,
                step_index=skill_step.step_index,
                elapsed_ms=int((time.time() - started) * 1000),
            )
            return skill_step

        triggered = trigger_skill(req.current_goal_id, req.history, page_id, session)
        if triggered is not None:
            log_event(
                "decide_v2_done",
                request_id=request_id,
                strategy="trigger_skill",
                page_id=page_id,
                action=triggered.action,
                skill_id=triggered.skill_id,
                step_index=triggered.step_index,
                elapsed_ms=int((time.time() - started) * 1000),
            )
            return triggered

        now = int(time.time() * 1000)
        if now < session.model_cooldown_until_ms:
            resp = fallback_wait(req.current_goal_id, "model_cooldown", 600)
            log_event("decide_v2_done", request_id=request_id, strategy="cooldown_wait", page_id=page_id, action=resp.action, reason=resp.reason)
            return resp

        if not nodes_for_page:
            resp = fallback_wait(req.current_goal_id, "no_actionable_buttons", 900)
            log_event("decide_v2_done", request_id=request_id, strategy="no_nodes_wait", page_id=page_id, action=resp.action, reason=resp.reason)
            return resp

        if should_use_model(req.history):
            payload = extract_json(call_opencode(build_user_prompt(req, page_id, level1_nodes, level2_nodes), req.screenshot_file_path))
            session.model_cooldown_until_ms = now + 900
            resp = normalize_model_response(payload, req.current_goal_id, nodes_for_page)
            log_event(
                "decide_v2_done",
                request_id=request_id,
                strategy="model",
                page_id=page_id,
                action=resp.action,
                intent=resp.intent,
                x=resp.x,
                y=resp.y,
                wait_ms=resp.wait_ms,
                elapsed_ms=int((time.time() - started) * 1000),
            )
            return resp

        if level1_nodes:
            resp = choose_rule_action(req.current_goal_id, level1_nodes)
            strategy = "rule_level1"
        else:
            resp = fallback_wait(req.current_goal_id, "level2_candidates_need_model", 1200)
            strategy = "level2_wait"
        log_event(
            "decide_v2_done",
            request_id=request_id,
            strategy=strategy,
            page_id=page_id,
            action=resp.action,
            intent=resp.intent,
            x=resp.x,
            y=resp.y,
            wait_ms=resp.wait_ms,
            elapsed_ms=int((time.time() - started) * 1000),
        )
        return resp
    except HTTPException as exc:
        log_event("decide_v2_http_error", request_id=request_id, status=exc.status_code, detail=exc.detail)
        raise
    except Exception as exc:
        LOGGER.exception("decide_v2_unhandled request_id=%s error=%s", request_id, exc)
        raise HTTPException(status_code=500, detail={"error_code": "decide_v2_internal_error", "request_id": request_id}) from exc


async def process_learn_queue() -> None:
    async with LEARN_QUEUE_LOCK:
        changed = False
        for item in LEARN_QUEUE:
            if item.get("status") not in {"pending", "failed"}:
                continue
            if int(item.get("retry_count", 0)) >= 3:
                continue

            item["status"] = "sending"
            try:
                item["message"] = process_learn_task(item)
                item["status"] = "done"
                item["last_error"] = ""
                log_event(
                    "learn_queue_item_done",
                    task_id=item.get("task_id"),
                    message=item.get("message", ""),
                )
            except Exception as exc:
                item["status"] = "failed"
                item["retry_count"] = int(item.get("retry_count", 0)) + 1
                item["last_error"] = str(exc)
                LOGGER.exception("learn_queue_item_failed task_id=%s", item.get("task_id"))
            item["updated_at_ms"] = int(time.time() * 1000)
            changed = True

        if changed:
            persist_learn_queue()
            persist_skill_library()


def process_learn_task(task: dict) -> str:
    req = task.get("request", {})
    session_id = str(req.get("session_id", "device-local"))
    goal_id = str(req.get("goal_id", "daily_loop"))
    description = str(req.get("description", "")).strip()
    intent = str(req.get("intent", "observe_state"))
    skill_tags = parse_tags_list(req.get("skill_tags", []))
    scene_tags = parse_tags_list(req.get("scene_tags", []))
    sequence_done = bool(req.get("sequence_done", False))

    step = {
        "action": "click" if str(req.get("action_type", "click")) == "click" else "wait",
        "intent": intent,
        "x": int(req.get("x", 0)),
        "y": int(req.get("y", 0)),
        "wait_ms": clamp(int(req.get("wait_ms", 1200)), 300, 5000),
        "reason": description or "manual_step",
        "tags": sorted(set(skill_tags + scene_tags + [intent])),
    }

    LEARN_SESSION_BUFFER.setdefault(session_id, []).append(step)
    if not sequence_done:
        log_event("learn_step_buffered", task_id=task.get("task_id"), session_id=session_id, goal_id=goal_id)
        return "learn step appended"

    steps = LEARN_SESSION_BUFFER.pop(session_id, [])
    if not steps:
        return "empty sequence"

    skill = {
        "skill_id": f"manual_{goal_id}_{int(time.time())}",
        "goal_id": goal_id,
        "description": description or "manual learned sequence",
        "skill_tags": sorted(set(skill_tags + scene_tags + [goal_id])),
        "scene_tags": sorted(set(scene_tags + extract_keywords(description))),
        "trigger_intents": sorted(set([s.get("intent", "observe_state") for s in steps])),
        "steps": steps,
        "stats": {"success": 0, "failed": 0, "updated_at_ms": int(time.time() * 1000)},
    }
    SKILL_LIBRARY.setdefault("skills", []).append(skill)
    log_event(
        "skill_saved",
        task_id=task.get("task_id"),
        skill_id=skill["skill_id"],
        goal_id=goal_id,
        steps=len(steps),
    )
    return f"saved {skill['skill_id']}"


def parse_decide_v2_payload(form: Any, screenshot_file_path: str) -> DecideRequestV2:
    history = parse_history_json(str(form.get("history_json", "[]")))
    ui_nodes = parse_ui_nodes_json(str(form.get("ui_nodes_json", "[]")))
    return DecideRequestV2(
        session_id=str(form.get("session_id", "device-local")),
        timestamp_ms=int(form.get("timestamp_ms", "0")),
        current_goal_id=str(form.get("current_goal_id", "daily_loop")),
        screen_w=int(form.get("screen_w", "0")),
        screen_h=int(form.get("screen_h", "0")),
        orientation=str(form.get("orientation", "landscape")),
        history=history,
        ui_nodes=ui_nodes,
        screenshot_file_path=screenshot_file_path,
    )


async def parse_decide_v2_request(request: Request) -> DecideRequestV2:
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        body = await request.json()
        return DecideRequestV2(
            session_id=body.get("session_id", "device-local"),
            timestamp_ms=int(body.get("timestamp_ms", 0)),
            current_goal_id=body.get("current_goal_id", "daily_loop"),
            screen_w=int(body.get("screen_w", 0)),
            screen_h=int(body.get("screen_h", 0)),
            orientation=body.get("orientation", "landscape"),
            history=[HistoryItem(**x) for x in body.get("history", [])],
            ui_nodes=[UiNodeItem(**x) for x in body.get("ui_nodes", [])],
            screenshot_file_path=body.get("screenshot_file_path"),
        )

    form = await request.form()
    screenshot_file = form.get("screenshot_file")
    if screenshot_file is None:
        raise HTTPException(status_code=422, detail={"error_code": "missing_screenshot_file"})
    frame_path = TMP_DIR / sanitize_filename(f"frame-{uuid.uuid4().hex[:10]}.png")
    frame_path.write_bytes(await screenshot_file.read())
    log_event("decide_v2_frame_saved", frame_path=str(frame_path))
    return parse_decide_v2_payload(form, str(frame_path))


def next_skill_step(goal_id: str, session: SessionContext) -> DecideResponseV2 | None:
    if not session.active_steps:
        return None
    step = session.active_steps.popleft()
    idx = session.active_step_index
    session.active_step_index += 1
    return DecideResponseV2(
        action=step.get("action", "wait"),
        intent=step.get("intent", "observe_state"),
        x=int(step.get("x", 0)),
        y=int(step.get("y", 0)),
        wait_ms=clamp(int(step.get("wait_ms", 1200)), 300, 5000),
        goal_id=goal_id,
        reason=str(step.get("reason", "skill_step")),
        skill_id=session.active_skill_id,
        step_index=idx,
    )


def trigger_skill(goal_id: str, history: list[HistoryItem], page_id: str, session: SessionContext) -> DecideResponseV2 | None:
    last_intent = history[-1].intent if history else ""
    last_reason = history[-1].reason if history else ""
    candidates: list[tuple[int, dict]] = []
    for s in SKILL_LIBRARY.get("skills", []):
        if s.get("goal_id") != goal_id:
            continue
        score = 0
        if last_intent and last_intent in parse_tags_list(s.get("trigger_intents", [])):
            score += 3
        if any(t in last_reason for t in parse_tags_list(s.get("scene_tags", []))):
            score += 2
        if page_id in parse_tags_list(s.get("skill_tags", [])):
            score += 1
        if score > 0:
            candidates.append((score, s))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    skill = candidates[0][1]
    session.active_skill_id = str(skill.get("skill_id", ""))
    session.active_steps = deque(skill.get("steps", []))
    session.active_step_index = 0
    return next_skill_step(goal_id, session)


def should_use_model(history: list[HistoryItem]) -> bool:
    if not history:
        return True
    if len(history) >= 2 and history[-1].effect == "no_change" and history[-2].effect == "no_change":
        return True
    return history[-1].action == "wait"


def choose_rule_action(goal_id: str, nodes: list[UiNodeItem]) -> DecideResponseV2:
    sorted_nodes = sorted(nodes, key=lambda n: (n.center_y, n.center_x))
    first = sorted_nodes[0]
    return DecideResponseV2(
        action="click",
        intent="rule_click_first_actionable",
        x=first.center_x,
        y=first.center_y,
        wait_ms=1000,
        goal_id=goal_id,
        reason="rule_fallback_actionable_button",
    )


def normalize_model_response(payload: dict, goal_id: str, nodes: list[UiNodeItem]) -> DecideResponseV2:
    action = str(payload.get("action", "wait"))
    if action not in {"click", "wait"}:
        action = "wait"
    intent = str(payload.get("intent", "observe_state"))
    wait_ms = clamp(int(payload.get("wait_ms", 1000)), 300, 5000)
    reason = str(payload.get("reason", "model_decision"))

    if action == "wait":
        return DecideResponseV2(action="wait", intent=intent, x=0, y=0, wait_ms=wait_ms, goal_id=goal_id, reason=reason)

    button_id = str(payload.get("button_id", ""))
    matched = next((n for n in nodes if n.node_id == button_id), None)
    if matched is None:
        matched = nodes[0] if nodes else None
    if matched is None:
        return fallback_wait(goal_id, "model_no_button")

    return DecideResponseV2(
        action="click",
        intent=intent,
        x=matched.center_x,
        y=matched.center_y,
        wait_ms=wait_ms,
        goal_id=goal_id,
        reason=reason,
        skill_id=str(payload.get("skill_id", "")),
        step_index=int(payload.get("step_index", -1)),
    )


def page_id_from_nodes(nodes: list[UiNodeItem], screen_w: int, screen_h: int, orientation: str) -> str:
    fake_xml_nodes = [
        {
            "node_id": n.node_id,
            "x1": n.x1,
            "y1": n.y1,
            "x2": n.x2,
            "y2": n.y2,
            "center_x": n.center_x,
            "center_y": n.center_y,
            "class": n.class_name,
            "package": n.package_name,
            "clickable": n.clickable,
            "enabled": n.enabled,
        }
        for n in nodes
    ]
    # Reuse hash logic based on actionable distribution.
    class Obj:
        pass

    converted = []
    for row in fake_xml_nodes:
        o = Obj()
        o.node_id = row["node_id"]
        o.class_name = row["class"]
        o.package = row["package"]
        o.x1 = row["x1"]
        o.y1 = row["y1"]
        o.x2 = row["x2"]
        o.y2 = row["y2"]
        o.center_x = row["center_x"]
        o.center_y = row["center_y"]
        o.clickable = row["clickable"]
        o.enabled = row["enabled"]
        o.actionable = row["clickable"] and row["enabled"]
        converted.append(o)
    return build_page_id(converted, screen_w, screen_h, orientation)


def persist_page_if_new(page_id: str, req: DecideRequestV2) -> None:
    pages = PAGE_LIBRARY.setdefault("pages", {})
    if page_id in pages:
        pages[page_id]["updated_at_ms"] = int(time.time() * 1000)
        persist_page_library()
        return

    buttons = []
    for n in req.ui_nodes:
        if not n.enabled:
            continue
        buttons.append(
            {
                "button_id": n.node_id,
                "bounds": {"x1": n.x1, "y1": n.y1, "x2": n.x2, "y2": n.y2},
                "center": {"x": n.center_x, "y": n.center_y},
                "state": {
                    "clickable": n.clickable,
                    "enabled": n.enabled,
                    "actionable": (n.clickable and n.enabled) or n.actionable,
                },
                "class": n.class_name,
                "package": n.package_name,
                "intent_tag": "unknown",
                "source": n.source,
            }
        )

    pages[page_id] = {
        "updated_at_ms": int(time.time() * 1000),
        "screen": {"width": req.screen_w, "height": req.screen_h, "orientation": req.orientation},
        "buttons": buttons,
    }
    persist_page_library()


def build_user_prompt(
    req: DecideRequestV2,
    page_id: str,
    level1_nodes: list[UiNodeItem],
    level2_nodes: list[UiNodeItem],
) -> str:
    actionable_buttons = []
    for n in level1_nodes:
        actionable_buttons.append(
            {
                "button_id": n.node_id,
                "center": {"x": n.center_x, "y": n.center_y},
                "class": n.class_name,
                "source": "level1_clickable",
            }
        )

    candidate_buttons = []
    for n in level2_nodes[:18]:
        candidate_buttons.append(
            {
                "button_id": n.node_id,
                "bounds": {"x1": n.x1, "y1": n.y1, "x2": n.x2, "y2": n.y2},
                "center": {"x": n.center_x, "y": n.center_y},
                "class": n.class_name,
                "source": "level2_candidate",
            }
        )

    payload = {
        "task": "你是大话西游手游搬砖者，基于可点按钮列表决定下一步",
        "page_id": page_id,
        "goal_id": req.current_goal_id,
        "history": [h.model_dump() for h in req.history[-6:]],
        "actionable_buttons": actionable_buttons,
        "candidate_buttons": candidate_buttons,
        "rules": [
            "优先从actionable_buttons选择button_id",
            "若actionable_buttons为空，可从candidate_buttons选择最像按钮的目标",
            "若仍不确定，action=wait",
        ],
        "output": {
            "action": "click|wait",
            "intent": "semantic_intent",
            "button_id": "must from actionable_buttons or candidate_buttons when action=click",
            "wait_ms": "300-5000",
            "goal_id": req.current_goal_id,
            "reason": "short_cn_reason",
        },
    }
    return json.dumps(payload, ensure_ascii=False)


def call_opencode(user_prompt: str, screenshot_file_path: str | None) -> str:
    combined = (
        "[SYSTEM_RULES]\n"
        f"{SYSTEM_PROMPT}\n\n"
        "[USER_CONTEXT]\n"
        f"{user_prompt}\n\n"
        "只输出一个JSON对象。"
    )
    cmd = ["opencode", "run", "--model", MODEL]
    if screenshot_file_path:
        cmd += ["--file", screenshot_file_path]

    log_event("opencode_call_start", model=MODEL, with_file=bool(screenshot_file_path))

    result = subprocess.run(cmd, input=combined, capture_output=True, text=True, timeout=45, check=False)
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if result.returncode == 0 and stdout:
        log_event("opencode_call_done", returncode=result.returncode, stdout_len=len(stdout))
        return stdout
    log_event("opencode_call_failed", returncode=result.returncode, stderr=stderr[:180])
    raise HTTPException(status_code=503, detail={"error_code": "opencode_failed", "error_message": stderr[:280] or "empty"})


def extract_json(raw: str) -> dict:
    text = raw.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise HTTPException(status_code=422, detail={"error_code": "model_json_missing"})
    return json.loads(text[start : end + 1])


def parse_history_json(raw: str) -> list[HistoryItem]:
    data = json.loads(raw)
    if not isinstance(data, list):
        return []
    return [HistoryItem(**x) for x in data[-8:] if isinstance(x, dict)]


def parse_ui_nodes_json(raw: str) -> list[UiNodeItem]:
    data = json.loads(raw)
    if not isinstance(data, list):
        return []
    items: list[UiNodeItem] = []
    actionable_count = 0
    candidate_count = 0
    for x in data:
        if not isinstance(x, dict):
            continue
        item = UiNodeItem(**x)
        if item.clickable and item.enabled:
            actionable_count += 1
        elif item.enabled:
            candidate_count += 1
        items.append(item)
    log_event(
        "parse_ui_nodes",
        received=len(data),
        total=len(items),
        actionable=actionable_count,
        candidates=candidate_count,
    )
    return items


def fallback_wait(goal_id: str, reason: str, wait_ms: int = 900) -> DecideResponseV2:
    return DecideResponseV2(action="wait", intent="observe_state", x=0, y=0, wait_ms=clamp(wait_ms, 300, 5000), goal_id=goal_id, reason=reason)


def parse_tags(raw: str) -> list[str]:
    if not raw:
        return []
    return sorted(set([x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]))[:16]


def parse_tags_list(raw: object) -> list[str]:
    if isinstance(raw, list):
        return sorted(set([str(x).strip() for x in raw if str(x).strip()]))[:16]
    if isinstance(raw, str):
        return parse_tags(raw)
    return []


def extract_keywords(text: str) -> list[str]:
    return [x for x in text.replace("，", " ").replace(",", " ").split() if x][:6]


def sanitize_filename(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in {"-", "_", "."})


def clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def persist_page_library() -> None:
    PAGE_LIBRARY_PATH.write_text(json.dumps(PAGE_LIBRARY, ensure_ascii=False, indent=2), encoding="utf-8")
    log_event("persist_page_library", pages=len(PAGE_LIBRARY.get("pages", {})))


def persist_skill_library() -> None:
    SKILL_LIBRARY_PATH.write_text(json.dumps(SKILL_LIBRARY, ensure_ascii=False, indent=2), encoding="utf-8")
    log_event("persist_skill_library", skills=len(SKILL_LIBRARY.get("skills", [])))


def persist_learn_queue() -> None:
    LEARN_QUEUE_PATH.write_text(json.dumps(LEARN_QUEUE, ensure_ascii=False, indent=2), encoding="utf-8")
    log_event("persist_learn_queue", queue_size=len(LEARN_QUEUE))
