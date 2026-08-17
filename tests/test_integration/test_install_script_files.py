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

import re
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


# ---------------------------------------------------------------------------
# Private / mirrored package index support
# ---------------------------------------------------------------------------
#
# Both install scripts resolve dependencies through uv, which reads
# UV_DEFAULT_INDEX. On networks that block the public Python index the
# install fails, and the only actionable remedy is that variable -- so the
# scripts must (a) surface it, and (b) never hardcode a specific vendor's
# mirror, since Conductor is public OSS installed on many different
# networks.

README = REPO_ROOT / "README.md"

# The anchor both scripts link to from their failure guidance.
README_INDEX_ANCHOR = "installing-behind-a-proxy-or-private-package-index"

# Any index URL appearing in the scripts must be one of these: the public
# index (named as the thing being blocked) or the documented placeholder.
# Checking the shape rather than denylisting known vendors keeps this test
# from having to name anyone's internal infrastructure -- and catches a
# mirror this list's author never heard of.
ALLOWED_INDEX_URL_MARKERS = ("pypi.org", "<your-index-host>")

# Matches an index-looking URL: an https host followed by a path segment
# that PEP 503 Simple-API endpoints universally end in.
INDEX_URL_RE = re.compile(r"https://[^\s\"'`)\]]*?/simple/?", re.IGNORECASE)

# The guidance function bodies, which is where the user-facing advice has to
# live. Scoping the assertions here rather than to the whole file is
# load-bearing: both scripts also describe UV_DEFAULT_INDEX in their header
# comments, so a whole-file search passes even with every guidance function
# deleted -- verified by deleting them.
GUIDANCE_BLOCK_RE = {
    "install.sh": re.compile(r"^print_index_guidance\(\) \{.*?^\}", re.S | re.M),
    "install.ps1": re.compile(r"^function Write-IndexGuidance \{.*?^\}", re.S | re.M),
}


def _guidance_body(path: Path) -> str:
    """Return the blocked-index guidance function body, or fail loudly."""
    text = path.read_text(encoding="utf-8")
    match = GUIDANCE_BLOCK_RE[path.name].search(text)
    assert match, (
        f"could not locate the blocked-index guidance function in {path.name}. "
        f"If it was renamed, update GUIDANCE_BLOCK_RE -- do not widen the "
        f"search to the whole file, which would make these assertions vacuous."
    )
    return match.group(0)


def test_install_scripts_classify_a_blocked_package_index() -> None:
    """Both scripts must classify an unreachable index as its own failure mode.

    Without this, a policy-blocked index burns the full retry backoff and
    then reports generic file-lock advice (including a Windows Defender
    exclusion), which is misleading for a network block.

    Checks the classifier is *called*, not merely defined -- a function
    nobody invokes would satisfy a bare name search. Behavior is covered by
    ``test_install_scripts.py``; this only guards the default suite against
    the feature being deleted wholesale.
    """
    sh = INSTALL_SH.read_text(encoding="utf-8")
    ps = INSTALL_PS1.read_text(encoding="utf-8")
    # Two occurrences minimum: the definition and at least one call site.
    assert sh.count("is_definitive_index_block") >= 2, (
        "install.sh defines the index-block classifier but never calls it"
    )
    assert ps.count("Test-DefinitiveIndexBlock") >= 2, (
        "install.ps1 defines the index-block classifier but never calls it"
    )
    # A git failure is worded like an index failure by uv, so both scripts
    # must tell them apart or a blocked github.com is misdiagnosed.
    assert sh.count("is_git_host_failure") >= 2, (
        "install.sh does not distinguish a git-remote failure from an index one"
    )
    assert ps.count("Test-GitHostFailure") >= 2, (
        "install.ps1 does not distinguish a git-remote failure from an index one"
    )


def test_install_scripts_name_the_uv_index_variable() -> None:
    """The guidance itself must point users at ``UV_DEFAULT_INDEX``.

    This is the only setting that fixes a blocked index for these scripts.
    ``pip config set global.index-url`` does *not* work -- uv never reads
    pip's configuration -- so the guidance must say so rather than leave
    users following advice that silently does nothing.
    """
    for path in (INSTALL_SH, INSTALL_PS1):
        body = _guidance_body(path)
        assert "UV_DEFAULT_INDEX" in body, f"{path.name}'s guidance never mentions UV_DEFAULT_INDEX"
        assert "does not read pip" in body.lower(), (
            f"{path.name}'s guidance must warn that uv ignores pip's configuration"
        )


def test_install_scripts_do_not_hardcode_a_private_index() -> None:
    """Neither script may ship a specific organization's package mirror.

    Conductor is public OSS. A mirror baked in here would silently redirect
    every other user's dependency resolution through one organization's
    proxy. Private-index support is *configuration* the user supplies via
    ``UV_DEFAULT_INDEX``; every index URL in the scripts must therefore be
    either the public index (named as the thing being blocked) or the
    documented ``<your-index-host>`` placeholder.

    This checks the shape of any index URL rather than denylisting known
    vendors, so it catches a mirror this test's author never heard of --
    and, just as importantly, doesn't require naming anyone's internal
    infrastructure inside a public repository.
    """
    for path in (INSTALL_SH, INSTALL_PS1):
        text = path.read_text(encoding="utf-8")
        for url in INDEX_URL_RE.findall(text):
            assert any(marker in url for marker in ALLOWED_INDEX_URL_MARKERS), (
                f"{path.name} contains a hardcoded package index URL: {url!r}. "
                f"Conductor must not ship a default mirror -- users supply "
                f"their own via UV_DEFAULT_INDEX. Use the "
                f"'<your-index-host>' placeholder in documentation instead."
            )


def test_install_scripts_link_to_a_real_readme_section() -> None:
    """The failure guidance's docs link must resolve to a real README anchor.

    The scripts print this URL at the exact moment a user is blocked, so a
    dangling anchor sends them to a page that doesn't explain anything.
    """
    readme = README.read_text(encoding="utf-8")
    assert "### Installing behind a proxy or private package index" in readme, (
        "README is missing the section the install scripts link to; "
        f"expected a heading generating the anchor '#{README_INDEX_ANCHOR}'."
    )
    for path in (INSTALL_SH, INSTALL_PS1):
        text = path.read_text(encoding="utf-8")
        assert README_INDEX_ANCHOR in text, (
            f"{path.name} does not link to the README's private-index section"
        )
