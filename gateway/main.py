from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent
TMP_DIR = BASE_DIR / "tmp"
TMP_DIR.mkdir(exist_ok=True)
SYSTEM_PROMPT = (BASE_DIR / "system_prompt.txt").read_text(encoding="utf-8")
EXPERIENCE_LIBRARY_PATH = BASE_DIR / "experience_library.json"
LEARN_DEMOS_PATH = BASE_DIR / "learn_demos.jsonl"
LEARN_QUEUE_PATH = BASE_DIR / "learn_queue.json"
MODEL = os.getenv("MODEL", "openai/gpt-5.2")

if EXPERIENCE_LIBRARY_PATH.exists():
    EXPERIENCE_LIBRARY = json.loads(EXPERIENCE_LIBRARY_PATH.read_text(encoding="utf-8"))
else:
    EXPERIENCE_LIBRARY = {"skills": [], "action_profiles": []}

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


class DecideRequest(BaseModel):
    session_id: str
    timestamp_ms: int
    current_goal_id: str
    screen_w: int
    screen_h: int
    orientation: str = "landscape"
    history: list[HistoryItem] = Field(default_factory=list)
    screenshot_file_path: str | None = None


class DecideResponse(BaseModel):
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


SESSION_STORE: dict[str, SessionContext] = {}
LEARN_SESSION_BUFFER: dict[str, list[dict]] = {}
LEARN_QUEUE_LOCK = asyncio.Lock()

app = FastAPI(title="New Auto Gateway", version="0.3.0")


@app.get("/health")
def health() -> dict:
    pending = len([t for t in LEARN_QUEUE if t.get("status") in {"pending", "failed"}])
    return {
        "ok": True,
        "model": MODEL,
        "skills": len(EXPERIENCE_LIBRARY.get("skills", [])),
        "action_profiles": len(EXPERIENCE_LIBRARY.get("action_profiles", [])),
        "learn_queue_pending": pending,
    }


@app.get("/experience")
def experience() -> dict:
    return EXPERIENCE_LIBRARY


@app.get("/learn/queue")
def learn_queue() -> dict:
    return {"items": LEARN_QUEUE[-100:]}


@app.post("/learn")
async def learn(request: Request) -> dict:
    request_id = uuid.uuid4().hex[:12]
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
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "learn_parse_failed",
                "error_message": f"invalid learning fields: {exc}",
                "request_id": request_id,
            },
        ) from exc

    if before_file is None or after_file is None:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "learn_missing_screenshots",
                "error_message": "before_file and after_file are required",
                "request_id": request_id,
            },
        )

    before_name = TMP_DIR / sanitize_filename(f"learn-before-{request_id}.png")
    after_name = TMP_DIR / sanitize_filename(f"learn-after-{request_id}.png")
    before_name.write_bytes(await before_file.read())
    after_name.write_bytes(await after_file.read())

    task = {
        "task_id": request_id,
        "status": "pending",
        "retry_count": 0,
        "created_at_ms": int(time.time() * 1000),
        "updated_at_ms": int(time.time() * 1000),
        "request": {
            "session_id": session_id,
            "goal_id": goal_id,
            "description": description,
            "action_type": action_type,
            "intent": intent,
            "skill_tags": skill_tags,
            "scene_tags": scene_tags,
            "x": max(0, x),
            "y": max(0, y),
            "wait_ms": clamp(wait_ms, 300, 5000),
            "sequence_done": sequence_done,
            "before_path": str(before_name),
            "after_path": str(after_name),
        },
    }

    LEARN_QUEUE.append(task)
    persist_learn_queue()
    await process_learn_queue()

    saved = next((x for x in LEARN_QUEUE if x.get("task_id") == request_id), task)
    return {
        "ok": saved.get("status") == "done",
        "queued": True,
        "task_id": request_id,
        "status": saved.get("status", "pending"),
        "message": saved.get("message", "learn task queued"),
    }


