"""Test-only model provider installed in a real runner container."""

import asyncio
import os
from contextlib import suppress
from pathlib import Path

from conductor.aca_runner import server
from conductor.providers.base import AgentOutput


class FixtureProvider:
    def __init__(self, **_settings):
        pass

    async def execute(self, agent, context, prompt, **kwargs):
        callback = kwargs["event_callback"]
        callback("agent_message", {"content": "fixture-event"})
        if prompt.startswith("overlap:"):
            # A FIFO on the shared volume requires both runner processes to arrive.
            def meet_other_runner():
                fifo = Path("/workspace/main/overlap.fifo")
                with suppress(FileExistsError):
                    os.mkfifo(fifo)
                if prompt == "overlap: a":
                    with fifo.open("rb", buffering=0) as reader:
                        assert reader.read(1) == b"x"
                else:
                    with fifo.open("wb", buffering=0) as writer:
                        writer.write(b"x")

            await asyncio.wait_for(asyncio.to_thread(meet_other_runner), 15)
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
