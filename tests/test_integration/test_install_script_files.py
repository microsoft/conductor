"""Static checks on ``install.ps1`` and ``install.sh`` that don't execute them.

These tests are fast (microseconds) and run as part of the default ``make
test`` suite — unlike :mod:`tests.test_integration.test_install_scripts`,
which actually drives the scripts end-to-end behind the
``install_scripts`` pytest marker.

The most important check here is that ``install.ps1`` does **not** start
with a UTF-8 BOM (``EF BB BF``).  When the script is delivered via the
canonical README install command::

    irm https://aka.ms/conductor/install.ps1 | iex

``Invoke-RestMethod`` returns the body as a single ``System.String`` with
the BOM surviving as ``U+FEFF`` at index 0.  Piping that string to
``Invoke-Expression`` makes PowerShell's parser fail on the
``[CmdletBinding()]`` attribute that follows the comment header — so
nothing installs.  The ``-File`` invocation used by
:mod:`tests.test_integration.test_install_scripts` does *not* exhibit the
bug because PowerShell's file loader handles the BOM differently from the
in-memory ``iex`` parser, which is why the bug slipped through CI before
issue #175.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_PS1 = REPO_ROOT / "install.ps1"
INSTALL_SH = REPO_ROOT / "install.sh"

UTF8_BOM = b"\xef\xbb\xbf"


def test_install_ps1_has_no_utf8_bom() -> None:
    """``install.ps1`` must be UTF-8 without BOM.

    A leading BOM survives ``Invoke-RestMethod`` as ``U+FEFF`` and breaks
    the documented ``irm <url> | iex`` install path (see issue #175).
    """
    data = INSTALL_PS1.read_bytes()
    assert not data.startswith(UTF8_BOM), (
        "install.ps1 must not start with a UTF-8 BOM (EF BB BF) — it breaks "
        "`irm <url> | iex` (the documented install command) and "
        "`conductor update --apply`. Re-save the file as 'UTF-8 without BOM'."
    )


def test_install_sh_has_no_utf8_bom() -> None:
    """``install.sh`` must be UTF-8 without BOM (POSIX shells reject one)."""
    data = INSTALL_SH.read_bytes()
    assert not data.startswith(UTF8_BOM), (
        "install.sh must not start with a UTF-8 BOM (EF BB BF). POSIX "
        "shells treat it as a literal command and abort."
    )


def test_install_sh_has_shebang() -> None:
    """``install.sh`` must start with a ``#!`` shebang."""
    data = INSTALL_SH.read_bytes()
    assert data.startswith(b"#!"), (
        "install.sh must start with a '#!' shebang as its very first bytes."
    )


def test_install_ps1_uses_lf_line_endings() -> None:
    """``install.ps1`` should use LF line endings.

    PowerShell tolerates CRLF, but pinning to LF keeps the file consistent
    across platforms and avoids spurious diffs from Windows editors that
    auto-convert.  This is also enforced by ``.gitattributes``.
    """
    data = INSTALL_PS1.read_bytes()
    assert b"\r\n" not in data, (
        "install.ps1 contains CRLF line endings; should be LF-only (see .gitattributes)."
    )
