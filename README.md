# New Auto V2

This repo now uses only the V2 framework.

- Keep: accessibility execution (`AutomationAccessibilityService`)
- Keep: floating control + learning submit
- Use: `gateway_v2` only
- Removed: old `gateway` framework

## V2 Rules

- Page id is rule-generated from actionable button distribution.
- Actionable button means: `clickable=true && enabled=true`.
- If actionable list is empty, use level2 candidate nodes (`enabled=true`, non-clickable leaf views with valid area) as fallback.
- `focusable` is ignored for game decision.
- Decision output only: `click` or `wait`.

## App Flow

1. Accessibility service dumps actionable nodes.
2. App captures screenshot (overlay hidden during capture).
3. App sends `/decide_v2` with screenshot + actionable node list + recent history.
4. Gateway returns `click/wait` and app executes with accessibility tap.

Debug probe:

- Floating button `测试:原始UI分析` sends raw nodes + xml-like + screenshot to `/v2/debug_probe`.

## Learning Flow

1. Use floating panel learning actions (`前截图/后截图/提交步骤/结束序列`).
2. App sends `/learn_v2` with tags and coordinates.
3. Gateway queues tasks in `gateway_v2/learn_queue.json`.
4. Completed sequence becomes tagged skill in `gateway_v2/skill_library.json`.

## Gateway V2

```bash
cd gateway_v2
pip install -r requirements.txt
./start_gateway_v2.sh
```

Health:

```bash
cd gateway_v2
./check_gateway_v2.sh
```

Stop:

```bash
cd gateway_v2
./stop_gateway_v2.sh
```

## XML Analysis API

- `POST /v2/analyze_xml`
  - input: `xml_file`, `screen_w`, `screen_h`, `orientation`, optional `screenshot_file`
  - output: `page_id`, actionable button list, optional cropped button images

## Build APK (GitHub Actions)

Workflow file: `.github/workflows/android-apk.yml`

Artifact: `new-auto-debug-apk`