@app.post("/decide", response_model=DecideResponse)
async def decide(request: Request) -> DecideResponse:
    request_id = uuid.uuid4().hex[:12]
    started = time.time()
    try:
        req = await parse_decide_request(request)
        session = SESSION_STORE.setdefault(req.session_id, SessionContext())

        if req.orientation != "landscape" or req.screen_w <= req.screen_h:
            return fallback_wait(req.current_goal_id, "only_landscape_supported")

        update_profiles_from_history(req.current_goal_id, req.history)

        running_step = next_skill_step(req, session)
        if running_step is not None:
            response = running_step
        else:
            matched = trigger_skill(req, session)
            if matched is not None:
                response = matched
            elif should_call_opencode(req, session):
                payload = extract_json(call_opencode(build_user_prompt(req), req.screenshot_file_path))
                response = normalize_payload(payload, req)
                maybe_activate_skill(payload, session)
                session.model_cooldown_until_ms = int(time.time() * 1000) + 1000
            else:
                response = smart_wait(req)

        elapsed_ms = int((time.time() - started) * 1000)
        print(
            f"request_id={request_id} action={response.action} intent={response.intent} x={response.x} y={response.y} "
            f"wait={response.wait_ms} skill={response.skill_id} step={response.step_index} elapsed_ms={elapsed_ms}"
        )
        return response
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, dict) else {
            "error_code": "http_exception",
            "error_message": str(e.detail),
        }
        detail["request_id"] = request_id
        return JSONResponse(status_code=e.status_code, content={"detail": detail})
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "detail": {
                    "error_code": "internal_error",
                    "error_message": str(e),
                    "request_id": request_id,
                }
            },
        )


async def process_learn_queue() -> None:
    async with LEARN_QUEUE_LOCK:
        changed = False
        for item in LEARN_QUEUE:
            if item.get("status") not in {"pending", "failed"}:
                continue
            if int(item.get("retry_count", 0)) >= 3:
                continue

            item["status"] = "sending"
            item["updated_at_ms"] = int(time.time() * 1000)
            changed = True
            try:
                message = process_learn_task(item)
                item["status"] = "done"
                item["message"] = message
                item["last_error"] = ""
            except Exception as exc:
                item["status"] = "failed"
                item["retry_count"] = int(item.get("retry_count", 0)) + 1
                item["last_error"] = str(exc)
            item["updated_at_ms"] = int(time.time() * 1000)
            changed = True

        if changed:
            persist_learn_queue()
            persist_experience_library()


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
        "reason": description or "manual_learned_step",
        "tags": sorted(set(skill_tags + scene_tags + [intent])),
    }

    demo = {
        "task_id": task.get("task_id", ""),
        "session_id": session_id,
        "goal_id": goal_id,
        "description": description,
        "step": step,
        "before_path": str(req.get("before_path", "")),
        "after_path": str(req.get("after_path", "")),
        "skill_tags": skill_tags,
        "scene_tags": scene_tags,
        "sequence_done": sequence_done,
        "timestamp_ms": int(time.time() * 1000),
    }

    with LEARN_DEMOS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(demo, ensure_ascii=False) + "\n")

    LEARN_SESSION_BUFFER.setdefault(session_id, []).append(step)
    upsert_action_profile(goal_id, step, success=True, effect="changed")

    if not sequence_done:
        return "learn step appended"

    steps = LEARN_SESSION_BUFFER.pop(session_id, [])
    if not steps:
        return "sequence_done with empty buffer"

    all_step_tags: list[str] = []
    all_intents: list[str] = []
    for s in steps:
        all_step_tags.extend(parse_tags_list(s.get("tags", [])))
        all_intents.append(str(s.get("intent", "observe_state")))

    merged_skill_tags = sorted(set(skill_tags + scene_tags + all_step_tags + [goal_id]))
    merged_scene_tags = sorted(set(scene_tags + extract_keywords(description)))
    trigger_intents = sorted(set(all_intents))[:5]

    skill_id = f"manual_{goal_id}_{int(time.time())}"
    skill = {
        "skill_id": skill_id,
        "goal_id": goal_id,
        "description": description or "manual learned sequence",
        "skill_tags": merged_skill_tags,
        "precondition": {
            "trigger_intents": trigger_intents,
            "scene_tags": merged_scene_tags,
            "description_keywords": extract_keywords(description),
        },
        "steps": steps,
        "stats": {
            "success": 0,
            "failed": 0,
            "updated_at_ms": int(time.time() * 1000),
        },
    }

    EXPERIENCE_LIBRARY.setdefault("skills", []).append(skill)
    return f"learned sequence saved as {skill_id}"


