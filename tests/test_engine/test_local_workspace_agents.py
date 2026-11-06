"""Agent consumers of a real prepared workspace; root lifecycle is separate."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, ClassVar

import pytest

from conductor.bundle.collector import collect_bundle
from conductor.bundle.store import publish_bundle
from conductor.config.loader import load_config
from conductor.config.schema import AgentDef, DialogConfig, ValidatorConfig
from conductor.engine.workflow import WorkflowEngine, _realm_kwargs
from conductor.exceptions import ConfigurationError, ProviderError
from conductor.execution.errors import ExecutionSpecError
from conductor.execution.local import LocalRunnerBackend
from conductor.execution.local_process_scope import LocalProcessScopeController
from conductor.execution.types import BundleRef, RunSpec
from conductor.gates.dialog import DialogHandler
from conductor.providers.base import AgentOutput, AgentProvider
from conductor.providers.capabilities import ProviderCapabilities
from conductor.providers.openai import OpenAIProvider
from conductor.skills.errors import SkillError

_CAPS = OpenAIProvider.CAPABILITIES
assert _CAPS is not None


class RecordingProvider(AgentProvider, abstract=True):
    """Observe eager bytes and native directory contents at the provider boundary."""

    CAPABILITIES: ClassVar[ProviderCapabilities | None] = _CAPS.model_copy(
        update={
            "settings_dir": True,
            "session_continuity": True,
            "plugins": True,
            "plugin_flavor": "claude",
        }
    )

    @property
    def supports_continuation(self) -> bool:
        return True

    def __init__(self, *, native: bool = False) -> None:
        self.native = native
        self.root_required = False
        self.calls: list[dict[str, Any]] = []
        self.opaque = object()
        self.output = AgentOutput(
            content={"result": "ok"},
            raw_response=self.opaque,
            continuation_state=self.opaque,
            billing_mode="subscription",
        )

    @property
    def supports_native_skills(self) -> bool:
        return self.native

    @property
    def skills_require_plugin_root(self) -> bool:
        return self.root_required

    async def execute(self, agent, context, rendered_prompt, **kwargs) -> AgentOutput:
        self.calls.append({"agent": agent, "prompt": rendered_prompt, **kwargs})
        if agent.validator is None and agent.output and "passed" in agent.output:
            return AgentOutput(content={"passed": False, "issues": ["fixture"]}, raw_response="")
        return self.output

    async def execute_dialog_turn(
        self, system_prompt, user_message, history=None, model=None, **kwargs
    ) -> str:
        self.calls.append({"dialog": user_message, **kwargs})
        return "[READY_TO_CONTINUE]"

    async def validate_connection(self) -> bool:
        return True

    async def close(self) -> None:
        return None


def write_skill(path: Path, token: str) -> None:
    (path / "references").mkdir(parents=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {path.name}\ndescription: Fixture skill\n---\n{token}\n"
    )
    (path / "references/read.md").write_text(token + "_REFERENCE")


FLOW = """\
workflow:
  name: agents
  entry_point: worker
  runtime:
    provider: openai
    skills: [./skills/one]
agents:
  - name: worker
    prompt: '{{ workflow.input.value }}'
    routes: [{to: $end}]
  - name: off
    prompt: off
    skills: []
    routes: [{to: $end}]
parallel:
  - name: parallel
    agents: [worker, off]
    routes: [{to: $end}]
for_each:
  - name: loop
    type: for_each
    source: workflow.input.items
    as: item
    agent:
      name: worker
      prompt: '{{ item }}'
      skills: [./skills/two]
    routes: [{to: $end}]
output:
  result: '{{ worker.output.result }}'
