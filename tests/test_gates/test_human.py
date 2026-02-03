"""Unit tests for HumanGateHandler with mocked terminal input."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from conductor.config.schema import AgentDef, GateOption
from conductor.exceptions import HumanGateError
from conductor.gates.human import GateResult, HumanGateHandler


@pytest.fixture
def mock_console() -> MagicMock:
    """Create a mock Rich console."""
    return MagicMock()


@pytest.fixture
def sample_options() -> list[GateOption]:
    """Create sample gate options."""
    return [
        GateOption(
            label="Approve and continue",
            value="approved",
            route="next_agent",
        ),
        GateOption(
            label="Request changes",
            value="changes_requested",
            route="revision_agent",
        ),
        GateOption(
            label="Reject",
            value="rejected",
            route="$end",
        ),
    ]


@pytest.fixture
def sample_options_with_prompt_for() -> list[GateOption]:
    """Create sample gate options with prompt_for field."""
    return [
        GateOption(
            label="Approve with feedback",
            value="approved_with_feedback",
            route="next_agent",
            prompt_for="feedback",
        ),
        GateOption(
            label="Reject",
            value="rejected",
            route="$end",
        ),
    ]


@pytest.fixture
def human_gate_agent(sample_options: list[GateOption]) -> AgentDef:
    """Create a sample human_gate agent."""
    return AgentDef(
        name="approval_gate",
        type="human_gate",
        prompt="Please review the following content:\n\n{{ agent1.output }}",
        options=sample_options,
    )


@pytest.fixture
def human_gate_agent_with_prompt_for(
    sample_options_with_prompt_for: list[GateOption],
) -> AgentDef:
    """Create a sample human_gate agent with prompt_for option."""
    return AgentDef(
        name="feedback_gate",
        type="human_gate",
        prompt="Please provide your feedback:",
        options=sample_options_with_prompt_for,
    )


@pytest.fixture
def human_gate_agent_no_options() -> AgentDef:
    """Create an invalid human_gate agent without options."""
    # We need to bypass the validator for testing error handling
    agent = AgentDef.__new__(AgentDef)
    object.__setattr__(agent, "name", "bad_gate")
    object.__setattr__(agent, "type", "human_gate")
    object.__setattr__(agent, "prompt", "This should fail")
    object.__setattr__(agent, "options", None)
    object.__setattr__(agent, "description", None)
    object.__setattr__(agent, "model", None)
    object.__setattr__(agent, "input", [])
    object.__setattr__(agent, "tools", None)
    object.__setattr__(agent, "system_prompt", None)
    object.__setattr__(agent, "output", None)
    object.__setattr__(agent, "routes", [])
    return agent


class TestGateResult:
    """Tests for the GateResult dataclass."""

    def test_gate_result_creation(self, sample_options: list[GateOption]) -> None:
        """Test creating a GateResult."""
        result = GateResult(
            selected_option=sample_options[0],
            route="next_agent",
            additional_input={"feedback": "Looks good!"},
        )
        assert result.selected_option == sample_options[0]
        assert result.route == "next_agent"
        assert result.additional_input == {"feedback": "Looks good!"}

    def test_gate_result_default_additional_input(self, sample_options: list[GateOption]) -> None:
        """Test GateResult with default additional_input."""
        result = GateResult(
            selected_option=sample_options[0],
            route="next_agent",
        )
        assert result.additional_input == {}


class TestHumanGateHandler:
    """Tests for the HumanGateHandler class."""

    def test_init_defaults(self) -> None:
        """Test handler initialization with defaults."""
        handler = HumanGateHandler()
        assert handler.skip_gates is False
        assert handler.console is not None

    def test_init_with_skip_gates(self) -> None:
        """Test handler initialization with skip_gates=True."""
        handler = HumanGateHandler(skip_gates=True)
        assert handler.skip_gates is True

    def test_init_with_console(self, mock_console: MagicMock) -> None:
        """Test handler initialization with custom console."""
        handler = HumanGateHandler(console=mock_console)
        assert handler.console is mock_console


class TestHumanGateHandlerSkipGates:
    """Tests for --skip-gates mode (auto-selection)."""

    @pytest.mark.asyncio
    async def test_skip_gates_auto_selects_first_option(
        self,
        mock_console: MagicMock,
        human_gate_agent: AgentDef,
        sample_options: list[GateOption],
    ) -> None:
        """Test that skip_gates mode auto-selects the first option."""
        handler = HumanGateHandler(console=mock_console, skip_gates=True)
        context = {"agent1": {"output": "Test output"}}

        result = await handler.handle_gate(human_gate_agent, context)

        assert result.selected_option == sample_options[0]
        assert result.route == "next_agent"
        assert result.additional_input == {}
        # Verify console output indicates auto-selection
        mock_console.print.assert_called()

    @pytest.mark.asyncio
    async def test_skip_gates_does_not_collect_prompt_for(
        self,
        mock_console: MagicMock,
        human_gate_agent_with_prompt_for: AgentDef,
    ) -> None:
        """Test that skip_gates mode does not collect additional input."""
        handler = HumanGateHandler(console=mock_console, skip_gates=True)
        context = {}

        result = await handler.handle_gate(human_gate_agent_with_prompt_for, context)

        # Should auto-select first option but not collect additional input
        assert result.selected_option.value == "approved_with_feedback"
        assert result.additional_input == {}  # No input collected in skip mode


class TestHumanGateHandlerInteractive:
    """Tests for interactive mode with mocked terminal input."""

    @pytest.mark.asyncio
    async def test_handle_gate_no_options_raises_error(
        self,
        mock_console: MagicMock,
        human_gate_agent_no_options: AgentDef,
    ) -> None:
        """Test that gate without options raises HumanGateError."""
        handler = HumanGateHandler(console=mock_console)
        context = {}

        with pytest.raises(HumanGateError) as exc_info:
            await handler.handle_gate(human_gate_agent_no_options, context)

        assert "no options defined" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_display_and_select_option_1(
        self,
        mock_console: MagicMock,
        human_gate_agent: AgentDef,
        sample_options: list[GateOption],
    ) -> None:
        """Test selecting option 1 via Prompt.ask."""
        handler = HumanGateHandler(console=mock_console, skip_gates=False)
        context = {"agent1": {"output": "Review this content"}}

        with patch("conductor.gates.human.Prompt.ask", return_value="1"):
            result = await handler.handle_gate(human_gate_agent, context)

        assert result.selected_option == sample_options[0]
        assert result.route == "next_agent"

    @pytest.mark.asyncio
    async def test_display_and_select_option_2(
        self,
        mock_console: MagicMock,
        human_gate_agent: AgentDef,
        sample_options: list[GateOption],
    ) -> None:
        """Test selecting option 2 via Prompt.ask."""
        handler = HumanGateHandler(console=mock_console, skip_gates=False)
        context = {"agent1": {"output": "Review this content"}}

        with patch("conductor.gates.human.Prompt.ask", return_value="2"):
            result = await handler.handle_gate(human_gate_agent, context)

        assert result.selected_option == sample_options[1]
        assert result.route == "revision_agent"

    @pytest.mark.asyncio
    async def test_display_and_select_option_3_routes_to_end(
        self,
        mock_console: MagicMock,
        human_gate_agent: AgentDef,
        sample_options: list[GateOption],
    ) -> None:
        """Test selecting option 3 which routes to $end."""
        handler = HumanGateHandler(console=mock_console, skip_gates=False)
        context = {"agent1": {"output": "Review this content"}}

        with patch("conductor.gates.human.Prompt.ask", return_value="3"):
            result = await handler.handle_gate(human_gate_agent, context)

        assert result.selected_option == sample_options[2]
        assert result.route == "$end"

    @pytest.mark.asyncio
    async def test_collect_additional_input(
        self,
        mock_console: MagicMock,
        human_gate_agent_with_prompt_for: AgentDef,
    ) -> None:
        """Test collecting additional input via prompt_for."""
        handler = HumanGateHandler(console=mock_console, skip_gates=False)
        context = {}

        # First call returns option selection, second returns feedback text
        with patch(
            "conductor.gates.human.Prompt.ask",
            side_effect=["1", "This is my feedback"],
        ):
            result = await handler.handle_gate(human_gate_agent_with_prompt_for, context)

        assert result.selected_option.value == "approved_with_feedback"
        assert result.additional_input == {"feedback": "This is my feedback"}

    @pytest.mark.asyncio
    async def test_prompt_rendered_with_context(
        self,
        mock_console: MagicMock,
        human_gate_agent: AgentDef,
    ) -> None:
        """Test that prompt template is rendered with context."""
        handler = HumanGateHandler(console=mock_console, skip_gates=False)
        context = {"agent1": {"output": "Generated content here"}}

        with (
            patch("conductor.gates.human.Prompt.ask", return_value="1"),
            patch("conductor.gates.human.Panel") as mock_panel,
        ):
            await handler.handle_gate(human_gate_agent, context)

            # Verify Panel was called with rendered content
            mock_panel.assert_called()
            panel_args = mock_panel.call_args
            # First positional arg should be the rendered prompt
            rendered_prompt = panel_args[0][0]
            assert "Generated content here" in rendered_prompt


class TestHumanGateHandlerAutoSelect:
    """Tests for the _auto_select method."""

    def test_auto_select_returns_gate_result(
        self,
        mock_console: MagicMock,
        sample_options: list[GateOption],
    ) -> None:
        """Test that _auto_select returns a proper GateResult."""
        handler = HumanGateHandler(console=mock_console, skip_gates=True)

        result = handler._auto_select(sample_options[0])

        assert isinstance(result, GateResult)
        assert result.selected_option == sample_options[0]
        assert result.route == "next_agent"
        assert result.additional_input == {}

    def test_auto_select_prints_message(
        self,
        mock_console: MagicMock,
        sample_options: list[GateOption],
    ) -> None:
        """Test that _auto_select prints an informative message."""
        handler = HumanGateHandler(console=mock_console, skip_gates=True)

        handler._auto_select(sample_options[0])

        mock_console.print.assert_called()
        call_args = str(mock_console.print.call_args)
        assert "--skip-gates" in call_args or "Auto-selecting" in call_args


class TestMaxIterationsPromptResult:
    """Tests for MaxIterationsPromptResult dataclass."""

    def test_prompt_result_continue(self) -> None:
        """Test creating a result that continues execution."""
        from conductor.gates.human import MaxIterationsPromptResult

        result = MaxIterationsPromptResult(
            continue_execution=True,
            additional_iterations=10,
        )
        assert result.continue_execution is True
        assert result.additional_iterations == 10

    def test_prompt_result_stop(self) -> None:
        """Test creating a result that stops execution."""
        from conductor.gates.human import MaxIterationsPromptResult

        result = MaxIterationsPromptResult(
            continue_execution=False,
            additional_iterations=0,
        )
        assert result.continue_execution is False
        assert result.additional_iterations == 0


class TestMaxIterationsHandler:
    """Tests for MaxIterationsHandler class."""

    def test_init_defaults(self) -> None:
        """Test handler initialization with defaults."""
        from conductor.gates.human import MaxIterationsHandler

        handler = MaxIterationsHandler()
        assert handler.skip_gates is False
        assert handler.console is not None

    def test_init_with_skip_gates(self) -> None:
        """Test handler initialization with skip_gates=True."""
        from conductor.gates.human import MaxIterationsHandler

        handler = MaxIterationsHandler(skip_gates=True)
        assert handler.skip_gates is True

    def test_init_with_console(self, mock_console: MagicMock) -> None:
        """Test handler initialization with custom console."""
        from conductor.gates.human import MaxIterationsHandler

        handler = MaxIterationsHandler(console=mock_console)
        assert handler.console is mock_console


class TestMaxIterationsHandlerSkipGates:
    """Tests for --skip-gates mode (auto-stop)."""

    @pytest.mark.asyncio
    async def test_skip_gates_auto_stops(self, mock_console: MagicMock) -> None:
        """Test that skip_gates mode auto-stops without prompting."""
        from conductor.gates.human import MaxIterationsHandler

        handler = MaxIterationsHandler(console=mock_console, skip_gates=True)

        result = await handler.handle_limit_reached(
            current_iteration=10,
            max_iterations=10,
            agent_history=["agent1", "agent2", "agent3"],
        )

        assert result.continue_execution is False
        assert result.additional_iterations == 0
        # Verify console output indicates auto-stop
        mock_console.print.assert_called()

    @pytest.mark.asyncio
    async def test_skip_gates_with_empty_history(self, mock_console: MagicMock) -> None:
        """Test skip_gates mode with empty agent history."""
        from conductor.gates.human import MaxIterationsHandler

        handler = MaxIterationsHandler(console=mock_console, skip_gates=True)

        result = await handler.handle_limit_reached(
            current_iteration=5,
            max_iterations=5,
            agent_history=[],
        )

        assert result.continue_execution is False
        assert result.additional_iterations == 0


class TestMaxIterationsHandlerInteractive:
    """Tests for interactive mode with mocked terminal input."""

    @pytest.mark.asyncio
    async def test_user_enters_positive_number(self, mock_console: MagicMock) -> None:
        """Test that user entering positive number continues execution."""
        from conductor.gates.human import MaxIterationsHandler

        handler = MaxIterationsHandler(console=mock_console, skip_gates=False)

        # Mock IntPrompt.ask to return 5
        with patch("conductor.gates.human.IntPrompt.ask", return_value=5):
            result = await handler.handle_limit_reached(
                current_iteration=10,
                max_iterations=10,
                agent_history=["agent1", "agent2"],
            )

        assert result.continue_execution is True
        assert result.additional_iterations == 5

    @pytest.mark.asyncio
    async def test_user_enters_zero(self, mock_console: MagicMock) -> None:
        """Test that user entering 0 stops execution."""
        from conductor.gates.human import MaxIterationsHandler

        handler = MaxIterationsHandler(console=mock_console, skip_gates=False)

        # Mock IntPrompt.ask to return 0
        with patch("conductor.gates.human.IntPrompt.ask", return_value=0):
            result = await handler.handle_limit_reached(
                current_iteration=10,
                max_iterations=10,
                agent_history=["agent1", "agent2"],
            )

        assert result.continue_execution is False
        assert result.additional_iterations == 0

    @pytest.mark.asyncio
    async def test_user_enters_negative_number(self, mock_console: MagicMock) -> None:
        """Test that user entering negative number stops execution."""
        from conductor.gates.human import MaxIterationsHandler

        handler = MaxIterationsHandler(console=mock_console, skip_gates=False)

        # Mock IntPrompt.ask to return -5 (should be treated as 0)
        with patch("conductor.gates.human.IntPrompt.ask", return_value=-5):
            result = await handler.handle_limit_reached(
                current_iteration=10,
                max_iterations=10,
                agent_history=["agent1", "agent2"],
            )

        assert result.continue_execution is False
        assert result.additional_iterations == 0

    @pytest.mark.asyncio
    async def test_panel_displays_iteration_info(self, mock_console: MagicMock) -> None:
        """Test that panel displays iteration information."""
        from conductor.gates.human import MaxIterationsHandler

        handler = MaxIterationsHandler(console=mock_console, skip_gates=False)

        with (
            patch("conductor.gates.human.IntPrompt.ask", return_value=0),
            patch("conductor.gates.human.Panel") as mock_panel,
        ):
            await handler.handle_limit_reached(
                current_iteration=10,
                max_iterations=10,
                agent_history=["agent1", "agent2", "agent3"],
            )

            # Verify Panel was called with iteration info
            mock_panel.assert_called()
            panel_args = mock_panel.call_args
            panel_content = panel_args[0][0]
            assert "10/10" in panel_content or "10" in panel_content

    @pytest.mark.asyncio
    async def test_panel_shows_agent_history(self, mock_console: MagicMock) -> None:
        """Test that panel shows recent agent history."""
        from conductor.gates.human import MaxIterationsHandler

        handler = MaxIterationsHandler(console=mock_console, skip_gates=False)

        with (
            patch("conductor.gates.human.IntPrompt.ask", return_value=0),
            patch("conductor.gates.human.Panel") as mock_panel,
        ):
            await handler.handle_limit_reached(
                current_iteration=5,
                max_iterations=5,
                agent_history=["agent1", "agent2", "agent3", "agent2", "agent3"],
            )

            # Verify Panel was called with agent history
            mock_panel.assert_called()
            panel_args = mock_panel.call_args
            panel_content = panel_args[0][0]
            assert "agent" in panel_content.lower()

    @pytest.mark.asyncio
    async def test_detects_potential_loop(self, mock_console: MagicMock) -> None:
        """Test that handler warns about potential loops."""
        from conductor.gates.human import MaxIterationsHandler

        handler = MaxIterationsHandler(console=mock_console, skip_gates=False)

        # Create a repeating pattern that suggests a loop
        with (
            patch("conductor.gates.human.IntPrompt.ask", return_value=0),
            patch("conductor.gates.human.Panel") as mock_panel,
        ):
            await handler.handle_limit_reached(
                current_iteration=6,
                max_iterations=6,
                agent_history=["loop_agent", "loop_agent", "loop_agent"],
            )

            # Verify Panel was called with loop warning
            mock_panel.assert_called()
            panel_args = mock_panel.call_args
            panel_content = panel_args[0][0]
            assert "loop" in panel_content.lower()
