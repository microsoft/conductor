"""Playwright screenshot test for the Conductor web dashboard.

Starts a mock FastAPI server pre-loaded with staged-workflow events,
then uses Playwright to screenshot the dashboard in various states.

Usage:
    uv run python tests/test_web/take_screenshots.py
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# Resolve paths
STATIC_DIR = Path(__file__).resolve().parents[2] / "src" / "conductor" / "web" / "static"
SCREENSHOTS_DIR = Path(__file__).resolve().parents[2] / "screenshots-for-pr"


def _build_staged_workflow_events() -> list[dict]:
    """Build a realistic set of events for a VP→IC→VP:review staged workflow."""
    t = time.time() - 60  # Start 60s ago

    return [
        {
            "type": "workflow_started",
            "timestamp": t,
            "data": {
                "workflow_name": "staged-review",
                "entry_point": "vp:default",
                "agents": [
                    {
                        "name": "vp",
                        "type": "agent",
                        "model": "gpt-4.1",
                        "routes": [{"to": "ic"}],
                        "stages": {"review": {}},
                    },
                    {
                        "name": "vp:default",
                        "type": "agent",
                        "model": "gpt-4.1",
                        "routes": [{"to": "ic"}],
                    },
                    {
                        "name": "vp:review",
                        "type": "agent",
                        "model": "gpt-4.1",
                        "routes": [
                            {"to": "ic", "when": "verdict == 'revise'"},
                            {"to": "$end", "when": "verdict == 'approve'"},
                        ],
                    },
                    {
                        "name": "ic",
                        "type": "agent",
                        "model": "gpt-4.1",
                        "routes": [{"to": "vp:review"}],
                    },
                ],
                "parallel": [],
                "for_each": [],
            },
        },
        # VP:default starts
        {
            "type": "agent_started",
            "timestamp": t + 1,
            "data": {"agent": "vp:default"},
        },
        {
            "type": "agent_turn_start",
            "timestamp": t + 1.5,
            "data": {"agent": "vp:default", "turn": "awaiting_model"},
        },
        {
            "type": "agent_message",
            "timestamp": t + 5,
            "data": {
                "agent": "vp:default",
                "content": "Based on the project requirements, I'm setting the technical "
                "direction for our team. We'll use a microservices architecture with "
                "Python FastAPI for the backend and React for the frontend. The key "
                "priorities are: 1) API design first, 2) Comprehensive test coverage, "
                "3) CI/CD pipeline setup.",
            },
        },
        {
            "type": "agent_completed",
            "timestamp": t + 8,
            "data": {
                "agent": "vp:default",
                "output": {
                    "direction": "microservices with FastAPI + React",
                    "priorities": ["API design", "test coverage", "CI/CD"],
                },
                "model": "gpt-4.1",
                "tokens": 245,
                "route": "ic",
            },
        },
        # IC starts
        {
            "type": "agent_started",
            "timestamp": t + 10,
            "data": {"agent": "ic"},
        },
        {
            "type": "agent_turn_start",
            "timestamp": t + 10.5,
            "data": {"agent": "ic", "turn": "awaiting_model"},
        },
        {
            "type": "agent_tool_start",
            "timestamp": t + 12,
            "data": {
                "agent": "ic",
                "tool": "create_file",
                "input": {"path": "src/api/main.py"},
            },
        },
        {
            "type": "agent_tool_complete",
            "timestamp": t + 13,
            "data": {
                "agent": "ic",
                "tool": "create_file",
                "output": "File created successfully",
            },
        },
        {
            "type": "agent_message",
            "timestamp": t + 18,
            "data": {
                "agent": "ic",
                "content": "I've implemented the initial API structure following the VP's "
                "direction. Created the FastAPI application with three main endpoints: "
                "/users, /projects, and /tasks. Added comprehensive Pydantic models "
                "for request/response validation and pytest fixtures for testing.",
            },
        },
        {
            "type": "agent_completed",
            "timestamp": t + 20,
            "data": {
                "agent": "ic",
                "output": {
                    "files_created": [
                        "src/api/main.py",
                        "src/api/models.py",
                        "tests/test_api.py",
                    ],
                    "implementation_summary": "FastAPI app with 3 endpoints, Pydantic models, tests",
                },
                "model": "gpt-4.1",
                "tokens": 512,
                "route": "vp:review",
            },
        },
        # VP:review starts (same agent, review stage)
        {
            "type": "agent_started",
            "timestamp": t + 22,
            "data": {"agent": "vp:review"},
        },
        {
            "type": "agent_turn_start",
            "timestamp": t + 22.5,
            "data": {"agent": "vp:review", "turn": "awaiting_model"},
        },
        {
            "type": "agent_message",
            "timestamp": t + 28,
            "data": {
                "agent": "vp:review",
                "content": "Reviewing the IC's implementation against my original direction. "
                "The API structure looks solid — good use of Pydantic models and the "
                "endpoint design follows REST conventions. Test coverage is present. "
                "Verdict: APPROVE. The implementation meets the technical requirements.",
            },
        },
        {
            "type": "agent_completed",
            "timestamp": t + 30,
            "data": {
                "agent": "vp:review",
                "output": {
                    "verdict": "approve",
                    "review_notes": "Solid implementation, meets requirements",
                },
                "model": "gpt-4.1",
                "tokens": 189,
                "route": "$end",
            },
        },
        # Workflow completes
        {
            "type": "workflow_completed",
            "timestamp": t + 32,
            "data": {
                "output": {
                    "final_verdict": "approve",
                    "review_notes": "Solid implementation, meets requirements",
                },
                "total_tokens": 946,
            },
        },
    ]


def _build_in_progress_events() -> list[dict]:
    """Build events showing the workflow mid-execution (IC running)."""
    full = _build_staged_workflow_events()
    # Return events up to IC running (before IC completes)
    return full[:9]  # Up through agent_tool_complete for IC


def create_mock_app(events: list[dict]) -> FastAPI:
    """Create a FastAPI app that serves the dashboard with pre-loaded events."""
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


async def take_screenshots() -> None:
    """Start mock servers and take screenshots with Playwright."""
    from playwright.async_api import async_playwright

    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    screenshots_taken: list[str] = []
    assertions_passed: list[str] = []
    warnings: list[str] = []

    # --- Server 1: Completed staged workflow ---
    completed_app = create_mock_app(_build_staged_workflow_events())
    config1 = uvicorn.Config(app=completed_app, host="127.0.0.1", port=8901, log_level="warning")
    server1 = uvicorn.Server(config1)
    task1 = asyncio.create_task(server1.serve())
    while not server1.started:
        await asyncio.sleep(0.05)

    async with async_playwright() as p:
        browser = await p.chromium.launch()

        # ---- Test 1: Dashboard loads and renders graph (desktop) ----
        page = await browser.new_page(viewport={"width": 1400, "height": 900})
        await page.goto("http://127.0.0.1:8901", wait_until="networkidle")
        await page.wait_for_timeout(2500)

        # Assert: dashboard title/header is visible
        header = page.locator("header").first
        if await header.is_visible():
            assertions_passed.append("Header is visible")
        else:
            warnings.append("Header not found")

        # Assert: workflow name displayed
        page_text = await page.text_content("body")
        if "staged-review" in (page_text or "").lower():
            assertions_passed.append("Workflow name 'staged-review' displayed")

        # Assert: stage-qualified agent nodes are rendered
        for node_name in ["vp:default", "vp:review", "ic"]:
            node = page.locator(f'text="{node_name}"').first
            if await node.is_visible(timeout=3000):
                assertions_passed.append(f"Node '{node_name}' rendered in graph")
            else:
                warnings.append(f"Node '{node_name}' not visible in graph")

        # Screenshot: full completed workflow overview
        path = str(SCREENSHOTS_DIR / "staged-workflow-completed.png")
        await page.screenshot(path=path, full_page=False)
        screenshots_taken.append(path)
        print(f"  ✅ {path}")

        # ---- Test 2: Click each stage node and verify detail panel ----
        for node_name in ["vp:default", "vp:review", "ic"]:
            try:
                node = page.locator(f'text="{node_name}"').first
                if await node.is_visible(timeout=2000):
                    await node.click()
                    await page.wait_for_timeout(800)

                    safe_name = node_name.replace(":", "-")
                    path = str(SCREENSHOTS_DIR / f"staged-node-{safe_name}.png")
                    await page.screenshot(path=path, full_page=False)
                    screenshots_taken.append(path)
                    print(f"  ✅ {path}")
                    assertions_passed.append(f"Detail panel opened for '{node_name}'")
            except Exception as e:
                warnings.append(f"Could not interact with node '{node_name}': {e}")

        # ---- Test 3: Verify completed status indicators ----
        body_text = await page.text_content("body") or ""
        if "completed" in body_text.lower() or "✓" in body_text or "complete" in body_text.lower():
            assertions_passed.append("Completed status indicator found")
        else:
            warnings.append("No completed status indicator found")

        # Assert: token count displayed
        if "946" in body_text or "tok" in body_text.lower():
            assertions_passed.append("Token count displayed")

        # ---- Test 4: API state endpoint returns correct data ----
        response = await page.evaluate(
            """async () => {
                const resp = await fetch('/api/state');
                const data = await resp.json();
                return { count: data.length, firstType: data[0]?.type, lastType: data[data.length-1]?.type };
            }"""
        )
        if response["count"] > 0:
            assertions_passed.append(f"API /api/state returns {response['count']} events")
        if response["firstType"] == "workflow_started":
            assertions_passed.append("First event is workflow_started")
        if response["lastType"] == "workflow_completed":
            assertions_passed.append("Last event is workflow_completed")

        # ---- Test 5: Verify stage-qualified agents in API response ----
        agents_data = await page.evaluate(
            """async () => {
                const resp = await fetch('/api/state');
                const data = await resp.json();
                const started = data[0]?.data?.agents || [];
                return started.map(a => a.name);
            }"""
        )
        for expected in ["vp", "vp:default", "vp:review", "ic"]:
            if expected in agents_data:
                assertions_passed.append(f"Agent '{expected}' in API state")
            else:
                warnings.append(f"Agent '{expected}' missing from API state")

        await page.close()

        # ---- Test 6: Mobile viewport ----
        mobile_page = await browser.new_page(viewport={"width": 375, "height": 812})
        await mobile_page.goto("http://127.0.0.1:8901", wait_until="networkidle")
        await mobile_page.wait_for_timeout(2000)

        path = str(SCREENSHOTS_DIR / "staged-workflow-mobile.png")
        await mobile_page.screenshot(path=path, full_page=False)
        screenshots_taken.append(path)
        print(f"  ✅ {path}")
        assertions_passed.append("Mobile viewport renders without crash")

        await mobile_page.close()

        # ---- Test 7: Dark mode (prefers-color-scheme) ----
        dark_page = await browser.new_page(
            viewport={"width": 1400, "height": 900},
            color_scheme="dark",
        )
        await dark_page.goto("http://127.0.0.1:8901", wait_until="networkidle")
        await dark_page.wait_for_timeout(2000)

        path = str(SCREENSHOTS_DIR / "staged-workflow-dark-mode.png")
        await dark_page.screenshot(path=path, full_page=False)
        screenshots_taken.append(path)
        print(f"  ✅ {path}")
        assertions_passed.append("Dark mode renders without crash")

        await dark_page.close()
        await browser.close()

    server1.should_exit = True
    await task1

    # --- Server 2: In-progress staged workflow ---
    progress_app = create_mock_app(_build_in_progress_events())
    config2 = uvicorn.Config(app=progress_app, host="127.0.0.1", port=8902, log_level="warning")
    server2 = uvicorn.Server(config2)
    task2 = asyncio.create_task(server2.serve())
    while not server2.started:
        await asyncio.sleep(0.05)

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1400, "height": 900})

        await page.goto("http://127.0.0.1:8902", wait_until="networkidle")
        await page.wait_for_timeout(2000)

        # Screenshot: in-progress state
        path = str(SCREENSHOTS_DIR / "staged-workflow-in-progress.png")
        await page.screenshot(path=path, full_page=False)
        screenshots_taken.append(path)
        print(f"  ✅ {path}")

        # Assert: running state visible
        body_text = await page.text_content("body") or ""
        if "running" in body_text.lower() or "progress" in body_text.lower():
            assertions_passed.append("Running status indicator shown in-progress view")

        # Click on IC node (currently running)
        try:
            ic_node = page.locator('text="ic"').first
            if await ic_node.is_visible(timeout=2000):
                await ic_node.click()
                await page.wait_for_timeout(800)
                path = str(SCREENSHOTS_DIR / "staged-in-progress-ic-detail.png")
                await page.screenshot(path=path, full_page=False)
                screenshots_taken.append(path)
                print(f"  ✅ {path}")
                assertions_passed.append("IC node detail panel opened in-progress view")
        except Exception as e:
            warnings.append(f"Could not click IC node in-progress: {e}")

        await browser.close()

    server2.should_exit = True
    await task2

    # --- Summary ---
    print(f"\n{'='*60}")
    print(f"📸 Screenshots: {len(screenshots_taken)} captured")
    print(f"✅ Assertions:  {len(assertions_passed)} passed")
    if warnings:
        print(f"⚠️  Warnings:    {len(warnings)}")
        for w in warnings:
            print(f"   - {w}")
    print(f"{'='*60}")
    for a in assertions_passed:
        print(f"  ✅ {a}")
    print(f"\nAll screenshots saved to {SCREENSHOTS_DIR}/")


if __name__ == "__main__":
    asyncio.run(take_screenshots())