"""


@asynccontextmanager
async def prepared_agents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    native: bool = False,
    flow: str = FLOW,
    setup_source: Callable[[Path], None] | None = None,
) -> AsyncIterator[tuple[WorkflowEngine, RecordingProvider]]:
    """Retain the original lease and physically release every owned resource."""
    source = tmp_path / "source"
    source.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    conductor_home = tmp_path / "conductor"
    monkeypatch.setenv("CONDUCTOR_HOME", str(conductor_home))
    write_skill(source / "skills/one", "STAGED_ONE")
    write_skill(source / "skills/two", "STAGED_TWO")
    if setup_source is not None:
        setup_source(source)
    path = source / "flow.yaml"
    path.write_text(flow)
    config = load_config(path)
    collected = collect_bundle(
        path, environment=None, allow_network=False, on_warning=lambda _: None
    )
    store = publish_bundle(collected.manifest, collected.files, collected.links)
    run = RunSpec(
        tmp_path.name,
        bundle=BundleRef(
            collected.manifest.bundle_digest,
            str(store),
            collected.root,
            collected.source_roots,
            collected.agent_paths,
        ),
        workspace_persistence="durable",
        local_workspace_root=str(conductor_home / "workspaces" / tmp_path.name / "local"),
    )
    backend = LocalRunnerBackend()
    lease = await backend.prepare_run(run)
    binding = backend.local_workspace_binding(lease)
    assert binding is not None
    controller = binding.runtime_context.process_scopes
    assert isinstance(controller, LocalProcessScopeController)
    provider = RecordingProvider(native=native)
    engine = WorkflowEngine(config, provider, workflow_path=path, skip_gates=True)
    engine._execution_session.backends["local"] = backend
    engine._execution_session.leases["local"] = lease
    engine._execution_session.agent_dependencies = collected.agent_dependencies
    engine.context.set_workflow_inputs({"value": "TASK_TOKEN", "items": ["left", "right"]})
    try:
        yield engine, provider
    finally:
        await engine._close_mcp_step_managers()
        result = await controller.await_release()
        if result.status == "released":
            await backend.confirm_workspace_released(lease)
            await backend.finalize_run(lease, "succeeded")
        await controller.close()
        if controller._tasks:
            done, pending = await asyncio.wait(tuple(controller._tasks), timeout=10)
            assert not pending
            for task in done:
                if not task.cancelled():
                    task.result()
        controller._accepting.join(timeout=5)
        assert not controller._accepting.is_alive()
        assert not controller.owners.tasks
        assert not controller._connections and not controller._send_tails


async def invocation(engine: WorkflowEngine, name: str = "worker", **kwargs):
    agent = engine._find_agent(name)
    assert isinstance(agent, AgentDef)
    realm = engine._agent_realm(name)
    assert realm is not None
    mapped = engine._resolve_agent_working_dir(agent, {}, realm)
    executor = await engine._get_executor_for_agent(mapped, realm)
    output = await executor.execute(
        mapped, {"workflow": {"input": {"value": "TASK_TOKEN"}}}, **_realm_kwargs(realm), **kwargs
    )
    return output, mapped, realm, executor


async def test_main_reads_staged_eager_bytes_after_source_removal(tmp_path, monkeypatch):
    # Requirement: the actual main handler uses staged skill/reference bytes, not deleted sources.
    async with prepared_agents(tmp_path, monkeypatch) as (engine, provider):
        shutil.rmtree(tmp_path / "source/skills")
        await engine._execute_loop("worker")
        call = provider.calls[0]
        assert "STAGED_ONE_REFERENCE" in call["prompt"]
        binding = engine._local_workspace_binding()
        assert binding is not None
        assert call["managed_workspace"] is binding.runtime_context
        assert call["agent"].working_dir == str(Path(binding.work_root) / "main")
        original = engine.config.agents[0]
        assert isinstance(original, AgentDef) and original.working_dir is None
        assert engine._execution_session.executed_backends() == {"local"}


@pytest.mark.parametrize("provider_name", ["claude", "openai"])
@pytest.mark.parametrize("plugin_path", [False, True])
async def test_managed_eager_skill_preserves_distinct_frontmatter_identity(
    tmp_path, monkeypatch, provider_name, plugin_path
):
    # Requirement: eager dispatch grants the selected basename while retaining its declared name.
    from unittest.mock import AsyncMock

    from pydantic_ai.messages import ModelMessagesTypeAdapter, UserPromptPart
    from pydantic_ai.models.test import TestModel

    from conductor.providers._pydantic_ai import agent_builder
    from conductor.providers.claude import ClaudeProvider

    entry = "./plugin/skills/local-guide" if plugin_path else "./skills/local-guide"

    def source_files(source: Path) -> None:
        directory = source / entry
        write_skill(directory, "STAGED_BODY {{ literal_skill }}")
        manifest = directory / "SKILL.md"
        manifest.write_text(manifest.read_text().replace("name: local-guide", "name: acme-guide"))
        if plugin_path:
            (source / "plugin/.claude-plugin").mkdir()
            (source / "plugin/.claude-plugin/plugin.json").write_text(
                '{"name":"owner","mcpServers":{"unselected":{"command":"NEVER_RUN"}}}'
            )
            write_skill(source / "plugin/skills/sibling", "UNSELECTED_SIBLING")

    flow = json.dumps(
        {
            "workflow": {
                "name": "eager-identity",
                "entry_point": "worker",
                "runtime": {"provider": provider_name},
            },
            "agents": [
                {
                    "name": "worker",
                    "prompt": "{{ '{{ literal_task }}' }}",
                    "skills": [entry],
                    "tools": [],
                    "routes": [{"to": "$end"}],
                }
            ],
        }
    )
    model = TestModel(custom_output_text="done")
    monkeypatch.setattr(agent_builder, "_resolve_anthropic_model", lambda *a, **kw: model)
    monkeypatch.setattr(agent_builder, "_resolve_openai_model", lambda *a, **kw: model)
    async with prepared_agents(tmp_path, monkeypatch, flow=flow, setup_source=source_files) as (
        engine,
        _,
    ):
        provider_type = ClaudeProvider if provider_name == "claude" else OpenAIProvider
        provider = provider_type(api_key="fixture-key", resolve_environment=False)
        monkeypatch.setattr(provider, "get_max_prompt_tokens", AsyncMock(return_value=128_000))
        monkeypatch.setattr(provider, "get_max_output_tokens", AsyncMock(return_value=4096))
        engine._single_provider = provider
        (tmp_path / "source").rename(tmp_path / "host-unavailable")
        try:
            output, _, realm, _ = await invocation(engine)
            messages = ModelMessagesTypeAdapter.validate_python(output.continuation_state)
            prompts = [
                part.content
                for message in messages
                for part in message.parts
                if isinstance(part, UserPromptPart)
            ]
            assert len(prompts) == 1 and isinstance(prompts[0], str)
            prompt = prompts[0]
            assert prompt.count('<skill name="local-guide">') == 1
            assert "name: acme-guide" in prompt
            assert "STAGED_BODY {{ literal_skill }}" in prompt
            assert "STAGED_BODY {{ literal_skill }}_REFERENCE" in prompt
            assert prompt.endswith("{{ literal_task }}")
            assert "UNSELECTED_SIBLING" not in prompt
            assert '<skill name="acme-guide">' not in prompt
            assert realm.dependencies is not None and realm.binding is not None
            (selected,) = realm.dependencies.skills
            assert selected.name == "local-guide"
            assert selected.frontmatter_name == "acme-guide"
            assert selected.directory == "skills/local-guide"
            assert selected.owner_root is None and realm.dependencies.plugins == ()
            assert model.last_model_request_parameters is not None
            assert model.last_model_request_parameters.function_tools == []
        finally:
            binding = engine._local_workspace_binding()
            assert binding is not None
            await provider.release_workspace(binding.runtime_context.root)
            await provider.close()


@pytest.mark.parametrize("provider_name", ["claude", "openai"])
@pytest.mark.parametrize("replacement", ["one", "renamed"])
async def test_managed_skill_rejects_frontmatter_drift_even_to_its_basename(
    tmp_path, monkeypatch, provider_name, replacement
):
    # Requirement: collection freezes the declared name; changing it to the basename is still drift.
    def source_files(source: Path) -> None:
        manifest = source / "skills/one/SKILL.md"
        manifest.write_text(manifest.read_text().replace("name: one", "name: original-declared"))

    async with prepared_agents(
        tmp_path,
        monkeypatch,
        flow=FLOW.replace("provider: openai", f"provider: {provider_name}"),
        setup_source=source_files,
    ) as (engine, provider):
        binding = engine._local_workspace_binding()
        assert binding is not None
        manifest = Path(binding.work_root) / "skills/one/SKILL.md"
        manifest.write_text(
            manifest.read_text().replace("name: original-declared", f"name: {replacement}")
        )
        (tmp_path / "source").rename(tmp_path / "host-unavailable")

        with pytest.raises(ConfigurationError, match="frozen name"):
            await invocation(engine)
        assert provider.calls == []


async def test_same_incarnation_mutable_payload_is_fresh_without_host_cache_clear(
    tmp_path, monkeypatch
):
    # Requirement: a second invocation in one incarnation reads current selected eager bytes.
    async with prepared_agents(tmp_path, monkeypatch) as (engine, provider):
        _, _, realm, executor = await invocation(engine)
        assert realm.dependencies is not None and realm.binding is not None
        selected = realm.dependencies.skills[0]
        staged = Path(realm.binding.work_root) / selected.directory / "references/read.md"
        staged.write_text("LIVE_SECOND_REFERENCE")
        agent = engine._find_agent("worker")
        assert isinstance(agent, AgentDef)
        mapped = engine._resolve_agent_working_dir(agent, {}, realm)
        await executor.execute(
            mapped, {"workflow": {"input": {"value": "SECOND"}}}, **_realm_kwargs(realm)
        )
        assert "LIVE_SECOND_REFERENCE" in provider.calls[-1]["prompt"]
        assert "STAGED_ONE_REFERENCE" not in provider.calls[-1]["prompt"]


@pytest.mark.parametrize("kind", ["parallel", "for_each"])
async def test_handler_selections_use_authored_group_not_item_identity(tmp_path, monkeypatch, kind):
    # Requirement: parallel and inline same-named agents retain exact distinct frozen selections.
    async with prepared_agents(tmp_path, monkeypatch, native=True) as (engine, provider):
        if kind == "parallel":
            await engine._execute_parallel_group(engine.config.parallel[0])
            selected = [call for call in provider.calls if call["agent"].name == "worker"]
            assert len(selected) == 1
            assert Path(selected[0]["skill_directories"][0]).name == "one"
            disabled = next(call for call in provider.calls if call["agent"].name == "off")
            assert disabled["skill_directories"] is None
        else:
            await engine._execute_for_each_group(engine.config.for_each[0])
            assert len(provider.calls) == 2
            assert all(Path(call["skill_directories"][0]).name == "two" for call in provider.calls)


@pytest.mark.parametrize("damage", ["missing", "escape", "reference_directory", "frontmatter"])
async def test_invalid_selected_dependency_refuses_before_dispatch(tmp_path, monkeypatch, damage):
    # Requirement: missing/escaping/broken staged dependencies produce zero provider dispatches.
    async with prepared_agents(tmp_path, monkeypatch) as (engine, provider):
        realm = engine._agent_realm("worker")
        assert realm is not None and realm.binding is not None and realm.dependencies is not None
        skill = Path(realm.binding.work_root) / realm.dependencies.skills[0].directory
        if damage == "missing":
            (skill / "SKILL.md").unlink()
        elif damage == "escape":
            external = tmp_path / "outside"
            external.write_text("HOST_TOKEN")
            (skill / "references/read.md").unlink()
            (skill / "references/read.md").symlink_to(external)
        elif damage == "reference_directory":
            redirected = Path(realm.binding.work_root) / "main/redirected"
            redirected.mkdir()
            external = tmp_path / "outside"
            external.write_text("HOST_TOKEN")
            (redirected / "read.md").symlink_to(external)
            shutil.rmtree(skill / "references")
            (skill / "references").symlink_to(redirected, target_is_directory=True)
        else:
            (skill / "SKILL.md").write_text("---\nname: wrong\n---\n")
        with pytest.raises((ConfigurationError, SkillError)):
            await invocation(engine)
        assert provider.calls == []
        assert not engine._execution_session.executed_backends()


async def test_single_render_and_live_cwd_precede_host_checks(tmp_path, monkeypatch):
    # Requirement: rendered cwd/settings are mapped once without mutating the shared AgentDef.
    async with prepared_agents(tmp_path, monkeypatch) as (engine, provider):
        agent = engine._find_agent("worker")
        assert isinstance(agent, AgentDef)
        agent.working_dir = "{{ value }}"
        realm = engine._agent_realm("worker")
        assert realm is not None and realm.binding is not None
        literal = "{{ literal }}"
        destination = Path(realm.binding.work_root) / "main" / literal
        destination.mkdir()
        mapped = engine._resolve_agent_working_dir(agent, {"value": literal}, realm)
        assert mapped.working_dir == str(destination)
        assert agent.working_dir == "{{ value }}"
        assert provider.calls == []


async def test_opaque_output_and_continuation_keep_identity(tmp_path, monkeypatch):
    # Requirement: local execution never serializes opaque provider state or billing provenance.
    async with prepared_agents(tmp_path, monkeypatch) as (engine, provider):
        output, *_ = await invocation(
            engine, continuation_state=provider.opaque, guidance_section="FOLLOWUP_TOKEN"
        )
        assert output is provider.output
        assert (
            output.raw_response is provider.opaque and output.continuation_state is provider.opaque
        )
        assert output.billing_mode == "subscription"
        assert provider.calls[0]["continuation_state"] is provider.opaque
        assert provider.calls[0]["prompt"] == "FOLLOWUP_TOKEN"


async def test_seal_after_resolution_refuses_late_payload(tmp_path, monkeypatch):
    # Requirement: resolving a managed executor before seal cannot admit a new payload after seal.
    async with prepared_agents(tmp_path, monkeypatch) as (engine, provider):
        agent = engine._find_agent("worker")
        assert isinstance(agent, AgentDef)
        realm = engine._agent_realm("worker")
        assert realm is not None and realm.binding is not None
        mapped = engine._resolve_agent_working_dir(agent, {}, realm)
        executor = await engine._get_executor_for_agent(mapped, realm)
        realm.binding.runtime_context.process_scopes.begin_teardown()
        with pytest.raises(ExecutionSpecError):
            await executor.execute(mapped, {}, **_realm_kwargs(realm))
        assert provider.calls == [] and not engine._execution_session.executed_backends()


async def test_validator_rerun_preserves_binding_but_grader_is_isolated(tmp_path, monkeypatch):
    # Requirement: grader and continuation rerun retain cwd/context without settings/session/MCP.
    async with prepared_agents(tmp_path, monkeypatch) as (engine, provider):
        output, mapped, realm, executor = await invocation(engine)
        assert realm.binding is not None
        mapped = mapped.model_copy(
            update={
                "validator": ValidatorConfig(criteria="fixture"),
                "session_key": "primary",
                "settings_dir": mapped.working_dir,
            }
        )
        await engine._apply_validator(mapped, output, 0, {}, executor, None, None, realm=realm)
        grader, rerun = provider.calls[-2:]
        assert grader["agent"].settings_dir is None and grader["agent"].session_key is None
        assert grader["agent"].working_dir == mapped.working_dir
        assert grader["suppress_mcp_servers"] is True and grader["tools"] == []
        assert grader["managed_workspace"] is realm.binding.runtime_context
        assert rerun["continuation_state"] is provider.opaque
        assert rerun["managed_workspace"] is realm.binding.runtime_context


async def test_refused_grader_is_fail_open_without_executed_accounting(tmp_path, monkeypatch):
    # Requirement: a refused grading dispatch is fail-open but creates no successful usage row.
    async with prepared_agents(tmp_path, monkeypatch) as (engine, provider):
        agent = engine._find_agent("worker")
        assert isinstance(agent, AgentDef)
        agent.validator = ValidatorConfig(criteria="fixture")
        realm = engine._agent_realm("worker")
        assert realm is not None and realm.binding is not None
        mapped = engine._resolve_agent_working_dir(agent, {}, realm)
        executor = await engine._get_executor_for_agent(mapped, realm)
        realm.binding.runtime_context.process_scopes.begin_teardown()
        result = await engine._apply_validator(
            mapped, provider.output, 0, {}, executor, None, None, realm=realm
        )
        assert result is provider.output and provider.calls == []
        assert not engine._execution_session.executed_backends()
        assert engine.usage_tracker.get_summary().agents == []


async def test_tty_dialog_turn_uses_mapped_context(tmp_path, monkeypatch):
    # Requirement: tty dialog turns keep explicit mapped cwd and the prepared ownership context.
    async with prepared_agents(tmp_path, monkeypatch) as (engine, provider):
        _, agent, realm, _ = await invocation(engine)
        assert realm.binding is not None
        agent = agent.model_copy(update={"dialog": DialogConfig(trigger_prompt="fixture")})
        handler = DialogHandler()

        async def engagement():
            return "engage"

        replies = iter(["TURN_TOKEN", "yes"])

        async def user_input(**kwargs):
            return next(replies)

        monkeypatch.setattr(handler, "_ask_engagement", engagement)
        monkeypatch.setattr(handler, "_get_user_input", user_input)
        await handler.handle_dialog(
            agent,
            {},
            "fixture",
            provider,
            managed_workspace=realm.binding.runtime_context,
            working_dir=agent.working_dir,
            on_dispatch=engine._mark_local_workspace_dispatch,
        )
        assert provider.calls[-1]["managed_workspace"] is realm.binding.runtime_context
        assert provider.calls[-1]["working_dir"] == agent.working_dir


def plugin_source(source: Path) -> None:
    plugin = source / "plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin/plugin.json").write_text(json.dumps({"name": "toolkit"}))
    (plugin / ".mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"fixture": {"command": "OPAQUE_EXEC", "args": ["/authored/raw"]}}}
        )
    )
    (plugin / "agents").mkdir()
    (plugin / "agents/helper.md").write_text(
        "---\nname: helper\ndescription: fixture\ntools: [LiteralTool]\n---\nSTAGED_AGENT"
    )
    write_skill(plugin / "skills/task", "STAGED_PLUGIN")


PLUGIN_FLOW = """\
workflow:
  name: plugins
  entry_point: worker
  runtime: {provider: copilot}
