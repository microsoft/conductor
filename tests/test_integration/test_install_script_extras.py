"""Extras preservation across an install-script upgrade (issue #441).

``uv tool install --force`` replaces the tool's *entire* requirement set, so
an upgrade that names no extras silently uninstalls ``[tui]``/``[aca]``. Both
install scripts therefore read the existing install's ``uv-receipt.toml`` and
rebuild the source as a PEP 508 direct reference carrying those extras.

Both scripts' helpers are executed for real against fixture receipts,
because the failure mode here is a shell-quoting or pattern-matching mistake
that no amount of reading the file catches. ``install.sh`` runs under a POSIX
shell; ``install.ps1``'s helpers are extracted with PowerShell's own parser
and evaluated wherever a native ``pwsh`` exists, and a parity class then
feeds both implementations the same receipt. A handful of static checks cover
the wiring that is awkward to execute (flag names, the up-to-date gate).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conductor.install_hint import installed_extras

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "install.sh"
INSTALL_PS1 = REPO_ROOT / "install.ps1"

# Gate on the platform, not just on `which`: GitHub's windows-latest images
# put Git-Bash's `sh.exe` on PATH, which satisfies `which("sh")` while this
# harness (POSIX `PATH`, a scrubbed env, a Windows path fed to `sh -c`) does
# not work there at all. The repo's precedent for this is a platform skip --
# see test_install_scripts.py's shell-profile check.
SH = None if sys.platform == "win32" else shutil.which("sh")
requires_sh = pytest.mark.skipif(SH is None, reason="POSIX shell required")

# Only a *native* PowerShell is usable. Under WSL, `powershell.exe` resolves
# through Windows PATH interop but cannot read Linux paths, so it would run
# the harness against a file it can't see and silently report no extras.
PWSH = shutil.which("pwsh")
if sys.platform == "win32":
    PWSH = PWSH or shutil.which("powershell.exe")
requires_pwsh = pytest.mark.skipif(PWSH is None, reason="native PowerShell required")

RECEIPT_BARE = '[tool]\nrequirements = [{ name = "conductor-cli" }]\n'

RECEIPT_WITH_OTHER_PKG = (
    "[tool]\nrequirements = [\n"
    '  { name = "other-pkg", extras = ["zzz"] },\n'
    '  { name = "conductor-cli", extras = ["tui"] },\n'
    "]\n"
)

RECEIPT_WRAPPED = """\
[tool]
requirements = [
    { name = "conductor-cli", extras = [
        "tui",
        "aca",
    ], git = "https://github.com/microsoft/conductor.git?rev=v0.1.30" },
]
"""

RECEIPT_NONCANONICAL = '[tool]\nrequirements = [{ name = "Conductor_CLI", extras = ["tui"] }]\n'

RECEIPT_TUI = """\
[tool]
requirements = [{ name = "conductor-cli", extras = ["tui"], \
git = "https://github.com/microsoft/conductor.git?rev=v0.1.30" }]
entrypoints = [
    { name = "conductor", install-path = "/home/u/.local/bin/conductor", from = "conductor-cli" },
]
"""


@pytest.fixture()
def sh_helpers(tmp_path: Path) -> Path:
    """``install.sh`` with its ``main`` invocation stripped, so it can be sourced.

    Sourcing the real file (rather than a copy of the helper bodies) is the
    point: a copy would drift from the script it claims to test.
    """
    lines = INSTALL_SH.read_text(encoding="utf-8").splitlines()
    assert lines[-1].strip() == "main", (
        "install.sh no longer ends with a bare `main` invocation; this fixture "
        "strips that line so the script can be sourced without installing anything."
    )
    stub = tmp_path / "install_sourceable.sh"
    stub.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    return stub


def run_sh(stub: Path, snippet: str, uv_tool_dir: Path | None = None) -> str:
    """Source the stripped install script and evaluate *snippet*.

    A fake ``uv`` is put on PATH so ``uv tool dir`` resolves to the fixture
    directory without needing a real install.
    """
    assert SH is not None
    env = {"PATH": str(stub.parent / "bin") + ":/usr/bin:/bin", "HOME": str(stub.parent)}
    fake_bin = stub.parent / "bin"
    fake_bin.mkdir(exist_ok=True)
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then\n'
        f'  printf "%s\\n" "{uv_tool_dir or ""}"\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    proc = subprocess.run(
        [SH, "-c", f". {stub}\n{snippet}"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, f"snippet failed:\n{proc.stdout}\n{proc.stderr}"
    return proc.stdout.strip()


@requires_sh
class TestInstallShExtras:
    """The real ``install.sh`` helpers, executed."""

    def test_reads_the_extras_recorded_in_the_receipt(self, sh_helpers: Path) -> None:
        tools = sh_helpers.parent / "tools"
        (tools / "conductor-cli").mkdir(parents=True)
        (tools / "conductor-cli" / "uv-receipt.toml").write_text(RECEIPT_TUI, encoding="utf-8")

        assert run_sh(sh_helpers, "receipt_extras", tools) == "tui"

    def test_reads_several_extras(self, sh_helpers: Path) -> None:
        tools = sh_helpers.parent / "tools"
        (tools / "conductor-cli").mkdir(parents=True)
        (tools / "conductor-cli" / "uv-receipt.toml").write_text(
            '[tool]\nrequirements = [{ name = "conductor-cli", extras = ["tui", "aca"] }]\n',
            encoding="utf-8",
        )

        assert run_sh(sh_helpers, "receipt_extras", tools) == "tui,aca"

    def test_ignores_extras_belonging_to_other_requirements(self, sh_helpers: Path) -> None:
        """`uv tool install --with X` adds requirements that are not this
        distribution; folding their extras into the spec would install
        something the user never asked for."""
        tools = sh_helpers.parent / "tools"
        (tools / "conductor-cli").mkdir(parents=True)
        (tools / "conductor-cli" / "uv-receipt.toml").write_text(
            "[tool]\nrequirements = [\n"
            '  { name = "other-pkg", extras = ["zzz"] },\n'
            '  { name = "conductor-cli", extras = ["tui"] },\n'
            "]\n",
            encoding="utf-8",
        )

        assert run_sh(sh_helpers, "receipt_extras", tools) == "tui"

    def test_a_bare_install_reports_no_extras(self, sh_helpers: Path) -> None:
        tools = sh_helpers.parent / "tools"
        (tools / "conductor-cli").mkdir(parents=True)
        (tools / "conductor-cli" / "uv-receipt.toml").write_text(
            '[tool]\nrequirements = [{ name = "conductor-cli" }]\n', encoding="utf-8"
        )

        assert run_sh(sh_helpers, "receipt_extras", tools) == ""

    def test_a_missing_receipt_is_not_an_error(self, sh_helpers: Path) -> None:
        """A first-time install has no receipt. `set -e` is active, so a
        non-zero exit here would abort the whole installer."""
        assert run_sh(sh_helpers, "receipt_extras", sh_helpers.parent / "nope") == ""

    def test_an_unusable_uv_tool_dir_is_not_an_error(self, sh_helpers: Path) -> None:
        """`uv tool dir` can print nothing (or fail) on an older uv; that must
        degrade to "no extras", not abort the installer under `set -e`."""
        assert run_sh(sh_helpers, "receipt_extras", None) == ""

    def test_merge_sorts_and_deduplicates(self, sh_helpers: Path) -> None:
        assert run_sh(sh_helpers, 'merge_extras "tui,aca" "tui"') == "aca,tui"

    def test_merge_of_nothing_is_empty(self, sh_helpers: Path) -> None:
        assert run_sh(sh_helpers, 'merge_extras "" ""') == ""

    def test_merge_tolerates_whitespace_in_a_user_supplied_list(self, sh_helpers: Path) -> None:
        assert run_sh(sh_helpers, 'merge_extras "" " tui , aca "') == "aca,tui"

    def test_extras_become_a_pep508_direct_reference(self, sh_helpers: Path) -> None:
        out = run_sh(
            sh_helpers,
            'apply_extras "git+https://github.com/microsoft/conductor.git@v1.2.3" "aca,tui"',
        )

        assert out == (
            "conductor-cli[aca,tui] @ git+https://github.com/microsoft/conductor.git@v1.2.3"
        )

    def test_no_extras_leaves_the_source_untouched(self, sh_helpers: Path) -> None:
        """A bare install must keep passing the plain git source, so a
        first-time install is byte-for-byte what it always was."""
        out = run_sh(sh_helpers, 'apply_extras "git+https://example.com/x.git@v1" ""')

        assert out == "git+https://example.com/x.git@v1"

    def test_a_wrapped_requirements_array_is_still_parsed(self, sh_helpers: Path) -> None:
        """uv may wrap the requirements array across lines, which is the whole
        reason the parser flattens newlines first. Without a wrapped fixture,
        deleting that flatten breaks nothing and the extras would be silently
        dropped on the next upgrade."""
        tools = sh_helpers.parent / "tools"
        (tools / "conductor-cli").mkdir(parents=True)
        (tools / "conductor-cli" / "uv-receipt.toml").write_text(RECEIPT_WRAPPED, encoding="utf-8")

        assert run_sh(sh_helpers, 'merge_extras "$(receipt_extras)" ""', tools) == "aca,tui"

    def test_a_similarly_named_requirement_is_not_mistaken_for_this_one(
        self, sh_helpers: Path
    ) -> None:
        """`conductor-cli-plugin` contains `conductor-cli`. Matching the bare
        substring would fold a `--with` package's extras into our spec and
        install something the user never asked for."""
        tools = sh_helpers.parent / "tools"
        (tools / "conductor-cli").mkdir(parents=True)
        (tools / "conductor-cli" / "uv-receipt.toml").write_text(
            "[tool]\nrequirements = [\n"
            '  { name = "conductor-cli-plugin", extras = ["evil"] },\n'
            '  { name = "conductor-cli", extras = ["tui"] },\n'
            "]\n",
            encoding="utf-8",
        )

        assert run_sh(sh_helpers, "receipt_extras", tools) == "tui"

    def test_merge_lower_cases_so_the_comparison_converges(self, sh_helpers: Path) -> None:
        """PowerShell's `Sort-Object -Unique` is case-insensitive. If `sort -u`
        here is not, `--extras TUI` against a recorded `tui` yields a resolved
        set that never equals the installed one, so every run reinstalls."""
        assert run_sh(sh_helpers, 'merge_extras "tui" "TUI"', sh_helpers.parent) == "tui"

    def test_an_unknown_extra_is_refused(self, sh_helpers: Path) -> None:
        """uv treats an unknown extra as a warning and still exits 0, so a typo
        would otherwise install nothing and report success."""
        assert run_sh(sh_helpers, "validate_extras tui,aca", sh_helpers.parent) == ""
        with pytest.raises(AssertionError):
            run_sh(sh_helpers, "validate_extras tuii", sh_helpers.parent)

    def test_an_unreadable_receipt_is_reported_not_treated_as_bare(self, sh_helpers: Path) -> None:
        """The destructive case. An unreadable receipt reported as "no extras"
        rebuilds the tool without them, which is the #441 data loss this whole
        change exists to prevent — so the helper has to fail rather than
        return empty."""
        tools = sh_helpers.parent / "tools"
        (tools / "conductor-cli").mkdir(parents=True)
        receipt = tools / "conductor-cli" / "uv-receipt.toml"
        receipt.write_text(RECEIPT_TUI, encoding="utf-8")
        receipt.chmod(0o000)
        try:
            with pytest.raises(AssertionError):
                run_sh(sh_helpers, "receipt_extras", tools)
        finally:
            receipt.chmod(0o644)

    def test_a_receipt_without_our_requirement_is_reported_unreadable(
        self, sh_helpers: Path
    ) -> None:
        """Schema drift must not read as "no extras" either, for the same
        reason — `read_receipt` draws the same line on the Python side."""
        tools = sh_helpers.parent / "tools"
        (tools / "conductor-cli").mkdir(parents=True)
        (tools / "conductor-cli" / "uv-receipt.toml").write_text(
            '[tool]\nrequirements = [{ name = "something-else" }]\n', encoding="utf-8"
        )

        with pytest.raises(AssertionError):
            run_sh(sh_helpers, "receipt_extras", tools)

    def test_a_non_canonical_distribution_name_still_matches(self, sh_helpers: Path) -> None:
        """PEP 503 makes `Conductor_CLI` the same project. The Python reader
        normalises; a case-sensitive shell match would find nothing here and
        drop the extras on upgrade."""
        tools = sh_helpers.parent / "tools"
        (tools / "conductor-cli").mkdir(parents=True)
        (tools / "conductor-cli" / "uv-receipt.toml").write_text(
            RECEIPT_NONCANONICAL, encoding="utf-8"
        )

        assert run_sh(sh_helpers, "receipt_extras", tools) == "tui"

    def test_an_extra_is_accepted_regardless_of_case(self, sh_helpers: Path) -> None:
        """PowerShell's `-notcontains` is case-insensitive; rejecting `TUI`
        only on POSIX would be a new cross-platform divergence."""
        assert run_sh(sh_helpers, "validate_extras TUI", sh_helpers.parent) == ""

    @pytest.mark.parametrize(
        "receipt",
        [RECEIPT_TUI, RECEIPT_WRAPPED, RECEIPT_BARE, RECEIPT_WITH_OTHER_PKG, RECEIPT_NONCANONICAL],
        ids=["one-extra", "wrapped", "bare", "other-pkg", "non-canonical-name"],
    )
    def test_the_shell_and_python_parsers_read_the_same_receipt(
        self, sh_helpers: Path, receipt: str
    ) -> None:
        """`install.sh` parses the receipt with sed and `conductor.install_hint`
        parses it with tomllib. They cannot share an implementation -- the
        installer runs before any conductor exists -- so a shared oracle is the
        only thing keeping them honest. This is the parity that matters:
        sh-vs-ps1 can only catch divergence, never a bug both inherited."""
        tools = sh_helpers.parent / "tools"
        (tools / "conductor-cli").mkdir(parents=True)
        (tools / "conductor-cli" / "uv-receipt.toml").write_text(receipt, encoding="utf-8")

        from_sh = run_sh(sh_helpers, 'merge_extras "$(receipt_extras)" ""', tools)
        from_python = ",".join(sorted(installed_extras(tools / "conductor-cli")))

        assert from_sh == from_python


class TestInstallScriptsStayInSync:
    """Both installers have to preserve extras, or upgrading on one platform
    silently drops what the other kept.

    These are the checks that are awkward to execute (they live in the
    scripts' argument-parsing preamble rather than in a helper). Each asserts
    on the *wiring*, not on a string that also appears in the comment header
    -- an earlier version of this class passed against scripts whose parsing
    had been deleted, because the needle was still in the docs block.
    """

    @pytest.mark.parametrize("script", [INSTALL_SH, INSTALL_PS1], ids=["sh", "ps1"])
    def test_builds_a_direct_reference_carrying_the_extras(self, script: Path) -> None:
        assert "conductor-cli[" in script.read_text(encoding="utf-8"), (
            f"{script.name} does not build a `conductor-cli[<extras>] @ <source>` "
            "direct reference, which is the only shape that carries extras through "
            "`uv tool install`."
        )

    @pytest.mark.parametrize(
        ("script", "needle"),
        [
            (INSTALL_SH, 'EXTRAS="${CONDUCTOR_INSTALL_EXTRAS:-}"'),
            (INSTALL_PS1, "$Extras   = $env:CONDUCTOR_INSTALL_EXTRAS"),
        ],
        ids=["sh", "ps1"],
    )
    def test_accepts_an_explicit_extras_request(self, script: Path, needle: str) -> None:
        assert needle in script.read_text(encoding="utf-8"), (
            f"{script.name} does not read CONDUCTOR_INSTALL_EXTRAS, so there is no way "
            "to add an extra through the documented `curl | sh` / `irm | iex` install."
        )

    @pytest.mark.parametrize(
        ("script", "needle"),
        [
            (INSTALL_SH, 'NO_PRESERVE_EXTRAS="${CONDUCTOR_INSTALL_NO_PRESERVE_EXTRAS:-0}"'),
            (INSTALL_PS1, "$NoPreserveExtras = $true"),
        ],
        ids=["sh", "ps1"],
    )
    def test_offers_a_way_back_to_a_bare_install(self, script: Path, needle: str) -> None:
        assert needle in script.read_text(encoding="utf-8"), (
            f"{script.name} does not wire up the opt-out, so a user who installed "
            "[aca] once could never get back to a bare install through the installer."
        )

    @pytest.mark.parametrize(
        ("script", "needle"),
        [
            (INSTALL_SH, 'apply_extras "$install_source" "$resolved_extras"'),
            (INSTALL_PS1, "(Format-ProcessArgument $InstallSource)"),
        ],
        ids=["sh", "ps1"],
    )
    def test_the_spec_reaches_the_installer_intact(self, script: Path, needle: str) -> None:
        """PowerShell's `-ArgumentList` does not quote its elements, so the
        spec has to be routed through `Format-ProcessArgument` or uv receives
        `conductor-cli[tui]`, `@` and the URL as three arguments."""
        assert needle in script.read_text(encoding="utf-8"), (
            f"{script.name} no longer passes the install source through the helper that "
            "keeps it a single argument; the extras would be silently dropped."
        )

    @pytest.mark.parametrize(
        ("script", "needle"),
        [
            (INSTALL_SH, '[ "$resolved_extras" = "$receipt_now" ]'),
            (INSTALL_PS1, "$resolvedExtras -eq $receiptNow"),
        ],
        ids=["sh", "ps1"],
    )
    def test_the_up_to_date_shortcut_compares_against_what_is_installed(
        self, script: Path, needle: str
    ) -> None:
        """The gate must compare against the extras *on disk*, not against the
        set this run decided to carry -- `--no-preserve-extras` zeroes the
        latter, which made both sides equal and turned the flag into a no-op
        that reported success."""
        assert needle in script.read_text(encoding="utf-8"), (
            f"{script.name}'s already-up-to-date early return does not compare against "
            "the installed extras, so --extras/--no-preserve-extras on a current "
            "install is silently a no-op."
        )


PS_HELPERS = (
    "Get-ReceiptExtras",
    "Merge-Extras",
    "Add-ExtrasToSource",
    "Test-ExtrasKnown",
    "Format-ProcessArgument",
)

# Extract the three helpers from install.ps1 via PowerShell's own parser and
# evaluate just those, so the real script text is exercised without running
# an install. `Get-ConductorToolDir` is then overridden to point at the
# fixture receipt. A brace-counting extraction would not work here: the
# helpers contain `}` inside string literals.
PS_HARNESS = """
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    '{script}', [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) {{ Write-Error ($errors | Out-String); exit 1 }}
$want = @({wanted})
$fns = $ast.FindAll({{ param($n)
    $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $want -contains $n.Name }}, $true)
if ($fns.Count -ne {count}) {{
    Write-Error "expected {count} helpers, found $($fns.Count)"; exit 1
}}
foreach ($f in $fns) {{ Invoke-Expression $f.Extent.Text }}
function Get-ConductorToolDir {{ return '{tool_dir}' }}
{snippet}
"""


def run_pwsh(snippet: str, tool_dir: Path | str) -> str:
    """Evaluate *snippet* with install.ps1's extras helpers in scope."""
    assert PWSH is not None
    script = PS_HARNESS.format(
        script=str(INSTALL_PS1).replace("'", "''"),
        wanted=",".join(f"'{name}'" for name in PS_HELPERS),
        count=len(PS_HELPERS),
        tool_dir=str(tool_dir).replace("'", "''"),
        snippet=snippet,
    )
    proc = subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"snippet failed:\n{proc.stdout}\n{proc.stderr}"
    return proc.stdout.strip()


@requires_pwsh
class TestInstallPs1Extras:
    """The real ``install.ps1`` helpers, executed.

    Runs on the Windows CI job (and anywhere ``pwsh`` is on PATH). Without
    this, the PowerShell half of the feature is only ever grepped — and the
    two installers producing *different* extras is exactly the failure this
    change exists to prevent.
    """

    @pytest.fixture()
    def tool_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "conductor-cli"
        d.mkdir()
        return d

    def test_reads_the_extras_recorded_in_the_receipt(self, tool_dir: Path) -> None:
        (tool_dir / "uv-receipt.toml").write_text(RECEIPT_TUI, encoding="utf-8")

        assert run_pwsh("Write-Output (Get-ReceiptExtras)", tool_dir) == "tui"

    def test_ignores_extras_belonging_to_other_requirements(self, tool_dir: Path) -> None:
        (tool_dir / "uv-receipt.toml").write_text(
            "[tool]\nrequirements = [\n"
            '  { name = "other-pkg", extras = ["zzz"] },\n'
            '  { name = "conductor-cli", extras = ["tui"] },\n'
            "]\n",
            encoding="utf-8",
        )

        assert run_pwsh("Write-Output (Get-ReceiptExtras)", tool_dir) == "tui"

    def test_a_bare_install_reports_no_extras(self, tool_dir: Path) -> None:
        (tool_dir / "uv-receipt.toml").write_text(
            '[tool]\nrequirements = [{ name = "conductor-cli" }]\n', encoding="utf-8"
        )

        assert run_pwsh('Write-Output ("[" + (Get-ReceiptExtras) + "]")', tool_dir) == "[]"

    def test_a_missing_receipt_is_not_an_error(self, tool_dir: Path) -> None:
        assert run_pwsh('Write-Output ("[" + (Get-ReceiptExtras) + "]")', tool_dir) == "[]"

    def test_quotes_an_argument_containing_whitespace(self, tool_dir: Path) -> None:
        """`Start-Process -ArgumentList` joins elements with spaces and does
        not quote them, so an unquoted `conductor-cli[tui] @ <src>` reached uv
        as three separate arguments and the extras were silently dropped."""
        out = run_pwsh("Write-Output (Format-ProcessArgument 'a b c')", tool_dir)

        assert out == '"a b c"'

    def test_leaves_a_whitespace_free_argument_alone(self, tool_dir: Path) -> None:
        out = run_pwsh("Write-Output (Format-ProcessArgument 'git+https://x@v1')", tool_dir)

        assert out == "git+https://x@v1"

    def test_an_unreadable_receipt_is_reported_not_treated_as_bare(self, tool_dir: Path) -> None:
        (tool_dir / "uv-receipt.toml").write_text(
            '[tool]\nrequirements = [{ name = "something-else" }]\n', encoding="utf-8"
        )

        assert run_pwsh("Write-Output ($null -eq (Get-ReceiptExtras))", tool_dir) == "True"

    def test_a_bare_install_is_empty_not_null(self, tool_dir: Path) -> None:
        """Empty means "understood, nothing recorded"; null means "could not
        tell". Collapsing them is what made an unreadable receipt destructive."""
        (tool_dir / "uv-receipt.toml").write_text(
            '[tool]\nrequirements = [{ name = "conductor-cli" }]\n', encoding="utf-8"
        )

        assert run_pwsh("Write-Output ('' -eq (Get-ReceiptExtras))", tool_dir) == "True"

    def test_merge_sorts_and_deduplicates(self, tool_dir: Path) -> None:
        out = run_pwsh("Write-Output (Merge-Extras 'tui,aca' ' tui ')", tool_dir)

        assert out == "aca,tui"

    def test_merge_of_nothing_is_empty(self, tool_dir: Path) -> None:
        assert run_pwsh("Write-Output ('[' + (Merge-Extras '' '') + ']')", tool_dir) == "[]"

    def test_extras_become_a_pep508_direct_reference(self, tool_dir: Path) -> None:
        out = run_pwsh(
            "Write-Output (Add-ExtrasToSource "
            "'git+https://github.com/microsoft/conductor.git@v1.2.3' 'aca,tui')",
            tool_dir,
        )

        assert out == (
            "conductor-cli[aca,tui] @ git+https://github.com/microsoft/conductor.git@v1.2.3"
        )

    def test_no_extras_leaves_the_source_untouched(self, tool_dir: Path) -> None:
        out = run_pwsh(
            "Write-Output (Add-ExtrasToSource 'git+https://example.com/x.git@v1' '')", tool_dir
        )

        assert out == "git+https://example.com/x.git@v1"


@requires_sh
@requires_pwsh
class TestBothInstallersAgree:
    """The two implementations are separate code in separate languages; the
    only thing keeping them honest is running both against the same input."""

    @pytest.mark.parametrize(
        "receipt",
        [
            RECEIPT_TUI,
            '[tool]\nrequirements = [{ name = "conductor-cli", extras = ["tui", "aca"] }]\n',
            '[tool]\nrequirements = [{ name = "conductor-cli" }]\n',
        ],
        ids=["one-extra", "two-extras", "no-extras"],
    )
    def test_the_same_receipt_yields_the_same_extras(
        self, sh_helpers: Path, tmp_path: Path, receipt: str
    ) -> None:
        sh_tools = sh_helpers.parent / "tools"
        (sh_tools / "conductor-cli").mkdir(parents=True)
        (sh_tools / "conductor-cli" / "uv-receipt.toml").write_text(receipt, encoding="utf-8")

        ps_dir = tmp_path / "ps" / "conductor-cli"
        ps_dir.mkdir(parents=True)
        (ps_dir / "uv-receipt.toml").write_text(receipt, encoding="utf-8")

        from_sh = run_sh(sh_helpers, 'merge_extras "$(receipt_extras)" ""', sh_tools)
        from_ps = run_pwsh("Write-Output (Merge-Extras (Get-ReceiptExtras) '')", ps_dir)

        assert from_sh == from_ps


@requires_sh
class TestExtrasDecisionEndToEnd:
    """Drives the real ``install.sh`` and asserts on the requirement it hands
    to ``uv tool install``.

    The three helpers can each be correct while the block that *uses* them is
    not — which is exactly what happened: ``--no-preserve-extras`` compared the
    resolved set against a variable the flag itself had zeroed, so the
    up-to-date shortcut fired and the opt-out did nothing while reporting
    success. Only a test that runs the script can see that, and only one that
    inspects the real argv can prove the extras survived into the spec.

    ``uv`` is faked so the run is hermetic and instant: a real one would fail
    to resolve the throwaway source and burn the script's retry backoff.
    """

    def run_installer(
        self,
        tmp_path: Path,
        receipt: str | None,
        *args: str,
        installed_version: str | None = None,
    ) -> tuple[str, str]:
        """Run the installer; return (output, the spec passed to ``uv``).

        With *installed_version* set, ``curl`` and ``conductor`` are faked too
        so the script takes its **release** path — the only one where the
        already-up-to-date shortcut is reachable. That shortcut is gated on
        ``[ -z "$SOURCE" ]``, so a ``--source`` run can never exercise it, and
        the regression it guards would go unnoticed.
        """
        tools = tmp_path / "tools"
        tools.mkdir(parents=True)
        if receipt is not None:
            (tools / "conductor-cli").mkdir(parents=True)
            (tools / "conductor-cli" / "uv-receipt.toml").write_text(receipt, encoding="utf-8")

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        argv_log = tmp_path / "uv-argv.txt"
        fake_uv = bin_dir / "uv"
        fake_uv.write_text(
            "#!/bin/sh\n"
            f'if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then printf "%s\\n" "{tools}"; exit 0; fi\n'
            f'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then\n'
            f'  printf "%s\\n" "$4" > "{argv_log}"\n'
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_uv.chmod(0o755)

        script_args = ["--force", "--skip-path-update", *args]
        if installed_version is None:
            script_args = ["--source", str(tmp_path / "src"), *script_args]
        else:
            fake_curl = bin_dir / "curl"
            # The release path fetches the latest tag, then downloads
            # constraints; failing the latter makes the script warn and carry
            # on, which keeps this hermetic.
            fake_curl.write_text(
                "#!/bin/sh\n"
                'for a in "$@"; do [ "$a" = "-o" ] && exit 1; done\n'
                f'printf \'{{"tag_name": "v{installed_version}"}}\'\n',
                encoding="utf-8",
            )
            fake_curl.chmod(0o755)
            fake_conductor = bin_dir / "conductor"
            fake_conductor.write_text(
                f"#!/bin/sh\nprintf 'Conductor v{installed_version}\\n'\n", encoding="utf-8"
            )
            fake_conductor.chmod(0o755)

        proc = subprocess.run(
            [str(INSTALL_SH), *script_args],
            capture_output=True,
            text=True,
            timeout=60,
            env={
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "HOME": str(tmp_path),
                "UV_TOOL_DIR": str(tools),
            },
        )
        spec = argv_log.read_text(encoding="utf-8").strip() if argv_log.exists() else ""
        return proc.stdout + proc.stderr, spec

    def test_an_upgrade_preserves_the_recorded_extras(self, tmp_path: Path) -> None:
        out, spec = self.run_installer(tmp_path, RECEIPT_TUI)

        assert "Including extras: tui" in out
        assert spec.startswith("conductor-cli[tui] @ ")

    def test_requested_extras_are_added_to_the_recorded_ones(self, tmp_path: Path) -> None:
        out, spec = self.run_installer(tmp_path, RECEIPT_TUI, "--extras", "aca")

        assert "Including extras: aca,tui" in out
        assert spec.startswith("conductor-cli[aca,tui] @ ")

    def test_no_preserve_extras_actually_drops_them(self, tmp_path: Path) -> None:
        """The regression: with the opt-out set, the script must not report
        "already up to date" and leave the extras installed."""
        out, spec = self.run_installer(tmp_path, RECEIPT_TUI, "--no-preserve-extras")

        assert "Dropping extras: tui" in out
        assert "Including extras" not in out
        assert "already installed and up to date" not in out
        assert "conductor-cli[" not in spec

    def test_an_unknown_extra_aborts_before_installing(self, tmp_path: Path) -> None:
        out, spec = self.run_installer(tmp_path, RECEIPT_TUI, "--extras", "tuii")

        assert "unknown extra 'tuii'" in out
        assert spec == ""

    def test_a_first_time_install_needs_no_receipt(self, tmp_path: Path) -> None:
        out, spec = self.run_installer(tmp_path, None)

        assert "Including extras" not in out
        assert "Dropping extras" not in out
        assert "conductor-cli[" not in spec

    def test_an_up_to_date_install_with_matching_extras_is_a_no_op(self, tmp_path: Path) -> None:
        out, spec = self.run_installer(tmp_path, RECEIPT_TUI, installed_version="1.2.3")

        assert "already installed and up to date" in out
        assert spec == ""

    def test_no_preserve_extras_defeats_the_up_to_date_shortcut(self, tmp_path: Path) -> None:
        """The regression, on the path where it actually bit: the gate used to
        compare the resolved set against a variable this flag had just zeroed,
        so both sides matched, the shortcut fired, and the extras stayed
        installed under a green "already up to date"."""
        out, spec = self.run_installer(
            tmp_path, RECEIPT_TUI, "--no-preserve-extras", installed_version="1.2.3"
        )

        assert "already installed and up to date" not in out
        assert "Dropping extras: tui" in out
        assert "conductor-cli[" not in spec

    def test_a_newly_requested_extra_defeats_the_up_to_date_shortcut(self, tmp_path: Path) -> None:
        out, spec = self.run_installer(
            tmp_path, RECEIPT_TUI, "--extras", "aca", installed_version="1.2.3"
        )

        assert "already installed and up to date" not in out
        assert spec.startswith("conductor-cli[aca,tui] @ ")
