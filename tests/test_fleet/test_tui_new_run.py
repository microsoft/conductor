"""Pilot tests for the Fleet Manager TUI's New-run screen (Fleet Manager E12).

Uses Textual's ``App.run_test()`` to drive a real (headless) instance of
:class:`~conductor.fleet.tui.app.FleetApp` against a real, temp
path-backed workflow file (matching E11's "real over stubbed" convention),
covering E12-T6:

- ``n`` from the Runs screen pushes the New-run screen; ``escape`` returns.
- Resolving a workflow reference renders a form generated from its
  declared ``wf.input`` -- one widget per input, required inputs marked,
  defaults pre-filled.
- Submitting invokes :func:`conductor.fleet.launch.launch_workflow` with
  the form's values coerced to each input's declared type.
- A successful launch pops back to the Runs screen, and the launched run
  (once its record is written, mirroring what the real background child
  would do) appears in the list on the next refresh.
- A launch failure (including a simulated D2 record-poll timeout) is
  reported in the screen, not raised as a traceback.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from textual.widgets import DataTable, Input, Static

from conductor.cli.bg_runner import BackgroundLaunch
from conductor.fleet.launch import LaunchError
from conductor.fleet.records import RunRecord, write_run_record
from conductor.fleet.tui.app import FleetApp
from conductor.fleet.tui.screens.new_run import NewRunScreen
from conductor.fleet.tui.screens.runs import RunsScreen
from tests.test_fleet.conftest import settle

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def fleet_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect both the run-record directory and the legacy ``.pid`` directory.

    Mirrors the ``fleet_env`` fixture used across the other TUI pilot test
    modules.
    """
    home = tmp_path / "conductor_home"
    home.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))

    legacy_dir = tmp_path / "legacy_runs"
    legacy_dir.mkdir()
    monkeypatch.setattr("conductor.cli.pid.pid_dir", lambda: legacy_dir)

    return home


_FIXTURE_WORKFLOW_YAML = """\
workflow:
  name: fixture-workflow
  description: A fixture workflow for the New Run form
  entry_point: helper
  input:
    question:
      type: string
      required: true
      description: The question to answer
    verbose:
      type: boolean
      required: false
      default: false
      description: Enable verbose output
    retries:
      type: number
      required: false
      default: 3

agents:
  - name: helper
    model: copilot
    prompt: Say hello
"""


@pytest.fixture()
def fixture_workflow(tmp_path: Path) -> Path:
    path = tmp_path / "fixture-workflow.yaml"
    path.write_text(_FIXTURE_WORKFLOW_YAML)
    return path


async def _goto_new_run(pilot) -> None:
    """Navigate from the (already-mounted) Runs screen to New Run."""
    await settle(pilot)
    await pilot.press("n")
    await settle(pilot)


async def _resolve(pilot, path: Path) -> None:
    """Type ``path`` into the workflow-reference field and resolve it."""
    ref_input = pilot.app.screen.query_one("#workflow-ref", Input)
    ref_input.value = str(path)
    await pilot.press("ctrl+r")
    await pilot.pause(0.3)


def _write_bg_record(run_id: str, workflow_path: Path, *, port: int = 8080) -> RunRecord:
    """Write a run record as the real background child would after a
    successful ``launch_background()`` call -- used to simulate "the
    launched run appears in the list on the next refresh" without
    actually spawning a subprocess."""
    log_path = workflow_path.parent / f"{run_id}.events.jsonl"
    log_path.write_text("")
    record = RunRecord(
        run_id=run_id,
        pid=os.getpid(),
        workflow_path=str(workflow_path),
        workflow_name=workflow_path.stem,
        started_at="2026-01-01T00:00:00+00:00",
        event_log_path=str(log_path),
        port=port,
        mode="bg",
        checkpoint_dir=None,
    )
    write_run_record(record)
    return record


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


class TestNewRunNavigation:
    async def test_n_pushes_new_run_screen(self, fleet_env: Path) -> None:
        app = FleetApp()
        async with app.run_test() as pilot:
            assert isinstance(app.screen, RunsScreen)
            await _goto_new_run(pilot)

            assert isinstance(app.screen, NewRunScreen)

    async def test_escape_returns_to_runs(self, fleet_env: Path) -> None:
        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_new_run(pilot)
            assert isinstance(app.screen, NewRunScreen)

            await pilot.press("escape")
            await settle(pilot)

            assert isinstance(app.screen, RunsScreen)


