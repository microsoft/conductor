"""Conductor - A CLI tool for defining and running multi-agent workflows.

Conductor enables orchestration of multi-agent workflows defined in YAML.
It supports conditional routing, loop-back patterns, human-in-the-loop gates,
and integrates with the GitHub Copilot SDK.

Example:
    Run a workflow from the command line::

        $ conductor run workflow.yaml --input question="What is Python?"

    Or use the library programmatically::

        from conductor.config.loader import load_config
        from conductor.engine.workflow import WorkflowEngine
        from conductor.providers.factory import create_provider

        config = load_config("workflow.yaml")
        provider = await create_provider("copilot")
        engine = WorkflowEngine(config, provider)
        result = await engine.run({"question": "What is Python?"})

Modules:
    config: Configuration loading, schema validation, and environment variable resolution.
    engine: Workflow execution engine, context management, routing, and limits.
    executor: Agent execution, template rendering, and output validation.
    providers: SDK provider abstraction and implementations.
    gates: Human-in-the-loop gate handling.
    cli: Command-line interface commands.
    exceptions: Custom exception hierarchy.
"""

# Enable Python's ``faulthandler`` as early as possible — before any other
# conductor import — so a native crash (segfault, abort, fatal Python error)
# anywhere in the process dumps a traceback to the original stderr stream.
# This is the earliest module imported by both ``python -m conductor`` (via
# ``conductor.__main__``) and the installed ``conductor`` console script
# (``conductor.cli.app:app`` in ``pyproject.toml``), so this single hook
# covers every launch path.
#
# In ``--web-bg`` mode the parent passes a redirected stderr file handle to
# ``subprocess.Popen`` (see ``conductor.cli.bg_runner``); when Python
# initialises in the child process, ``sys.__stderr__`` already points at
# that captured log file, so the crash trace survives the parent's exit.
# See issue #116 for context.
import sys as _sys

try:
    import faulthandler as _faulthandler

    if _sys.__stderr__ is not None:
        _faulthandler.enable(file=_sys.__stderr__, all_threads=True)
except Exception as _faulthandler_exc:  # noqa: BLE001 - diagnostics must never break startup
    # The very diagnostic this PR adds is dead if this silently fails;
    # print a warning so the user knows the crash trace will not survive
    # a native abort. Use ``sys.stderr`` (not ``sys.__stderr__``) because
    # the original stderr may itself be the source of the failure.
    try:  # noqa: SIM105 - cannot use contextlib.suppress here without an import
        print(
            f"conductor: WARNING: faulthandler failed to initialize: {_faulthandler_exc}",
            file=_sys.stderr,
        )
    except Exception:  # noqa: BLE001 - really, truly must not break startup
        pass

from importlib.metadata import version as _pkg_version

__version__ = _pkg_version("conductor-cli")

__all__ = ["__version__"]
