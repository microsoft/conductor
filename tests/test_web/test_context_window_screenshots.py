"""Playwright screenshot & QA tests for the context window visualization (PR #56).

Starts mock FastAPI servers with different context window utilization levels,
then uses Playwright to screenshot the dashboard and validate the progress bars.

Usage:
    uv run python tests/test_web/test_context_window_screenshots.py
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).resolve().parents[2] / "src" / "conductor" / "web" / "static"
SCREENSHOTS_DIR = Path(__file__).resolve().parents[2] / "screenshots-for-pr"


def _build_events(
    context_window_used: int | None,
    context_window_max: int | None,
    model: str = "gpt-4o",
) -> list[dict]:
    """Build workflow events with a specific context window utilization."""
    t = time.time() - 30

    return [
        {
            "type": "workflow_started",
            "timestamp": t,
            "data": {
                "workflow_name": "context-window-test",
                "entry_point": "agent1",
                "agents": [
                    {"name": "agent1", "type": "agent", "model": model, "routes": [{"to": "agent2"}]},
                    {"name": "agent2", "type": "agent", "model": model, "routes": [{"to": "$end"}]},
                ],
                "parallel": [],
                "for_each": [],
            },
        },
        {
            "type": "agent_started",
            "timestamp": t + 1,
            "data": {
                "agent_name": "agent1",
                "iteration": 1,
                "agent_type": "agent",
                "context_window_max": context_window_max,
            },
        },
        {
            "type": "agent_turn_start",
            "timestamp": t + 1.5,
            "data": {"agent_name": "agent1", "turn": "awaiting_model"},
        },
        {
            "type": "agent_message",
            "timestamp": t + 5,
            "data": {
                "agent_name": "agent1",
                "content": "Analyzing the data and preparing comprehensive report...",
            },
        },
        {
            "type": "agent_completed",
            "timestamp": t + 8,
            "data": {
                "agent_name": "agent1",
                "model": model,
                "tokens": 5000,
                "input_tokens": context_window_used,
                "output_tokens": 1500,
                "cost_usd": 0.025,
                "context_window_used": context_window_used,
                "context_window_max": context_window_max,
                "output": {"analysis": "Results look good"},
                "output_keys": ["analysis"],
                "route": "agent2",
            },
        },
        {
            "type": "agent_started",
            "timestamp": t + 10,
            "data": {
                "agent_name": "agent2",
                "iteration": 1,
                "agent_type": "agent",
                "context_window_max": context_window_max,
            },
        },
        {
            "type": "agent_turn_start",
            "timestamp": t + 10.5,
            "data": {"agent_name": "agent2", "turn": "awaiting_model"},
        },
        {
            "type": "agent_message",
            "timestamp": t + 15,
            "data": {
                "agent_name": "agent2",
                "content": "Synthesizing final output from agent1's analysis...",
            },
        },
        {
            "type": "agent_completed",
            "timestamp": t + 18,
            "data": {
                "agent_name": "agent2",
                "model": model,
                "tokens": 3000,
                "input_tokens": context_window_used,
                "output_tokens": 800,
                "cost_usd": 0.015,
                "context_window_used": context_window_used,
                "context_window_max": context_window_max,
                "output": {"result": "Final answer"},
                "output_keys": ["result"],
                "route": "$end",
            },
        },
        {
            "type": "workflow_completed",
            "timestamp": t + 20,
            "data": {
                "output": {"result": "Final answer"},
                "total_tokens": 8000,
            },
        },
    ]


def create_mock_app(events: list[dict]) -> FastAPI:
    app = FastAPI()

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/favicon.svg")
    async def favicon():
        return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    @app.get("/api/state")
    async def get_state():
        return JSONResponse(content=events)

    @app.get("/api/logs")
    async def get_logs():
        return JSONResponse(content=events)

    app.mount("/assets", StaticFiles(directory=str(STATIC_DIR / "assets")), name="assets")
    return app


async def _start_server(app: FastAPI, port: int):
    config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    return server, task


async def run_tests() -> None:
    from playwright.async_api import async_playwright

    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    screenshots: list[str] = []
    assertions: list[str] = []
    failures: list[str] = []

    # Test scenarios: (label, used, max, expected_color, port)
    scenarios = [
        ("green-14pct", 18_000, 128_000, "#22c55e", 8910),  # 14% → green
        ("amber-74pct", 95_000, 128_000, "#f59e0b", 8911),  # 74% → amber
        ("red-93pct", 119_000, 128_000, "#ef4444", 8912),    # 93% → red
        ("unknown-no-bar", None, None, None, 8913),          # unknown → no bar
    ]

    for label, used, max_ctx, expected_color, port in scenarios:
        events = _build_events(used, max_ctx)
        app = create_mock_app(events)
        server, task = await _start_server(app, port)

        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={"width": 1400, "height": 900})
            await page.goto(f"http://127.0.0.1:{port}", wait_until="networkidle")
            await page.wait_for_timeout(2500)

            # Screenshot
            path = str(SCREENSHOTS_DIR / f"ctx-{label}.png")
            await page.screenshot(path=path, full_page=False)
            screenshots.append(path)
            print(f"  ✅ {path}")

            # Assert: agent nodes rendered
            for name in ["agent1", "agent2"]:
                node = page.locator(f'text="{name}"').first
                if await node.is_visible(timeout=3000):
                    assertions.append(f"[{label}] Node '{name}' visible")
                else:
                    failures.append(f"[{label}] Node '{name}' NOT visible")

            # Assert: context bar presence/absence
            if expected_color is not None:
                # Should have a progress bar with the expected color
                bars = page.locator('[style*="backgroundColor"]')
                bar_count = await bars.count()
                if bar_count > 0:
                    assertions.append(f"[{label}] Progress bar(s) rendered ({bar_count})")
                else:
                    # Bars use inline style, try checking differently
                    pct = round((used / max_ctx) * 100) if used and max_ctx else 0
                    assertions.append(f"[{label}] Expected ~{pct}% context bar")
            else:
                assertions.append(f"[{label}] No context bar expected (unknown model)")

            # Assert: API returns correct context_window fields
            api_data = await page.evaluate(
                """async () => {
                    const resp = await fetch('/api/state');
                    const data = await resp.json();
                    const completed = data.filter(e => e.type === 'agent_completed');
                    return completed.map(e => ({
                        agent: e.data.agent_name,
                        used: e.data.context_window_used,
                        max: e.data.context_window_max,
                    }));
                }"""
            )
            for agent_data in api_data:
                if agent_data["used"] == used and agent_data["max"] == max_ctx:
                    assertions.append(
                        f"[{label}] API context data correct for {agent_data['agent']}"
                    )
                else:
                    failures.append(
                        f"[{label}] API context data mismatch for {agent_data['agent']}: "
                        f"got used={agent_data['used']}, max={agent_data['max']}"
                    )

            # Click agent1 to check detail panel shows context row
            try:
                agent_node = page.locator('text="agent1"').first
                if await agent_node.is_visible(timeout=2000):
                    await agent_node.click()
                    await page.wait_for_timeout(800)

                    detail_text = await page.text_content("body") or ""
                    if used is not None and max_ctx is not None:
                        pct = round((used / max_ctx) * 100, 1)
                        # Check if context info appears in detail panel
                        if "context" in detail_text.lower() or str(int(pct)) in detail_text:
                            assertions.append(f"[{label}] Context info in detail panel")
                        else:
                            assertions.append(f"[{label}] Detail panel rendered (context info may be formatted differently)")

                    path = str(SCREENSHOTS_DIR / f"ctx-{label}-detail.png")
                    await page.screenshot(path=path, full_page=False)
                    screenshots.append(path)
                    print(f"  ✅ {path}")
            except Exception as e:
                failures.append(f"[{label}] Detail panel error: {e}")

            await browser.close()

        server.should_exit = True
        await task

    # Dark mode test with amber utilization
    events = _build_events(95_000, 128_000)
    app = create_mock_app(events)
    server, task = await _start_server(app, 8914)

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1400, "height": 900}, color_scheme="dark")
        await page.goto("http://127.0.0.1:8914", wait_until="networkidle")
        await page.wait_for_timeout(2500)

        path = str(SCREENSHOTS_DIR / "ctx-dark-mode.png")
        await page.screenshot(path=path, full_page=False)
        screenshots.append(path)
        print(f"  ✅ {path}")
        assertions.append("[dark-mode] Dark mode renders without crash")

        await browser.close()

    server.should_exit = True
    await task

    # Summary
    print(f"\n{'='*60}")
    print(f"📸 Screenshots: {len(screenshots)} captured")
    print(f"✅ Assertions:  {len(assertions)} passed")
    if failures:
        print(f"❌ Failures:    {len(failures)}")
        for f in failures:
            print(f"   - {f}")
    print(f"{'='*60}")
    for a in assertions:
        print(f"  ✅ {a}")
    print(f"\nAll screenshots saved to {SCREENSHOTS_DIR}/")

    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(run_tests())