def next_skill_step(req: DecideRequest, session: SessionContext) -> DecideResponse | None:
    if not session.active_steps:
        return None
    step = session.active_steps.popleft()
    idx = session.active_step_index
    session.active_step_index += 1
    return DecideResponse(
        action=step.get("action", "wait"),
        intent=step.get("intent", "observe_state"),
        x=int(step.get("x", 0)),
        y=int(step.get("y", 0)),
        wait_ms=clamp(int(step.get("wait_ms", 1200)), 300, 5000),
        goal_id=req.current_goal_id,
        reason=str(step.get("reason", "skill_step")),
        skill_id=session.active_skill_id,
        step_index=idx,
    )


def trigger_skill(req: DecideRequest, session: SessionContext) -> DecideResponse | None:
    if not req.history:
        return None

    last = req.history[-1]
    scored: list[tuple[int, dict]] = []
    for skill in EXPERIENCE_LIBRARY.get("skills", []):
        if skill.get("goal_id") != req.current_goal_id:
            continue
        score = score_skill_match(skill, last)
        if score > 0:
            scored.append((score, skill))

    if not scored:
        return None

    scored.sort(key=lambda x: x[0], reverse=True)
    best = scored[0][1]
    session.active_skill_id = str(best.get("skill_id", ""))
    session.active_steps = deque(best.get("steps", []))
    session.active_step_index = 0
    return next_skill_step(req, session)


def score_skill_match(skill: dict, last: HistoryItem) -> int:
    score = 0
    pre = skill.get("precondition", {})
    trigger_intents = parse_tags_list(pre.get("trigger_intents", []))
    scene_tags = parse_tags_list(pre.get("scene_tags", []))
    keywords = parse_tags_list(pre.get("description_keywords", []))
    skill_tags = parse_tags_list(skill.get("skill_tags", []))

    if trigger_intents and last.intent in trigger_intents:
        score += 4
    if scene_tags and any(tag in last.reason for tag in scene_tags):
        score += 3
    if keywords and any(k in last.reason for k in keywords):
        score += 2
    if skill_tags and (last.intent in skill_tags or any(t in last.reason for t in skill_tags)):
        score += 1
    return score


def should_call_opencode(req: DecideRequest, session: SessionContext) -> bool:
    now_ms = int(time.time() * 1000)
    if now_ms < session.model_cooldown_until_ms:
        return False

    if not req.history:
        return True

    last = req.history[-1]
    due_ms = last.timestamp_ms + clamp(last.wait_ms, 300, 5000)
    if now_ms < due_ms:
        return False

    if len(req.history) >= 2 and req.history[-1].effect == "no_change" and req.history[-2].effect == "no_change":
        return True

    if last.action == "wait":
        return True

    return last.result != "ok"


def smart_wait(req: DecideRequest) -> DecideResponse:
    if req.history and req.history[-1].effect == "no_change":
        wait_ms = clamp(req.history[-1].wait_ms + 300, 300, 5000)
        return fallback_wait(req.current_goal_id, "await_ui_settle", wait_ms)
    return fallback_wait(req.current_goal_id, "rule_wait", 900)


def maybe_activate_skill(payload: dict, session: SessionContext) -> None:
    next_steps = payload.get("next_steps", [])
    skill_id = str(payload.get("skill_id", ""))
    if isinstance(next_steps, list) and next_steps:
        session.active_steps = deque(next_steps)
        session.active_skill_id = skill_id or "runtime_sequence"
        session.active_step_index = 0


