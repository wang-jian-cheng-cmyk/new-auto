# New Auto (Accessibility + Learning + Gateway Decide)

This project now includes three capabilities:

- Accessibility execution chain (`click`/`wait` execution on device)
- Learning mode (`/learn` with before/after screenshots)
- Gateway decision loop (`/decide` with `opencode run --file`)

## Main modules

- `app/src/main/java/com/example/newauto/AutomationAccessibilityService.kt`
- `app/src/main/java/com/example/newauto/FloatingControlService.kt`
- `app/src/main/java/com/example/newauto/ScreenCaptureManager.kt`
- `app/src/main/java/com/example/newauto/LearningClient.kt`
- `app/src/main/java/com/example/newauto/DecisionClient.kt`
- `app/src/main/java/com/example/newauto/AutomationEngine.kt`

Gateway side:

- `gateway/main.py`
- `gateway/system_prompt.txt`
- `gateway/experience_library.json`

## Run flow

1. Open app and grant overlay permission.
2. Open accessibility settings and enable `New Auto` service.
3. Grant screen capture permission.
4. Start floating window.

In floating panel:

- Use `开始自动 / 暂停自动` to run decision loop.
- Use `测试点击` to validate accessibility execution.
- Use `查看屏幕大小` to read current resolution and orientation.
- Turn on learning mode.
- Record `学:记录前截图` and `学:记录后截图` around your manual operation.
- Submit by `学:提交步骤` or finish sequence by `学:结束序列`.
- Learning submit now uses queue persistence and tag fields (`skill_tags`, `scene_tags`).

Gateway endpoint defaults to `http://127.0.0.1:8787` (`/decide`, `/learn`).

Learning queue endpoint:

- `GET /learn/queue`

## Gateway run

```bash
cd gateway
pip install -r requirements.txt
./start_gateway_real.sh
```

Check health:

```bash
cd gateway
./check_gateway.sh
```

Stop:

```bash
cd gateway
./stop_gateway.sh
```

## Decision protocol

Gateway returns only:

- `action`: `click` or `wait`
- `intent`: semantic intent (`tap_skip`, `toggle_auto_battle`, etc.)
- `x`, `y`: pixel coordinates
- `wait_ms`: delay before next frame

No confidence field is used.

## Skill trigger strategy

The gateway uses `Rule -> Skill -> LLM Fallback`:

1. If active skill sequence exists, execute next skill step.
2. Else try matching saved skill preconditions.
3. Else call `opencode run --file <frame>` only when needed (cooldown and wait-window aware).

Learned sequences are persisted in `gateway/experience_library.json`.

## Build APK on GitHub Actions

This project includes CI build workflow:

- `.github/workflows/android-apk.yml`

How to use:

1. Push this project to your GitHub repository.
2. Open `Actions` tab.
3. Run workflow `Build Android APK` (or trigger by push).
4. Download artifact `new-auto-debug-apk`.