# ---------------------------------------------------------------------------
# Form rendering (E12-T3)
# ---------------------------------------------------------------------------


def _form_text(screen) -> str:
    """Flatten every heading/description line the rendered form shows.

    Each input is now a ``.field`` container holding a heading, an optional
    description, and the widget -- so the direct children of
    ``#input-fields`` are containers, not the labels themselves, and have
    nothing renderable of their own to assert against.
    """
    parts: list[str] = []
    for widget in screen.query(".field-heading"):
        parts.append(str(widget.render()))
    for widget in screen.query(".field-description"):
        parts.append(str(widget.render()))
    return "\n".join(parts)


class TestNewRunForm:
    async def test_form_renders_from_resolved_inputs(
        self, fleet_env: Path, fixture_workflow: Path
    ) -> None:
        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_new_run(pilot)
            await _resolve(pilot, fixture_workflow)

            message = app.screen.query_one("#resolve-message", Static)
            assert "fixture-workflow" in str(message.render())

            question_input = app.screen._input_widgets["question"]
            assert question_input.value == ""  # no default

            verbose_checkbox = app.screen._input_widgets["verbose"]
            assert verbose_checkbox.value is False  # default pre-filled

            retries_input = app.screen._input_widgets["retries"]
            assert retries_input.value == "3"  # default pre-filled

            assert app.screen._resolved is not None  # launchable

    async def test_required_field_marked_in_label(
        self, fleet_env: Path, fixture_workflow: Path
    ) -> None:
        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_new_run(pilot)
            await _resolve(pilot, fixture_workflow)

            text = _form_text(app.screen)
            assert "question" in text
            assert "required" in text  # required marker

    async def test_unresolvable_reference_shows_error_and_disables_launch(
        self, fleet_env: Path, tmp_path: Path
    ) -> None:
        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_new_run(pilot)
            await _resolve(pilot, tmp_path / "does-not-exist.yaml")

            message = app.screen.query_one("#resolve-message", Static)
            assert "not found" in str(message.render()).lower()

            assert app.screen._resolved is None  # not launchable


# ---------------------------------------------------------------------------
# Submission (E12-T2 / E12-T6)
# ---------------------------------------------------------------------------