def normalize_payload(payload: dict, req: DecideRequest) -> DecideResponse:
    action = str(payload.get("action", "wait"))
    if action not in {"click", "wait"}:
        action = "wait"

    x = int(payload.get("x", 0))
    y = int(payload.get("y", 0))
    if action == "wait":
        x = 0
        y = 0

    return DecideResponse(
        action=action,
        intent=str(payload.get("intent", "observe_state")),
        x=clamp(x, 0, max(0, req.screen_w - 1)),
        y=clamp(y, 0, max(0, req.screen_h - 1)),
        wait_ms=clamp(int(payload.get("wait_ms", 1000)), 300, 5000),
        goal_id=str(payload.get("goal_id", req.current_goal_id)),
        reason=str(payload.get("reason", "model_decision")),
        skill_id=str(payload.get("skill_id", "")),
        step_index=int(payload.get("step_index", -1)),
    )


def update_profiles_from_history(goal_id: str, history: list[HistoryItem]) -> None:
    if not history:
        return
    last = history[-1]
    if last.action not in {"click", "wait"}:
        return
    upsert_action_profile(
        goal_id,
        {
            "intent": last.intent,
            "x": last.x,
            "y": last.y,
            "wait_ms": last.wait_ms,
            "tags": [last.intent],
        },
        success=last.result == "ok",
        effect=last.effect,
    )


def upsert_action_profile(goal_id: str, step: dict, success: bool, effect: str) -> None:
    key = {
        "goal_id": goal_id,
        "intent": str(step.get("intent", "observe_state")),
        "x": int(step.get("x", 0)),
        "y": int(step.get("y", 0)),
    }
    profiles = EXPERIENCE_LIBRARY.setdefault("action_profiles", [])
    profile = next(
        (
            p
            for p in profiles
            if p.get("goal_id") == key["goal_id"]
            and p.get("intent") == key["intent"]
            and p.get("x") == key["x"]
            and p.get("y") == key["y"]
        ),
        None,
    )
    if profile is None:
        profile = {
            **key,
            "tags": parse_tags_list(step.get("tags", [])),
            "recommended_wait_ms": clamp(int(step.get("wait_ms", 1000)), 300, 5000),
            "success_count": 0,
            "fail_count": 0,
            "no_change_count": 0,
            "updated_at_ms": int(time.time() * 1000),
        }
        profiles.append(profile)

    if success:
        profile["success_count"] += 1
    else:
        profile["fail_count"] += 1
    if effect == "no_change":
        profile["no_change_count"] += 1

    old_wait = int(profile.get("recommended_wait_ms", 1000))
    target = int(step.get("wait_ms", old_wait))
    if effect == "no_change":
        target += 300
    new_wait = int(old_wait * 0.8 + clamp(target, 300, 5000) * 0.2)
    profile["recommended_wait_ms"] = clamp(new_wait, 300, 5000)
    profile["updated_at_ms"] = int(time.time() * 1000)


def parse_history_json(raw: str) -> list[HistoryItem]:
    data = json.loads(raw)
    if not isinstance(data, list):
        return []
    items: list[HistoryItem] = []
    for row in data[-8:]:
        if isinstance(row, dict):
            items.append(HistoryItem(**row))
    return items


async def parse_decide_request(request: Request) -> DecideRequest:
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        body = await request.json()
        return DecideRequest(**body)

    form = await request.form()
    try:
        session_id = str(form.get("session_id", "device-local"))
        timestamp_ms = int(form.get("timestamp_ms", "0"))
        current_goal_id = str(form.get("current_goal_id", "daily_loop"))
        screen_w = int(form.get("screen_w", "0"))
        screen_h = int(form.get("screen_h", "0"))
        orientation = str(form.get("orientation", "landscape"))
        history_json = str(form.get("history_json", "[]"))
        screenshot_file = form.get("screenshot_file")
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail={"error_code": "multipart_parse_failed", "error_message": f"invalid multipart fields: {exc}"},
        ) from exc

    if screenshot_file is None:
        raise HTTPException(
            status_code=422,
            detail={"error_code": "missing_screenshot_file", "error_message": "missing screenshot_file"},
        )

    filename = sanitize_filename(f"frame-{session_id}-{timestamp_ms}.png")
    frame_path = TMP_DIR / filename
    frame_path.write_bytes(await screenshot_file.read())

    return DecideRequest(
        session_id=session_id,
        timestamp_ms=timestamp_ms,
        current_goal_id=current_goal_id,
        screen_w=screen_w,
        screen_h=screen_h,
        orientation=orientation,
        history=parse_history_json(history_json),
        screenshot_file_path=str(frame_path),
    )


