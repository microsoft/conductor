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
from typing import TYPE_CHECKING, cast

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Checkbox, Footer, Header, Input, Label, Static

from conductor.config.schema import InputDef
from conductor.console import styled
from conductor.fleet.launch import LaunchError, ResolvedWorkflow, launch_workflow, resolve_workflow

if TYPE_CHECKING:
    # The app module imports this screen module, so a top-level import of
    # FleetApp here would cycle (same reason runs.py defers it).
    from conductor.fleet.tui.app import FleetApp

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


def _field_heading(name: str, input_def: InputDef) -> Text:
    """Render a form field's heading: name, declared type, required marker.

    Deliberately excludes the description, which is rendered as its own
    wrapping line below (see :meth:`NewRunScreen._rebuild_input_fields`).
    Appending a workflow's prose to this line is what made the form
    unreadable: a real description runs to several hundred characters, and
    a heading is laid out ``width: auto``, so it extended past the right
    edge and took the field's own name off screen with it.

    ``name`` is data, not authored Rich markup, so the heading is built as
    a ``Text`` -- a value containing e.g. ``[/red]`` renders as literal
    text instead of raising ``MarkupError``, without routing it through
    ``rich.markup.escape`` (which is not byte-exact and cannot round-trip
    a backslash before a bracket).
    """
    text = Text(name, style="bold")
    text.append(f"  {input_def.type}", style="dim")
    if input_def.required:
        text.append("  required", style="italic yellow")
    return text


