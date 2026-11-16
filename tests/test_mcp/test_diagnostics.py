"""SDK-source logging privacy, factory delegation and caller context isolation."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import anyio
import mcp
import pytest
from mcp import ClientSession

from conductor.mcp import diagnostics
from conductor.mcp.manager import MCPManager
from tests.test_engine.mcp_placement import placement_config, placement_engine
from tests.test_mcp.fixtures.diagnostic_stdio import CANARY


@pytest.fixture
def realm(tmp_path, monkeypatch):
    from tests.test_mcp.fixtures.docker_realm import create_realm

    return create_realm(tmp_path, monkeypatch)


@pytest.fixture
def factory_witness(monkeypatch):
    previous = logging.getLogRecordFactory()
    calls = []

    def witness(*args, **kwargs):
        calls.append((args[0], args[2], diagnostics.SAFE_TRANSPORT_DIAGNOSTICS.get()))
        record = logging.LogRecord(*args, **kwargs)
        record.safe_extra = "preserved"
        record.original_path = args[2]
        record.private_context = diagnostics.SAFE_TRANSPORT_DIAGNOSTICS.get()
        record.pathname = (
            str(Path(mcp.__file__).parent / "shared/session.py")
            if record.msg == "application %s"
            else "previous-factory-source.py"
        )
        return record

    logging.setLogRecordFactory(witness)
    monkeypatch.setattr(diagnostics, "_installed", False)
    try:
        yield calls
    finally:
        logging.setLogRecordFactory(previous)


@pytest.mark.parametrize("mode,logger", [("root", "root"), ("client", "client")])
async def test_diagnostic_real_sdk_source_and_factory_delegation(
    realm, mode, logger, caplog, factory_witness
):
    # Requirement: actual root/client SDK diagnostics are safe without changing application records.
    config = placement_config(failure=f"diagnostic-{mode}")
    caplog.set_level(logging.DEBUG)
    async with placement_engine(config, realm) as engine:
        manager = await engine._get_mcp_step_manager("srv", None, step_key="call")
        factory = logging.getLogRecordFactory()
        for _ in range(3):
            other = MCPManager()
            await other.close()
            assert logging.getLogRecordFactory() is factory
        tool = CANARY if mode == "client" else "receive"
        result = await manager.call_tool_structured("srv", tool, {})
        assert result["is_error"] is False
        another = await engine._get_mcp_step_manager("srv", "next", step_key="call")
        await another.call_tool_structured("srv", tool, {})
        assert logging.getLogRecordFactory() is factory
        sdk_records = [
            record
            for record in caplog.records
            if record.name == logger
            and record.private_context
            and record.original_path.startswith(str(Path(mcp.__file__).parent))
        ]
        assert sdk_records
        assert all(
            record.getMessage() == "MCP transport diagnostic (payload suppressed)"
            for record in sdk_records
        )
        assert all(
            record.exc_info is None and record.exc_text is None and record.stack_info is None
            for record in sdk_records
        )
        assert all(
            record.safe_extra == "preserved" and record.pathname == "previous-factory-source.py"
            for record in sdk_records
        )
        token = diagnostics.SAFE_TRANSPORT_DIAGNOSTICS.set(True)
        try:
            for name in ("root", "client"):
                logging.getLogger(name).warning("application %s", "control")
        finally:
            diagnostics.SAFE_TRANSPORT_DIAGNOSTICS.reset(token)
        controls = [record for record in caplog.records if record.msg == "application %s"]
        assert [
            (record.name, record.args, record.levelno, record.pathname) for record in controls
        ] == [
            (
                "root",
                ("control",),
                logging.WARNING,
                str(Path(mcp.__file__).parent / "shared/session.py"),
            ),
            (
                "client",
                ("control",),
                logging.WARNING,
                str(Path(mcp.__file__).parent / "shared/session.py"),
            ),
        ]
        assert CANARY not in caplog.text
        assert len(factory_witness) == len(caplog.records)
        (realm.root / "diagnostic-observation.json").write_text(
            json.dumps(
                [
                    {
                        "logger": record.name,
                        "source": record.original_path,
                        "marker": record.private_context,
                        "message": record.getMessage(),
                    }
                    for record in sdk_records
                ]
            )
        )


@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
async def test_diagnostic_caller_child_and_concurrent_local_context(
    realm, outcome, monkeypatch, caplog
):
    # Requirement: caller/child SDK records are private; overlapping ordinary calls stay visible.
    config = placement_config()
    caplog.set_level(logging.DEBUG)
    entered = anyio.Event()
    local_finished = anyio.Event()
    real_call = ClientSession.call_tool
    private_session = None

    async def child_log(arguments):
        logging.getLogger("mcp.client.probe").debug("request arguments %r", arguments)

    async def observed_call(session, name, arguments=None, **kwargs):
        async with anyio.create_task_group() as group:
            group.start_soon(child_log, arguments)
        logging.getLogger("mcp.client.probe").debug("request arguments %r", arguments)
        if session is private_session:
            entered.set()
            await local_finished.wait()
            if outcome == "error":
                raise RuntimeError(CANARY)
            if outcome == "cancel":
                await anyio.sleep_forever()
        return await real_call(session, name, arguments=arguments, **kwargs)

    monkeypatch.setattr(ClientSession, "call_tool", observed_call)
    async with placement_engine(config, realm) as engine:
        manager = await engine._get_mcp_step_manager("srv", None, step_key="call")
        private_session = manager.sessions["srv"]
        local = MCPManager()
        peer = Path(__file__).with_name("fixtures") / "docker_stdio.py"
        import sys

        reset = []

        async def private_call():
            try:
                return await manager.call_tool_structured("srv", "receive", {"q": CANARY})
            finally:
                reset.append(diagnostics.SAFE_TRANSPORT_DIAGNOSTICS.get())

        task = asyncio.create_task(private_call())
        try:
            with anyio.fail_after(15):
                await entered.wait()
                await local.connect_server(
                    "ordinary",
                    sys.executable,
                    args=[str(peer), "--peer", str(realm.root / "local.json")],
                    env={
                        "PEER_FAILURE": "diagnostic-root",
                        "DIAGNOSTIC_CANARY": "ordinary-sdk-control",
                    },
                )
                await local.call_tool_structured("ordinary", "receive", {})
                local_finished.set()
                if outcome == "cancel":
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                elif outcome == "error":
                    with pytest.raises(RuntimeError, match="stderr suppressed"):
                        await task
                else:
                    assert (await task)["is_error"] is False
        finally:
            local_finished.set()
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            await local.close(strict=True)
        assert reset == [False]
        assert not diagnostics.SAFE_TRANSPORT_DIAGNOSTICS.get()
        assert CANARY not in caplog.text
        assert any(
            record.name == "root"
            and "ordinary-sdk-control" in record.getMessage()
            and record.levelno == logging.WARNING
            for record in caplog.records
        )
        assert any(record.msg == "request arguments %r" for record in caplog.records)
        assert any(
            record.getMessage() == "MCP transport diagnostic (payload suppressed)"
            for record in caplog.records
        )