def build_user_prompt(req: DecideRequest) -> str:
    skills = EXPERIENCE_LIBRARY.get("skills", [])[:8]
    profiles = EXPERIENCE_LIBRARY.get("action_profiles", [])[-30:]
    data = {
        "task": "根据截图选择下一步搬砖动作，目标是稳定提高银两收益",
        "output_schema": {
            "action": "click|wait",
            "intent": "toggle_auto_battle|tap_skip|open_role|add_point|close_panel|observe_state|other",
            "x": "pixel_x",
            "y": "pixel_y",
            "wait_ms": "300-5000",
            "goal_id": req.current_goal_id,
            "reason": "short_cn_reason",
            "skill_id": "optional",
            "step_index": "optional",
            "next_steps": "optional array of click/wait steps",
        },
        "screen": {"width": req.screen_w, "height": req.screen_h, "orientation": req.orientation},
        "goal_id": req.current_goal_id,
        "history": [h.model_dump() for h in req.history[-6:]],
        "skills": skills,
        "action_profiles": profiles,
        "rules": [
            "只输出一个JSON对象",
            "不确定时返回wait",
            "战斗中优先尝试toggle_auto_battle",
            "看到跳过按钮优先tap_skip",
            "禁止输出置信度字段",
        ],
    }
    return json.dumps(data, ensure_ascii=False)


def call_opencode(user_prompt: str, screenshot_file_path: str | None) -> str:
    combined_prompt = (
        "[SYSTEM_RULES]\n"
        f"{SYSTEM_PROMPT}\n\n"
        "[USER_CONTEXT]\n"
        f"{user_prompt}\n\n"
        "只输出一个JSON对象。"
    )
    cmd = ["opencode", "run", "--model", MODEL]
    if screenshot_file_path:
        cmd += ["--file", screenshot_file_path]

    try:
        result = subprocess.run(
            cmd,
            input=combined_prompt,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(
            status_code=503,
            detail={"error_code": "opencode_timeout", "error_message": f"opencode timeout after {int(exc.timeout)}s"},
        ) from exc
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error_code": "opencode_not_found", "error_message": str(exc)},
        ) from exc

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if result.returncode == 0 and stdout:
        return stdout

    raise HTTPException(
        status_code=503,
        detail={"error_code": "opencode_failed", "error_message": stderr[:300] or "empty model output"},
    )


def extract_json(raw: str) -> dict:
    text = raw.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise HTTPException(
            status_code=422,
            detail={"error_code": "model_json_missing", "error_message": "model output has no json object"},
        )
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error_code": "model_json_invalid", "error_message": str(exc)},
        ) from exc


def fallback_wait(goal_id: str, reason: str, wait_ms: int = 1000) -> DecideResponse:
    return DecideResponse(
        action="wait",
        intent="observe_state",
        x=0,
        y=0,
        wait_ms=clamp(wait_ms, 300, 5000),
        goal_id=goal_id,
        reason=reason,
        skill_id="",
        step_index=-1,
    )


def parse_tags(raw: str) -> list[str]:
    if not raw:
        return []
    cleaned = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
    return sorted(set(cleaned))[:16]


def parse_tags_list(raw: object) -> list[str]:
    if isinstance(raw, list):
        return sorted(set([str(x).strip() for x in raw if str(x).strip()]))[:16]
    if isinstance(raw, str):
        return parse_tags(raw)
    return []


def sanitize_filename(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in {"-", "_", "."})


def extract_keywords(text: str) -> list[str]:
    cleaned = [t.strip() for t in text.replace("，", " ").replace(",", " ").split() if t.strip()]
    return cleaned[:6]


def clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def persist_experience_library() -> None:
    EXPERIENCE_LIBRARY_PATH.write_text(
        json.dumps(EXPERIENCE_LIBRARY, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def persist_learn_queue() -> None:
    LEARN_QUEUE_PATH.write_text(
        json.dumps(LEARN_QUEUE, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
