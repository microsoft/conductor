"""The New-run screen for the Fleet Manager TUI (Fleet Manager E12).

Per the design's *Launch model: viewer, not supervisor*: this screen's
entire job is to gather a workflow reference and its inputs, then hand off
to :func:`conductor.fleet.launch.launch_workflow` -- which itself delegates
to :func:`conductor.cli.bg_runner.launch_background` -- and forget. Once a
launch succeeds this screen pops back to Runs, where the new run appears on
that screen's own next poll tick; this screen never tracks the launched
run's lifecycle itself.

Two steps, both driven by explicit user action (never a poll timer, since
both can touch the network -- resolving a registry reference can fetch an
index/workflow file, and the launch itself waits on the child's dashboard
and run record):

1. Enter a workflow reference (a file path or registry ref) and resolve it
   via :func:`conductor.fleet.launch.resolve_workflow`, rendering a
   :class:`~conductor.config.schema.InputDef`-driven form (E12-T3): one
   widget per declared input (a ``Checkbox`` for ``boolean``, an ``Input``
   for the other four types), required inputs marked, defaults pre-filled,
   descriptions shown as label text.
2. Submit the form via :func:`conductor.fleet.launch.launch_workflow`, which
   validates/coerces every value against its declared type before the
   launch is attempted. Any failure -- a missing required field, a bad
   value for the declared type, or ``launch_background()`` itself failing
   (including its own D2 run-record-poll timeout) -- is rendered as text in
   this screen, never a traceback.
"""

from __future__ import annotations

import asyncio
import json
import logging

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Footer, Header, Input, Label, Static

from conductor.config.schema import InputDef
from conductor.console import styled
from conductor.fleet.launch import LaunchError, ResolvedWorkflow, launch_workflow, resolve_workflow

logger = logging.getLogger(__name__)


def _default_to_raw(input_def: InputDef) -> str:
    """Render an ``InputDef.default`` as the raw string an ``Input`` widget
    would hold -- ``array``/``object`` defaults are JSON-encoded, matching
    the JSON representation :func:`conductor.fleet.launch.coerce_input_value`
    expects back on submission."""
    if input_def.default is None:
        return ""
    if input_def.type in ("array", "object"):
        return json.dumps(input_def.default)
    return str(input_def.default)


def _field_label(name: str, input_def: InputDef) -> Text:
    """Render a form field's label: name, a required marker, and its
    description (E12-T3) -- mirrors the field set ``conductor show`` /
    the Registries drill-down's inputs screen already render.

    ``name`` and ``input_def.description`` are data, not authored Rich
    markup, so the label is built as a ``Text`` -- a value containing e.g.
    ``[/red]`` renders as literal text instead of raising ``MarkupError``,
    without routing it through ``rich.markup.escape`` (which is not
    byte-exact and cannot round-trip a backslash before a bracket).
    """
    text = Text(f"{name} ({input_def.type})")
    if input_def.required:
        text.append(" *")
    if input_def.description:
        text.append(f" — {input_def.description}")
    return text