agents:
  - name: worker
    prompt: task
    skills: []
    plugins: [./plugin]
    routes: [{to: $end}]
  - name: narrow
    prompt: narrow
    skills: [./plugin/skills/task]
    routes: [{to: $end}]
  - name: quiet
    prompt: quiet
    skills: []
    plugins: [{name: ./plugin, skills: false, agents: false, mcp: false}]
    routes: [{to: $end}]
"""


@pytest.mark.skipif(sys.platform != "linux", reason="Native peers use Linux scope custody")
@pytest.mark.parametrize("provider_name", ["claude-agent-sdk", "copilot"])
@pytest.mark.parametrize("inline", [True, False])
@pytest.mark.parametrize("off_first", [True, False])
async def test_shared_plugin_mcp_switches_reach_native_provider_wire(
    tmp_path, monkeypatch, provider_name, inline, off_first
):
    # Requirement: actual SDK sessions share one staged skill root without sharing MCP grants.
    from conductor.providers import claude_agent_sdk as claude
    from conductor.providers.copilot import CopilotProvider
    from tests.test_providers.managed_claude_sdk_fixture import native_harness

    def source_files(source: Path) -> None:
        plugin = source / "plugin"
        (plugin / ".claude-plugin").mkdir(parents=True)
        servers = {"fixture": {"command": "OPAQUE_EXEC", "args": ["/authored/raw"]}}
        (plugin / ".claude-plugin/plugin.json").write_text(
            json.dumps({"name": "toolkit", "mcpServers": servers if inline else "servers.json"})
        )
        (plugin / "servers.json").write_text(json.dumps({"mcpServers": servers}))
        write_skill(plugin / "skills/task", "STAGED_PLUGIN")

    agent_order = ("off", "on") if off_first else ("on", "off")
    definitions = [
        {
            "name": name,
            "prompt": "fixture",
            "skills": [],
            "plugins": [{"name": "./plugin", "mcp": name == "on"}],
            **(
                {"tools": ["fixture__lookup"] if name == "on" else ["view"]}
                if provider_name == "copilot"
                else {}
            ),
            "routes": [{"to": "$end"}],
        }
        for name in agent_order
    ]
    flow = json.dumps(
        {
            "workflow": {
                "name": "shared-native-plugin",
                "entry_point": definitions[0]["name"],
                "runtime": {"provider": provider_name},
            },
            "agents": definitions,
            **({"tools": ["fixture__lookup", "view"]} if provider_name == "copilot" else {}),
        }
    )
    run = tmp_path / "run"
    run.mkdir()
    options = []
    payloads = []
    original_options = claude.ClaudeAgentOptions
    original_write = claude._write_mcp_config

    def capture_options(**kwargs):
        option = original_options(**kwargs)
        options.append(option)
        return option

    def capture_payload(servers):
        path = original_write(servers)
        payloads.append(json.loads(Path(path).read_text()))
        return path

    monkeypatch.setattr(claude, "ClaudeAgentOptions", capture_options)
    monkeypatch.setattr(claude, "_write_mcp_config", capture_payload)
    async with native_harness(tmp_path / "peer", monkeypatch) as peer:
        receipt = tmp_path / "receipts"
        receipt.mkdir()
        fixture = tmp_path / "copilot-peer"
        fixture.write_text(
            f"#!{sys.executable}\n"
            + Path(__file__)
            .parents[1]
            .joinpath("test_providers/managed_copilot_fixture.py")
            .read_text()
        )
        fixture.chmod(0o700)
        monkeypatch.setenv("COPILOT_CLI_PATH", str(fixture))
        monkeypatch.setenv("COPILOT_FIXTURE_RECEIPT", str(receipt))
        async with prepared_agents(
            run, monkeypatch, native=True, flow=flow, setup_source=source_files
        ) as (engine, _):
            workflow_servers = {"workflow-only": {"type": "stdio", "command": "WF_OPAQUE"}}
            provider = (
                claude.ClaudeAgentSdkProvider(mcp_servers=workflow_servers)
                if provider_name == "claude-agent-sdk"
                else CopilotProvider(mcp_servers=workflow_servers, github_token="fixture-token")
            )
            engine._single_provider = provider
            binding = engine._local_workspace_binding()
            assert binding is not None
            root = Path(binding.work_root) / "plugins/toolkit"
            expected_skills = [str(root / "skills/task")]
            (run / "source/plugin").rename(run / "retired-plugin")
            original_agents = [agent.model_dump() for agent in engine.config.agents]
            try:
                for name in agent_order:
                    await invocation(engine, name)
                assert [agent.model_dump() for agent in engine.config.agents] == original_agents
                if provider_name == "claude-agent-sdk":
                    assert [o.plugins for o in options] == [
                        [{"type": "local", "path": str(root)}],
                        [{"type": "local", "path": str(root)}],
                    ]
                    assert all(o.strict_mcp_config and o.setting_sources == [] for o in options)
                    assert all(o.skills == ["toolkit:task"] for o in options)
                    for name, option, payload in zip(agent_order, options, payloads, strict=True):
                        enabled = name == "on"
                        assert set(payload["mcpServers"]) == (
                            {"workflow-only", "fixture"} if enabled else {"workflow-only"}
                        )
                        assert set(option.allowed_tools) == (
                            {"mcp__workflow-only__*", "mcp__fixture__*"}
                            if enabled
                            else {"mcp__workflow-only__*"}
                        )
                    main = [event for event in peer.events if event["event"] == "main"]
                    assert len(main) == 2
                    assert all("--strict-mcp-config" in event["argv"] for event in main)
                    assert all(str(root) in event["argv"] for event in main)
                else:
                    wires = [
                        json.loads(line)
                        for path in receipt.glob("*.jsonl")
                        for line in path.read_text().splitlines()
                    ]
                    creates = [
                        wire["params"] for wire in wires if wire.get("method") == "session.create"
                    ]
                    assert len(creates) == 2
                    by_name = {config["sessionId"]: config for config in creates}
                    assert len(by_name) == 2
                    for name in agent_order:
                        config = by_name[provider.get_session_ids()[name]]
                        assert config["skillDirectories"] == expected_skills
                        assert "pluginDirectories" not in config
                        assert not config.get("enableConfigDiscovery")
                        assert set(config["mcpServers"]) == (
                            {"workflow-only", "fixture"} if name == "on" else {"workflow-only"}
                        )
                        assert config["availableTools"] == (
                            ["mcp:fixture-lookup"] if name == "on" else ["view"]
                        )
            finally:
                await provider.release_workspace(binding.runtime_context.root)
                await provider.close()


async def test_full_plugin_and_private_skill_owner_do_not_share_authority(tmp_path, monkeypatch):
    # Requirement: skill-only native ownership never expands to another agent's full plugin root.
    async with prepared_agents(
        tmp_path,
        monkeypatch,
        native=True,
        flow=PLUGIN_FLOW.replace("provider: copilot", "provider: claude-agent-sdk"),
        setup_source=plugin_source,
    ) as (engine, provider):
        provider.root_required = True
        shutil.rmtree(tmp_path / "source/plugin")
        _, _, full, _ = await invocation(engine)
        _, _, narrow, _ = await invocation(engine, "narrow")
        assert full.binding is not None and narrow.dependencies is not None
        full_call, narrow_call = provider.calls
        assert full_call["custom_agents"][0]["name"] == "toolkit:helper"
        assert full_call["custom_agents"][0]["tools"] == ["LiteralTool"]
        assert full_call["extra_mcp_servers"]["fixture"]["args"] == ["/authored/raw"]
        assert narrow_call["custom_agents"] is None and narrow_call["extra_mcp_servers"] is None
        selected = narrow.dependencies.skills[0]
        assert selected.owner_root is not None and selected.owner_root.startswith("skills/")
        owner = Path(full.binding.work_root) / selected.owner_root
        assert json.loads((owner / ".claude-plugin/plugin.json").read_text()) == {"name": "toolkit"}
        assert not (owner / ".mcp.json").exists() and not (owner / "agents").exists()


async def test_component_switches_do_not_broaden_union_translation(tmp_path, monkeypatch):
    # Requirement: a full plugin staged for a sibling does not override disabled components.
    async with prepared_agents(
        tmp_path, monkeypatch, native=True, flow=PLUGIN_FLOW, setup_source=plugin_source
    ) as (engine, provider):
        await invocation(engine, "quiet")
        call = provider.calls[0]
        assert call["skill_directories"] is None
        assert call["custom_agents"] is None and call["extra_mcp_servers"] is None


async def test_nested_child_uses_its_workflow_filename_and_namespace(tmp_path, monkeypatch):
    # Requirement: a same-named child agent never consumes the root workflow's selection.
    def child_source(source: Path) -> None:
        child = source / "child"
        write_skill(child / "skills/one", "CHILD_STAGED")
        write_skill(child / "skills/two", "CHILD_UNUSED")
        (child / "flow.yaml").write_text(FLOW)

    flow = """\
