"""Static checks on ``install.ps1`` and ``install.sh`` that don't execute them.

These tests are fast (microseconds) and run as part of the default ``make
test`` suite -- unlike :mod:`tests.test_integration.test_install_scripts`,
which actually drives the scripts end-to-end behind the
``install_scripts`` pytest marker.

The most important check here is that ``install.ps1`` does **not** start
with a UTF-8 BOM (``EF BB BF``).  When the script is delivered via the
canonical README install command::

    irm https://aka.ms/conductor/install.ps1 | iex

``Invoke-RestMethod`` returns the body as a single ``System.String`` with
the BOM surviving as ``U+FEFF`` at index 0.  Piping that string to
``Invoke-Expression`` makes PowerShell's parser fail on the
``[CmdletBinding()]`` attribute that follows the comment header -- so
nothing installs.  ``conductor update --apply`` builds the same
``irm | iex`` command in :mod:`conductor.cli.update` so it is broken by
the same regression; this test protects both paths.

The ``-File`` invocation used by
:mod:`tests.test_integration.test_install_scripts` does *not* exhibit
the bug because PowerShell's file loader uses the BOM as an encoding
sniff and *strips* it from the resulting string before parsing.
``Invoke-RestMethod`` decodes the HTTP body without that special
handling, so the U+FEFF is preserved as a literal character that
``iex`` then sees at offset 0.  This is why the bug slipped through CI
before issue #175.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_PS1 = REPO_ROOT / "install.ps1"
INSTALL_SH = REPO_ROOT / "install.sh"

UTF8_BOM = b"\xef\xbb\xbf"
UTF16_LE_BOM = b"\xff\xfe"
UTF16_BE_BOM = b"\xfe\xff"


def test_install_ps1_has_no_utf8_bom() -> None:
    """``install.ps1`` must be UTF-8 without BOM.

    A leading BOM survives ``Invoke-RestMethod`` as ``U+FEFF`` and breaks
    both ``irm <url> | iex`` (the documented install command) and
    ``conductor update --apply`` (which builds the same command). See
    issue #175.
    """
    data = INSTALL_PS1.read_bytes()
    assert not data.startswith(UTF8_BOM), (
        "install.ps1 must not start with a UTF-8 BOM (EF BB BF) -- it breaks "
        "`irm <url> | iex` (the documented install command) and "
        "`conductor update --apply`. Re-save the file as 'UTF-8 without BOM'."
    )


def test_install_ps1_has_no_utf16_bom() -> None:
    """``install.ps1`` must not be saved as UTF-16.

    Some Windows editors offer "UTF-16 with BOM" as the default save
    encoding. A UTF-16 BOM (``FF FE`` for LE or ``FE FF`` for BE) would
    break ``irm | iex`` even more catastrophically than UTF-8 BOM, since
    every other byte would be NUL.
    """
    data = INSTALL_PS1.read_bytes()
    assert not data.startswith(UTF16_LE_BOM) and not data.startswith(UTF16_BE_BOM), (
        "install.ps1 must not be saved as UTF-16. Re-save as 'UTF-8 without BOM'."
    )


def test_install_ps1_is_pure_ascii() -> None:
    """``install.ps1`` must contain only ASCII bytes.

    Windows PowerShell 5.1 (the ``powershell.exe`` shipped with Windows
    10/11) does *not* default to UTF-8 for files without a BOM -- it
    falls back to the system code page (Windows-1252 on US/EU systems).
    A non-ASCII multi-byte UTF-8 sequence in the source then gets
    mis-decoded into multiple Windows-1252 characters; some of those
    (notably ``U+201C`` left curly quote, byte ``0x93``) are valid
    PowerShell string delimiters, which derails the parser and produces
    cascading "unexpected token" errors at end-of-function.

    The clean fix is to keep the script ASCII-only so it parses
    identically regardless of how PowerShell guesses the encoding.
    Replacements used in the script:
    ``->`` for arrows, ``[OK]`` / ``[X]`` for check/cross,
    ``--`` for em-dash, ``...`` for ellipsis, ``*`` for bullet,
    ``-`` for box-drawing horizontal.
    """
    data = INSTALL_PS1.read_bytes()
    if not data.isascii():
        first = next(i for i, b in enumerate(data) if b > 127)
        raise AssertionError(
            f"install.ps1 contains a non-ASCII byte (0x{data[first]:02X}) at offset {first}. "
            "Replace with ASCII equivalents -- Windows PowerShell 5.1 reads BOM-less "
            "files as Windows-1252 and mangles multi-byte UTF-8 into curly quotes "
            "that derail the parser."
        )


def test_install_sh_has_no_utf8_bom() -> None:
    """``install.sh`` must be UTF-8 without BOM.

    A BOM (``EF BB``) at the start of the file breaks the kernel's
    ``#!`` shebang detection (the first two bytes are no longer ``#!``),
    so the script is not honored as a shell script. Even if invoked
    explicitly (``sh install.sh``), the BOM characters become a literal
    command at the top of the script.
    """
    data = INSTALL_SH.read_bytes()
    assert not data.startswith(UTF8_BOM), (
        "install.sh must not start with a UTF-8 BOM (EF BB BF). It breaks "
        "the `#!` shebang detection so the kernel won't honor the interpreter "
        "directive, and the BOM characters become a literal command."
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
    auto-convert.  This is also documented in ``.gitattributes``.
    """
    data = INSTALL_PS1.read_bytes()
    assert b"\r\n" not in data, (
        "install.ps1 contains CRLF line endings; should be LF-only (see .gitattributes)."
    )


def test_install_sh_uses_lf_line_endings() -> None:
    """``install.sh`` must use LF line endings.

    Unlike ``install.ps1``, this is hard-required: a CRLF in a POSIX
    shell script causes ``\\r`` to be appended to every token, producing
    errors like ``: command not found`` and breaking the shebang line.
    """
    data = INSTALL_SH.read_bytes()
    assert b"\r\n" not in data, (
        "install.sh contains CRLF line endings; must be LF-only or POSIX "
        "shells will see literal carriage returns and break the shebang."
    )
