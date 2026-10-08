"""Test-only model provider installed in a real runner container."""

import asyncio
from pathlib import Path

from conductor.aca_runner import server
from conductor.providers.base import AgentOutput


class FixtureProvider:
    overlapping = asyncio.Event()
    active = 0

    def __init__(self, **_settings):
        pass

    async def execute(self, agent, context, prompt, **kwargs):
        callback = kwargs["event_callback"]
        callback("agent_message", {"content": "fixture-event"})
        if prompt.startswith("overlap:"):
            type(self).active += 1
            if self.active == 2:
                self.overlapping.set()
            try:
                await asyncio.wait_for(self.overlapping.wait(), 15)
            finally:
                type(self).active -= 1
            return AgentOutput(content={"answer": "overlapped"}, raw_response=None)
        if prompt.startswith("interrupt:"):
            signal = kwargs["interrupt_signal"]
            await asyncio.wait_for(signal.wait(), 15)
            return AgentOutput(content={"answer": "interrupted"}, raw_response=None, partial=True)
        skill_paths = kwargs.get("skill_directories") or []
        skill = (Path(skill_paths[0]) / "SKILL.md").read_text() if skill_paths else ""
        artifact = Path("/workspace/main/artifact.txt")
        value = artifact.read_text().strip() if artifact.exists() else ""
        return AgentOutput(content={"answer": f"{value}|{skill.strip()}"}, raw_response=None)

    async def close(self):
        pass


server.CopilotProvider = FixtureProvider
server.OpenAIProvider = FixtureProvider
server.ClaudeProvider = FixtureProvider