class NewRunScreen(Screen):
    """Resolve a workflow reference, render its inputs as a form, and
    launch it in the background (E12)."""

    DEFAULT_CSS = """
    NewRunScreen {
        /* Every rule here exists because its absence was visible: with no
           CSS at all, Textual laid every widget out `width: auto`, so a
           label ran off the right edge instead of wrapping, a Checkbox with
           no label collapsed to a truncated "X…", nothing had spacing, and
           the Launch button sat below a full-height scroller rather than
           where it could be seen. */
        layout: vertical;
    }

    #ref-row {
        height: auto;
        padding: 1 2 0 2;
    }

    #workflow-ref {
        width: 1fr;
    }


    #resolve-message {
        height: auto;
        padding: 0 2;
    }

    #input-fields {
        /* 1fr, so the field list absorbs the spare space and the launch bar
           below it stays on screen instead of being pushed off. */
        height: 1fr;
        padding: 1 2;
    }

    .field {
        height: auto;
        margin-bottom: 1;
    }

    .field-heading {
        width: 100%;
    }

    .field-description {
        /* width: 100% is what makes a long description wrap rather than
           run off the right edge -- the defect that made this form
           unreadable. */
        width: 100%;
        color: $text-muted;
        margin-bottom: 1;
    }

    .field-widget {
        width: 100%;
    }

    Checkbox.field-widget {
        /* auto, not 100%: a full-width checkbox draws its focus rule across
           the whole screen. It is legible because it now carries its own
           label (an unlabelled Checkbox is the "X…" blob). */
        width: auto;
    }

    #form-hint {
        height: auto;
        padding: 0 2;
        color: $text-muted;
    }

    #launch-message {
        height: auto;
        padding: 0 2;
    }

    #empty-inputs {
        color: $text-muted;
    }
    """

    # Two blocky buttons used to carry these actions: a primary "Resolve"
    # floating at the top right and a "Launch" stranded below a full-height
    # scroller. Both are keystrokes now -- Enter already resolved the
    # reference, so the Resolve button was a second way to do what the
    # field itself did, and a form whose submit control is off the bottom
    # of its own scroll region is worse than one you submit from anywhere.
    # Control keys (not bare letters) because a text input has focus for
    # most of this screen's life and would otherwise swallow them.
    BINDINGS = [
        ("escape", "back", "Back"),
        ("ctrl+r", "resolve", "Resolve"),
        ("ctrl+s", "launch", "Launch"),
    ]

    def __init__(self, initial_ref: str | None = None) -> None:
        """
        Args:
            initial_ref: A workflow reference to pre-fill and resolve on
                mount. Passed by the Registries drill-down so ``n`` on a
                workflow launches *that* workflow instead of dropping the
                user on an empty form to retype a reference they just
                navigated through.
        """
        super().__init__()
        self._initial_ref = initial_ref
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

    def _update_hint(self) -> None:
        """Refresh the line that says what this screen is waiting for.

        Replaces the affordance the Launch button used to carry through its
        ``disabled`` state: with no button, "you cannot launch yet" has to
        be said in words, and saying *why* is more useful than a greyed-out
        control was.
        """
        hint = self.query_one("#form-hint", Static)
        if self._resolved is None:
            hint.update(
                Text.from_markup(
                    "[dim]Enter a workflow reference above, then press "
                    "[/dim]enter[dim] to resolve it.[/dim]"
                )
            )
            return
        hint.update(Text.from_markup("[dim]Press [/dim]ctrl+s[dim] to launch this workflow.[/dim]"))

    def compose(self) -> ComposeResult:
        yield Header()
        yield Horizontal(
            Input(
                placeholder="Workflow: ./my-workflow.yaml or qa-bot@my-registry",
                id="workflow-ref",
            ),
            id="ref-row",
        )
        yield Static(id="resolve-message")
        yield VerticalScroll(id="input-fields")
        yield Static(id="form-hint")
        yield Static(id="launch-message")
        yield Footer()

    def on_mount(self) -> None:
        """Focus the reference field, pre-filling and resolving it when the
        caller supplied one (the Registries drill-down's ``n``)."""
        self._update_hint()
        ref_input = self.query_one("#workflow-ref", Input)
        if self._initial_ref:
            ref_input.value = self._initial_ref
            self.action_resolve()
        else:
            ref_input.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Pressing Enter in the workflow-reference field resolves it,
        mirroring the "Resolve" button."""
        if event.input.id == "workflow-ref":
            self.action_resolve()

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
        self._update_hint()

        ref = self.query_one("#workflow-ref", Input).value.strip()
        message = self.query_one("#resolve-message", Static)
        self.query_one("#launch-message", Static).update("")

        if not ref:
            message.update(
                Text.from_markup("[red]Enter a workflow path or registry reference.[/red]")
            )
            await self._rebuild_input_fields({})
            return

        message.update(Text.from_markup("[dim]Resolving…[/dim]"))
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
        self._update_hint()

        # Land the cursor on the first field so the form is immediately
        # usable from the keyboard.
        first_widget = next(iter(self._input_widgets.values()), None)
        if first_widget is not None:
            first_widget.focus()

    async def _rebuild_input_fields(self, inputs: dict[str, InputDef]) -> None:
        """Replace the input-fields container's children with one field block
        per declared input, defaults pre-filled (E12-T3).

        Each block is a ``.field`` container holding a heading, an optional
        wrapping description line, and the widget itself -- rather than the
        single run-on label this used to emit, which put a workflow's entire
        prose description on the same unwrappable line as its field name.
        """
        container = self.query_one("#input-fields", VerticalScroll)
        await container.remove_children()
        self._input_widgets = {}
        self._widget_names = {}
        self._checkbox_touched = set()

        if not inputs:
            if self._resolved is not None:
                # A workflow with no declared inputs is a normal, launchable
                # state, not an error -- say so rather than showing a blank
                # panel that reads as "still loading".
                await container.mount(
                    Static(
                        Text("This workflow declares no inputs — press Launch to run it."),
                        id="empty-inputs",
                    )
                )
            return

        blocks: list[Vertical] = []
        for index, (name, input_def) in enumerate(inputs.items()):
            # An opaque, index-based id -- a declared input name (e.g.
            # "user.email" or "full name") is schema-valid but not a legal
            # Textual widget identifier, which would raise BadIdentifier.
            # The real name is retained via ``_input_widgets``/``_widget_names``.
            widget_id = f"input-{index}"
            widget: Input | Checkbox
            if input_def.type == "boolean":
                has_default = input_def.default is not None
                # The name is carried as the Checkbox's own label: an
                # unlabelled Checkbox renders as a bare, truncated "X…" with
                # nothing to say which field it belongs to.
                widget = Checkbox(
                    Text(name),
                    value=bool(input_def.default) if has_default else False,
                    id=widget_id,
                    classes="field-widget",
                )
                if has_default:
                    # A declared default is already a legitimate value --
                    # only an untouched, default-less checkbox stays unset.
                    self._checkbox_touched.add(name)
            else:
                widget = Input(
                    value=_default_to_raw(input_def),
                    id=widget_id,
                    classes="field-widget",
                )

            self._input_widgets[name] = widget
            self._widget_names[widget] = name

            children: list[Label | Static | Input | Checkbox] = [
                Label(_field_heading(name, input_def), classes="field-heading")
            ]
            if input_def.description:
                children.append(Static(Text(input_def.description), classes="field-description"))
            children.append(widget)
            blocks.append(Vertical(*children, classes="field"))

        await container.mount_all(blocks)

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
        if self._launching:
            # A second ctrl+s while a launch is in flight would start a
            # duplicate (billable) run. This guard used to live on the
            # button's `disabled` state; with no button it belongs here.
            return

        resolved = self._resolved
        if resolved is None:
            self.query_one("#launch-message", Static).update(
                Text.from_markup("[yellow]Resolve a workflow first (ctrl+r).[/yellow]")
            )
            return

        self._launching = True

        message = self.query_one("#launch-message", Static)
        message.update(Text.from_markup("[dim]Launching…[/dim]"))
        raw_values = {name: self._raw_value_for(name) for name in resolved.inputs}

        try:
            launch = await asyncio.to_thread(
                launch_workflow, resolved.path, raw_values, resolved.inputs
            )
        except LaunchError as e:
            logger.warning("Failed to launch workflow %s", resolved.path, exc_info=True)
            message.update(styled("[red]{}[/red]", str(e)))
            self._launching = False
            return

        # Success: hand off to the Runs screen, whose own poll timer will
        # pick up the new (already-discoverable, per D2) run record --
        # this screen never tracks the launched run itself (viewer, not
        # supervisor). Unwinds to Runs rather than popping one level: this
        # screen can also be reached from two levels inside the Registries
        # drill-down, where a single pop would land back on a workflow's
        # inputs with the just-started run nowhere in sight.
        cast("FleetApp", self.app).return_to_runs()
        self.app.notify(f"Launched: {launch.url}", markup=False)