class TestNewRunSubmission:
    async def test_submission_invokes_launcher_with_coerced_values(
        self, fleet_env: Path, fixture_workflow: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The value returned by launch_workflow itself just needs a `.url`.
        fake_result = Mock(url="http://127.0.0.1:8080")
        launch_mock = Mock(return_value=fake_result)
        monkeypatch.setattr("conductor.fleet.tui.screens.new_run.launch_workflow", launch_mock)

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_new_run(pilot)
            await _resolve(pilot, fixture_workflow)

            question_input = app.screen._input_widgets["question"]
            question_input.value = "What is Python?"
            verbose_checkbox = app.screen._input_widgets["verbose"]
            verbose_checkbox.value = True

            await pilot.press("ctrl+s")
            await pilot.pause(0.3)

        launch_mock.assert_called_once()
        args, _kwargs = launch_mock.call_args
        workflow_path, raw_values, input_defs = args[0], args[1], args[2]
        assert workflow_path == fixture_workflow
        assert raw_values["question"] == "What is Python?"
        assert raw_values["verbose"] == "true"
        assert raw_values["retries"] == "3"
        assert set(input_defs) == {"question", "verbose", "retries"}

    async def test_successful_launch_pops_to_runs_and_appears_on_next_refresh(
        self, fleet_env: Path, fixture_workflow: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fake_launch_workflow(workflow_path, raw_values, input_defs, **kwargs):
            record = _write_bg_record("launched01", workflow_path)
            return BackgroundLaunch(
                url="http://127.0.0.1:8080",
                stderr_log=fleet_env / "launched01.bg.stderr.log",
                stdout_log=fleet_env / "launched01.bg.stdout.log",
                run_id=record.run_id,
            )

        monkeypatch.setattr(
            "conductor.fleet.tui.screens.new_run.launch_workflow", _fake_launch_workflow
        )

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_new_run(pilot)
            await _resolve(pilot, fixture_workflow)

            question_input = app.screen._input_widgets["question"]
            question_input.value = "What is Python?"

            await pilot.press("ctrl+s")
            await pilot.pause(0.3)

            assert isinstance(app.screen, RunsScreen)

            # The Runs screen's own poll timer (or an explicit refresh)
            # picks up the newly-written record.
            app.screen.refresh_runs()
            await settle(pilot)

            table = app.screen.query_one(DataTable)
            rows = [table.get_row_at(i) for i in range(table.row_count)]
            assert any("fixture-workflow" in r[0] for r in rows)

    async def test_launch_with_no_run_record_written_shows_warning_notification(
        self, fleet_env: Path, fixture_workflow: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #435: a launch that succeeded but could not confirm its own
        discovery record must warn the user it will not appear on the Runs
        screen -- a ``Mock(...)`` here would leave ``.run_record_written``
        an auto-created truthy attribute and this branch structurally
        unreachable, so a real ``BackgroundLaunch`` is required."""
        launch = BackgroundLaunch(
            url="http://127.0.0.1:8080",
            stderr_log=fleet_env / "unregistered.bg.stderr.log",
            stdout_log=fleet_env / "unregistered.bg.stdout.log",
            run_id="unregist",
            run_record_written=False,
        )
        monkeypatch.setattr(
            "conductor.fleet.tui.screens.new_run.launch_workflow",
            Mock(return_value=launch),
        )

        notifications: list[tuple[str, str]] = []

        app = FleetApp()
        async with app.run_test() as pilot:
            original_notify = app.notify

            def _capture(message, **kwargs):
                notifications.append((message, str(kwargs.get("severity", "information"))))
                original_notify(message, **kwargs)

            with patch.object(app, "notify", _capture):
                await _goto_new_run(pilot)
                await _resolve(pilot, fixture_workflow)

                question_input = app.screen._input_widgets["question"]
                question_input.value = "What is Python?"

                await pilot.press("ctrl+s")
                await pilot.pause(0.3)

                assert isinstance(app.screen, RunsScreen)

        warnings = [message for message, severity in notifications if severity == "warning"]
        assert any("could not register itself for discovery" in m for m in warnings), notifications

    async def test_missing_required_field_shows_error_without_launching(
        self, fleet_env: Path, fixture_workflow: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real ``launch_workflow`` enforces required fields itself
        (via ``build_launch_inputs``) before ever calling
        ``launch_background`` -- patch that innermost call to prove it is
        never reached."""
        bg_launch_mock = Mock(side_effect=AssertionError("must not be called"))
        monkeypatch.setattr("conductor.cli.bg_runner.launch_background", bg_launch_mock)

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_new_run(pilot)
            await _resolve(pilot, fixture_workflow)
            # Leave the required "question" field blank.

            await pilot.press("ctrl+s")
            await pilot.pause(0.3)

            message = app.screen.query_one("#launch-message", Static)
            assert "question" in str(message.render()).lower()
            bg_launch_mock.assert_not_called()
            assert isinstance(app.screen, NewRunScreen)

    async def test_launch_failure_including_record_poll_timeout_is_reported_in_ui(
        self, fleet_env: Path, fixture_workflow: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A launch_background() failure -- including the D2 run-record
        poll timeout -- must render its message in the screen, never a
        traceback, and must not navigate away from the form."""
        launch_mock = Mock(
            side_effect=LaunchError(
                "Background process did not report a run record within 15 seconds "
                "(run_id=abc12345). The background process was terminated."
            )
        )
        monkeypatch.setattr("conductor.fleet.tui.screens.new_run.launch_workflow", launch_mock)

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_new_run(pilot)
            await _resolve(pilot, fixture_workflow)

            question_input = app.screen._input_widgets["question"]
            question_input.value = "What is Python?"

            await pilot.press("ctrl+s")
            await pilot.pause(0.3)

            message = app.screen.query_one("#launch-message", Static)
            text = str(message.render())
            assert "run record within 15 seconds" in text
            assert isinstance(app.screen, NewRunScreen)


# ---------------------------------------------------------------------------
# Markup safety (review round 1)
# ---------------------------------------------------------------------------
#
# Input names/descriptions are data, not authored Rich markup -- a value
# containing e.g. "[/red]" must render as literal text, never raise
# rich.errors.MarkupError and crash the form.

_MARKUP_WORKFLOW_YAML = """\
workflow:
  name: markup-workflow
  entry_point: helper
  input:
    question:
      type: string
      required: true
      description: "[/red]evil description[/bold]"

agents:
  - name: helper
    model: copilot
    prompt: Say hello
"""


@pytest.fixture()
def markup_workflow(tmp_path: Path) -> Path:
    path = tmp_path / "markup-workflow.yaml"
    path.write_text(_MARKUP_WORKFLOW_YAML)
    return path


class TestNewRunMarkupSafety:
    async def test_field_label_escapes_markup_like_description(
        self, fleet_env: Path, markup_workflow: Path
    ) -> None:
        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_new_run(pilot)
            await _resolve(pilot, markup_workflow)

            # Resolving and rendering the form raised no MarkupError, and
            # the already-rendered label contains the literal source text
            # rather than having "[/red]"/"[/bold]" interpreted as markup.
            text = _form_text(app.screen)
            assert "[/red]evil description[/bold]" in text


# ---------------------------------------------------------------------------
# Opaque widget ids for schema-valid-but-unsafe input names (review round 1)
# ---------------------------------------------------------------------------

_DOTTED_NAME_WORKFLOW_YAML = """\
workflow:
  name: dotted-name-workflow
  entry_point: helper
  input:
    "user.email":
      type: string
      required: true
    "full name":
      type: string
      required: false
      default: ""

agents:
  - name: helper
    model: copilot
    prompt: Say hello
"""


@pytest.fixture()
def dotted_name_workflow(tmp_path: Path) -> Path:
    path = tmp_path / "dotted-name-workflow.yaml"
    path.write_text(_DOTTED_NAME_WORKFLOW_YAML)
    return path


class TestNewRunOpaqueWidgetIds:
    async def test_schema_valid_but_unsafe_names_do_not_crash_form_rendering(
        self, fleet_env: Path, dotted_name_workflow: Path
    ) -> None:
        """Input names like ``user.email``/``full name`` are schema-valid
        but not legal Textual widget identifiers -- resolving must not
        raise ``BadIdentifier``, and the real name must still be reachable
        via the screen's widget map."""
        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_new_run(pilot)
            await _resolve(pilot, dotted_name_workflow)

            message = app.screen.query_one("#resolve-message", Static)
            assert "dotted-name-workflow" in str(message.render())

            assert set(app.screen._input_widgets) == {"user.email", "full name"}
            email_input = app.screen._input_widgets["user.email"]
            assert isinstance(email_input, Input)


# ---------------------------------------------------------------------------
# Required boolean without a default must stay "unset" (review round 1)
# ---------------------------------------------------------------------------

_REQUIRED_BOOLEAN_WORKFLOW_YAML = """\
workflow:
  name: required-boolean-workflow
  entry_point: helper
  input:
    confirm:
      type: boolean
      required: true

agents:
  - name: helper
    model: copilot
    prompt: Say hello
"""


@pytest.fixture()
def required_boolean_workflow(tmp_path: Path) -> Path:
    path = tmp_path / "required-boolean-workflow.yaml"
    path.write_text(_REQUIRED_BOOLEAN_WORKFLOW_YAML)
    return path


class TestNewRunRequiredBooleanUnset:
    async def test_untouched_required_boolean_without_default_rejects_launch(
        self, fleet_env: Path, required_boolean_workflow: Path
    ) -> None:
        """An unchecked Checkbox cannot represent "unset" -- a required
        boolean with no default must not be silently satisfied by the
        widget's default (unchecked/False) value; launching without
        touching it must be rejected as missing, not launched as False."""
        bg_launch_mock = Mock(side_effect=AssertionError("must not be called"))
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("conductor.cli.bg_runner.launch_background", bg_launch_mock)

            app = FleetApp()
            async with app.run_test() as pilot:
                await _goto_new_run(pilot)
                await _resolve(pilot, required_boolean_workflow)
                # Leave the required "confirm" checkbox untouched.

                await pilot.press("ctrl+s")
                await pilot.pause(0.3)

                message = app.screen.query_one("#launch-message", Static)
                assert "confirm" in str(message.render()).lower()
                bg_launch_mock.assert_not_called()
                assert isinstance(app.screen, NewRunScreen)

    async def test_toggling_the_checkbox_allows_launch(
        self, fleet_env: Path, required_boolean_workflow: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Once the user explicitly toggles the checkbox, its value (even
        False) is a real, provided answer and the launch proceeds."""
        fake_result = Mock(url="http://127.0.0.1:8080")
        launch_mock = Mock(return_value=fake_result)
        monkeypatch.setattr("conductor.fleet.tui.screens.new_run.launch_workflow", launch_mock)

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_new_run(pilot)
            await _resolve(pilot, required_boolean_workflow)

            checkbox = app.screen._input_widgets["confirm"]
            await pilot.click(checkbox)
            await settle(pilot)

            await pilot.press("ctrl+s")
            await pilot.pause(0.3)

        launch_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Launch guard against duplicate clicks (review round 1)
# ---------------------------------------------------------------------------


class TestNewRunLaunchGuard:
    async def test_second_launch_keystroke_does_not_start_a_duplicate_run(
        self, fleet_env: Path, fixture_workflow: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second, rapid click must not start a duplicate (potentially
        billable) background run -- the button must already be disabled
        synchronously, on the very first click, before the (awaited) launch
        worker even completes."""
        call_count = 0
        release_launch = threading.Event()

        def _blocking_launch_workflow(workflow_path, raw_values, input_defs, **kwargs):
            # ``launch_workflow`` runs via ``asyncio.to_thread``, so blocking
            # this real OS thread (rather than the event loop) is safe and
            # lets the test assert the button's state while a launch is
            # still genuinely in flight.
            nonlocal call_count
            call_count += 1
            release_launch.wait(timeout=2)
            return Mock(url="http://127.0.0.1:8080")

        monkeypatch.setattr(
            "conductor.fleet.tui.screens.new_run.launch_workflow", _blocking_launch_workflow
        )

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_new_run(pilot)
            await _resolve(pilot, fixture_workflow)

            question_input = app.screen._input_widgets["question"]
            question_input.value = "What is Python?"

            await pilot.press("ctrl+s")
            # Not `settle`: the launch worker is genuinely blocked on
            # `release_launch` (a real OS thread wait, not something
            # `wait_for_complete()` should be made to sit through), so this
            # must observe the in-flight state rather than wait it out.
            await pilot.pause()

            # Flagged synchronously -- before the still in-flight launch
            # worker has finished.
            assert app.screen._launching is True

            # A second keystroke while in flight must not start a second launch.
            await pilot.press("ctrl+s")
            await pilot.pause()

            release_launch.set()
            await pilot.pause(0.3)

        assert call_count == 1


# ---------------------------------------------------------------------------
# Resolve invalidates and disables launch; latest-request-wins (review round 1)
# ---------------------------------------------------------------------------


class TestNewRunResolveRace:
    async def test_launch_disabled_immediately_when_a_new_resolve_starts(
        self, fleet_env: Path, fixture_workflow: Path, tmp_path: Path
    ) -> None:
        """Starting a new resolve must synchronously invalidate the
        previously resolved workflow and disable Launch -- Launch must
        never be launchable while resolution against a new reference is
        still in flight."""
        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_new_run(pilot)
            await _resolve(pilot, fixture_workflow)

            assert app.screen._resolved is not None  # launchable

            ref_input = app.screen.query_one("#workflow-ref", Input)
            ref_input.value = str(tmp_path / "does-not-exist.yaml")
            await pilot.press("ctrl+r")
            # No pause() yet -- assert immediately after the click is
            # dispatched (the resolve worker's synchronous prefix has run,
            # invalidating the prior result before the network-capable
            # part of the resolve is awaited).
            assert app.screen._resolved is None  # not launchable
            await pilot.pause(0.3)

    async def test_out_of_order_resolve_does_not_overwrite_newer_result(
        self, fleet_env: Path, fixture_workflow: Path, dotted_name_workflow: Path
    ) -> None:
        """If an earlier, slower resolve finishes after a later, faster one,
        the earlier (stale) result must not overwrite the newer one
        (latest-request-wins)."""
        from conductor.fleet import launch as launch_module

        real_resolve_workflow = launch_module.resolve_workflow
        second_call_started = threading.Event()

        def _tracking_resolve_workflow(ref: str):
            if ref == str(fixture_workflow):
                # The first (slower) resolve waits for the second call to
                # start before returning, so it finishes *after* it --
                # simulating an out-of-order (stale) response.
                second_call_started.wait(timeout=2)
            else:
                second_call_started.set()
            return real_resolve_workflow(ref)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "conductor.fleet.tui.screens.new_run.resolve_workflow",
                _tracking_resolve_workflow,
            )

            app = FleetApp()
            async with app.run_test() as pilot:
                await _goto_new_run(pilot)

                ref_input = app.screen.query_one("#workflow-ref", Input)
                ref_input.value = str(fixture_workflow)
                await pilot.press("ctrl+r")
                await pilot.pause(0.3)

                ref_input.value = str(dotted_name_workflow)
                await pilot.press("ctrl+r")
                await pilot.pause(1.5)

                assert app.screen._resolved is not None
                assert app.screen._resolved.name == "dotted-name-workflow"