workflow: {name: parent, entry_point: child}
agents:
  - name: child
    type: workflow
    workflow: ./child/flow.yaml
    routes: [{to: $end}]
"""
    async with prepared_agents(tmp_path, monkeypatch, flow=flow, setup_source=child_source) as (
        engine,
        provider,
    ):
        await engine._execute_loop("child")
        assert len(provider.calls) == 1 and "CHILD_STAGED_REFERENCE" in provider.calls[0]["prompt"]
        binding = engine._local_workspace_binding()
        assert binding is not None
        assert provider.calls[0]["agent"].working_dir == str(Path(binding.work_root) / "main/child")


@pytest.mark.parametrize("capability", ["working_dir", "managed_local_workspace"])
async def test_missing_provider_capability_refuses_before_payload(
    tmp_path, monkeypatch, capability
):
    # Requirement: actual managed agents require both capability flags before provider invocation.
    async with prepared_agents(tmp_path, monkeypatch) as (engine, provider):
        capabilities = RecordingProvider.CAPABILITIES
        assert capabilities is not None
        monkeypatch.setattr(
            RecordingProvider,
            "CAPABILITIES",
            capabilities.model_copy(update={capability: False}),
        )
        with pytest.raises(ConfigurationError):
            await invocation(engine)
        assert provider.calls == [] and not engine._execution_session.executed_backends()


async def test_web_dialog_turn_retains_explicit_namespace_cwd(tmp_path, monkeypatch):
    # Requirement: web dialog turns carry the same explicit mapped namespace as tty turns.
    from unittest.mock import AsyncMock, MagicMock

    async with prepared_agents(tmp_path, monkeypatch) as (engine, provider):
        _, agent, realm, _ = await invocation(engine)
        assert realm.binding is not None
        dashboard = MagicMock()
        dashboard.wait_for_dialog_message = AsyncMock(
            side_effect=[{"content": "WEB_TOKEN"}, {"content": "yes"}]
        )
        handler = DialogHandler(web_dashboard=dashboard)
        await handler.handle_dialog(
            agent,
            {},
            "fixture",
            provider,
            managed_workspace=realm.binding.runtime_context,
            working_dir=agent.working_dir,
            on_dispatch=engine._mark_local_workspace_dispatch,
        )
        assert provider.calls[-1]["dialog"] == "WEB_TOKEN"
        assert provider.calls[-1]["managed_workspace"] is realm.binding.runtime_context
        assert provider.calls[-1]["working_dir"] == agent.working_dir


async def test_dialog_evaluation_uses_prepared_context_before_any_turn(tmp_path, monkeypatch):
    # Requirement: even the dialog trigger evaluation must not start a host-local conversation.
    async with prepared_agents(tmp_path, monkeypatch) as (engine, provider):
        _, agent, realm, executor = await invocation(engine)
        assert realm.binding is not None
        agent = agent.model_copy(update={"dialog": DialogConfig(trigger_prompt="fixture")})
        await engine._handle_dialog(agent, provider.output, {}, executor, realm)
        call = provider.calls[-1]
        assert call["managed_workspace"] is realm.binding.runtime_context
        assert call["working_dir"] == agent.working_dir


async def test_final_admission_rechecks_after_awaited_plugin_credentials(tmp_path, monkeypatch):
    # Requirement: a seal during awaited dependency resolution refuses the later provider payload.
    from conductor.executor import agent as executor_module

    async with prepared_agents(
        tmp_path, monkeypatch, native=True, flow=PLUGIN_FLOW, setup_source=plugin_source
    ) as (engine, provider):
        entered = asyncio.Event()
        release = asyncio.Event()
        original = executor_module.resolve_mcp_servers

        async def held(servers):
            entered.set()
            await release.wait()
            return await original(servers)

        monkeypatch.setattr(executor_module, "resolve_mcp_servers", held)
        task = asyncio.create_task(invocation(engine))
        try:
            async with asyncio.timeout(10):
                await entered.wait()
                binding = engine._local_workspace_binding()
                assert binding is not None
                binding.runtime_context.process_scopes.begin_teardown()
                release.set()
                with pytest.raises(ExecutionSpecError):
                    await task
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert provider.calls == [] and not engine._execution_session.executed_backends()


async def test_new_incarnation_does_not_reuse_previous_payload_view(tmp_path, monkeypatch):
    # Requirement: equal bundle digests in different incarnations never share mutable skill bytes.
    roots = [tmp_path / "left", tmp_path / "right"]
    incarnations = []
    digests = []
    for index, root in enumerate(roots):
        root.mkdir()
        with monkeypatch.context() as scoped:
            async with prepared_agents(root, scoped) as (engine, provider):
                binding = engine._local_workspace_binding()
                assert binding is not None
                incarnations.append(binding.incarnation)
                digests.append(binding.bundle.digest)
                realm = engine._agent_realm("worker")
                assert realm is not None and realm.dependencies is not None
                path = Path(binding.work_root) / realm.dependencies.skills[0].directory / "SKILL.md"
                path.write_text(f"---\nname: one\ndescription: fixture\n---\nINCARNATION_{index}\n")
                await invocation(engine)
                assert f"INCARNATION_{index}" in provider.calls[0]["prompt"]
                assert f"INCARNATION_{1 - index}" not in provider.calls[0]["prompt"]
    assert incarnations[0] != incarnations[1] and digests[0] == digests[1]


@pytest.mark.parametrize("absolute", [True, False])
async def test_plugin_config_path_is_checked_before_any_host_read(tmp_path, monkeypatch, absolute):
    # Requirement: typed MCP config paths never read unselected host bytes, even with mcp disabled.
    import os

    async with prepared_agents(
        tmp_path, monkeypatch, native=True, flow=PLUGIN_FLOW, setup_source=plugin_source
    ) as (engine, provider):
        realm = engine._agent_realm("quiet")
        assert realm is not None and realm.binding is not None and realm.dependencies is not None
        root = Path(realm.binding.work_root) / realm.dependencies.plugins[0][1]
        host = tmp_path / "host-config.json"
        host.write_text('{"mcpServers": {"host": {"command": "HOST_EXEC"}}}')
        declared = str(host) if absolute else os.path.relpath(host, root)
        (root / ".claude-plugin/plugin.json").write_text(
            json.dumps({"name": "toolkit", "mcpServers": declared})
        )
        original = Path.read_text

        def read_text(path, *args, **kwargs):
            assert path != host, "Host plugin configuration must never be read."
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read_text)
        with pytest.raises(ConfigurationError):
            await invocation(engine, "quiet")
        assert provider.calls == [] and not engine._execution_session.executed_backends()


async def test_authored_absolute_plugin_config_uses_its_staged_file(tmp_path, monkeypatch):
    # Requirement: selected absolute MCP config references survive removal of their original source.
    def setup(source: Path) -> None:
        plugin_source(source)
        (source / "plugin/.claude-plugin/plugin.json").write_text(
            json.dumps({"name": "toolkit", "mcpServers": str(source / "plugin/.mcp.json")})
        )

    flow = """\