class NewRunScreen(Screen):
    """Resolve a workflow reference, render its inputs as a form, and
    launch it in the background (E12)."""

    BINDINGS = [("escape", "back", "Back")]

    def __init__(self) -> None:
        super().__init__()
        self._resolved: ResolvedWorkflow | None = None
        self._input_widgets: dict[str, Input | Checkbox] = {}
        self._widget_names: dict[Input | Checkbox, str] = {}
        """Reverse of ``_input_widgets`` -- maps a mounted widget back to its
        declared input name, used by ``on_checkbox_changed`` to know which
        field a toggled ``Checkbox`` belongs to without relying on its
        (opaque, sanitized) widget id."""
        self._checkbox_touched: set[str] = set()
        """Names of boolean inputs the user has explicitly toggled (or that
        were pre-filled from a declared default) -- distinguishes "the user
        chose False" from "never set", so a required boolean with no
        default cannot be silently satisfied by an untouched ``Checkbox``."""
        self._launching = False
        """Synchronous guard against a second Launch click starting a
        duplicate (potentially billable) background run while one launch
        is already in flight."""
        self._resolve_generation = 0
        """Bumped at the start of every ``action_resolve`` call so an
        out-of-order (slower, superseded) resolve worker can detect it is
        stale and discard its result instead of overwriting a newer one."""

    def action_back(self) -> None:
        """Pop back to the Runs screen -- bound to ``escape``."""
        self.app.pop_screen()

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(
            Label("Workflow (file path or registry reference):"),
            Input(
                placeholder="e.g. ./my-workflow.yaml or qa-bot@my-registry",
                id="workflow-ref",
            ),
            Button("Resolve", id="resolve-button", variant="primary"),
            Static(id="resolve-message"),
            Vertical(id="input-fields"),
            Button("Launch", id="launch-button", variant="success", disabled=True),
            Static(id="launch-message"),
            id="new-run-form",
        )
        yield Footer()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Pressing Enter in the workflow-reference field resolves it,
        mirroring the "Resolve" button."""
        if event.input.id == "workflow-ref":
            self.action_resolve()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "resolve-button":
            self.action_resolve()
        elif event.button.id == "launch-button":
            if self._launching:
                return
            self._launching = True
            event.button.disabled = True
            self.action_launch()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        """Mark a boolean input as explicitly set once the user toggles it
        -- distinguishes a deliberate ``False`` from an untouched, still-unset
        required field (see :attr:`_checkbox_touched`)."""
        name = self._widget_names.get(event.checkbox)
        if name is not None:
            self._checkbox_touched.add(name)

    @work
    async def action_resolve(self) -> None:
        """Resolve the entered workflow reference and (re)build the input
        form from its declared ``wf.input`` (E12-T1/E12-T3).

        Run as an awaited worker -- resolving a registry reference can
        fetch an index or workflow file over the network
        (``registry/cache.py::resolve_and_fetch``), so this is dispatched
        via ``asyncio.to_thread`` rather than run inline, and only ever
        triggered by this explicit action (there is no poll timer on this
        screen to begin with).

        Invalidates the previously resolved workflow and disables Launch
        *synchronously*, before the network-capable resolve is awaited, so
        Launch can never fire against a stale/mismatched workflow while a
        new reference is resolving. Tags this call with a generation
        counter so that if a second resolve is triggered before this one
        finishes, this (now-stale) call discards its result instead of
        overwriting the newer one (latest-request-wins).
        """
        self._resolve_generation += 1
        generation = self._resolve_generation
        self._resolved = None
        launch_button = self.query_one("#launch-button", Button)
        launch_button.disabled = True

        ref = self.query_one("#workflow-ref", Input).value.strip()
        message = self.query_one("#resolve-message", Static)
        self.query_one("#launch-message", Static).update("")

        if not ref:
            message.update("[red]Enter a workflow path or registry reference.[/red]")
            await self._rebuild_input_fields({})
            return

        message.update("[dim]Resolving…[/dim]")
        try:
            resolved = await asyncio.to_thread(resolve_workflow, ref)
        except LaunchError as e:
            if generation != self._resolve_generation:
                return
            logger.warning("Failed to resolve workflow %r", ref, exc_info=True)
            message.update(styled("[red]{}[/red]", str(e)))
            await self._rebuild_input_fields({})
            return

        if generation != self._resolve_generation:
            # A newer resolve has since started (and already invalidated
            # ``self._resolved``/disabled Launch) -- this result is stale.
            return

        self._resolved = resolved
        message.update(styled("[green]Resolved:[/green] {}", resolved.name))
        await self._rebuild_input_fields(resolved.inputs)
        launch_button.disabled = False

    async def _rebuild_input_fields(self, inputs: dict[str, InputDef]) -> None:
        """Replace the input-fields container's children with one
        label + widget pair per declared input, defaults pre-filled
        (E12-T3)."""
        container = self.query_one("#input-fields", Vertical)
        await container.remove_children()
        self._input_widgets = {}
        self._widget_names = {}
        self._checkbox_touched = set()

        widgets: list[Label | Input | Checkbox] = []
        for index, (name, input_def) in enumerate(inputs.items()):
            widgets.append(Label(_field_label(name, input_def)))
            # An opaque, index-based id -- a declared input name (e.g.
            # "user.email" or "full name") is schema-valid but not a legal
            # Textual widget identifier, which would raise BadIdentifier.
            # The real name is retained via ``_input_widgets``/``_widget_names``.
            widget_id = f"input-{index}"
            if input_def.type == "boolean":
                has_default = input_def.default is not None
                checkbox = Checkbox(
                    value=bool(input_def.default) if has_default else False,
                    id=widget_id,
                )
                widgets.append(checkbox)
                self._input_widgets[name] = checkbox
                self._widget_names[checkbox] = name
                if has_default:
                    # A declared default is already a legitimate value --
                    # only an untouched, default-less checkbox stays unset.
                    self._checkbox_touched.add(name)
            else:
                field_input = Input(value=_default_to_raw(input_def), id=widget_id)
                widgets.append(field_input)
                self._input_widgets[name] = field_input
                self._widget_names[field_input] = name

        if widgets:
            await container.mount_all(widgets)

    def _raw_value_for(self, name: str) -> str:
        """Read a form widget's current value as the raw string
        :func:`conductor.fleet.launch.coerce_input_value` expects.

        An untouched, default-less boolean ``Checkbox`` returns ``""``
        (not-provided) rather than ``"false"`` -- an unchecked box cannot
        represent "the user hasn't answered yet", so treating it as a
        real ``False`` would silently satisfy a required field the user
        never actually set. ``build_launch_inputs`` already rejects a
        blank required value, so this preserves the "unset" state until
        :meth:`on_checkbox_changed` marks it touched.
        """
        widget = self._input_widgets[name]
        if isinstance(widget, Checkbox):
            if name not in self._checkbox_touched:
                return ""
            return "true" if widget.value else "false"
        return widget.value

    @work
    async def action_launch(self) -> None:
        """Coerce the form's values and launch the resolved workflow (E12-T2).

        Run as an awaited worker -- ``launch_workflow`` blocks on
        ``launch_background()``'s own dashboard-reachability and D2
        run-record-poll waits (each up to 15s), so this is dispatched via
        ``asyncio.to_thread`` rather than run inline, and only ever
        triggered by this explicit action.

        The Launch button is already disabled synchronously by
        ``on_button_pressed`` before this worker starts (a guard against a
        second click starting a duplicate, potentially billable run); it is
        re-enabled here only on failure -- on success the screen pops away
        entirely, so there is nothing left to re-enable.
        """
        resolved = self._resolved
        launch_button = self.query_one("#launch-button", Button)
        if resolved is None:
            # Defensive: Launch is disabled whenever there is no resolved
            # workflow, so this should be unreachable in practice. Reset the
            # click guard but leave the button disabled -- there is nothing
            # valid to launch.
            self._launching = False
            return

        message = self.query_one("#launch-message", Static)
        message.update("[dim]Launching…[/dim]")
        raw_values = {name: self._raw_value_for(name) for name in resolved.inputs}

        try:
            launch = await asyncio.to_thread(
                launch_workflow, resolved.path, raw_values, resolved.inputs
            )
        except LaunchError as e:
            logger.warning("Failed to launch workflow %s", resolved.path, exc_info=True)
            message.update(styled("[red]{}[/red]", str(e)))
            self._launching = False
            launch_button.disabled = False
            return

        # Success: hand off to the Runs screen, whose own poll timer will
        # pick up the new (already-discoverable, per D2) run record --
        # this screen never tracks the launched run itself (viewer, not
        # supervisor).
        self.app.pop_screen()
        self.app.notify(f"Launched: {launch.url}")