workflow: {name: plugin, entry_point: worker, runtime: {provider: copilot}}
agents:
  - name: worker
    prompt: fixture
    plugins: [./plugin]
    routes: [{to: $end}]
"""
    async with prepared_agents(
        tmp_path, monkeypatch, native=True, flow=flow, setup_source=setup
    ) as (engine, provider):
        shutil.rmtree(tmp_path / "source/plugin")
        await invocation(engine)
        assert provider.calls[0]["extra_mcp_servers"]["fixture"]["command"] == "OPAQUE_EXEC"


@pytest.mark.parametrize("managed", [False, True])
async def test_registry_failure_preserves_original_error(tmp_path, monkeypatch, managed):
    # Requirement: actual provider acquisition propagates its error, never metadata's fallback None.
    async with prepared_agents(tmp_path, monkeypatch) as (engine, provider):
        agent = engine._find_agent("worker")
        assert isinstance(agent, AgentDef)
        realm = engine._agent_realm("worker") if managed else None
        error = ProviderError("REGISTRY_SENTINEL")
        expected_context = None
        if realm is not None:
            assert realm.binding is not None
            expected_context = realm.binding.runtime_context

        class BrokenRegistry:
            async def get_provider(self, definition, *, managed_workspace=None):
                assert managed_workspace is expected_context
                raise error

        monkeypatch.setattr(engine, "_registry", BrokenRegistry())
        with pytest.raises(ProviderError) as caught:
            await engine._get_executor_for_agent(agent, realm)
        assert caught.value is error and provider.calls == []
        assert not engine._execution_session.executed_backends()
