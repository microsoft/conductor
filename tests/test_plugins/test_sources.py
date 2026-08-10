"""Tests for the ``plugin_sources:`` source-string grammar."""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from conductor.plugins.errors import PluginSourceError
from conductor.plugins.sources import parse_plugin_source


class TestOwnerRepoShorthand:
    """``owner/repo`` is the GitHub shorthand, not a relative path."""

    def test_expands_to_a_github_remote(self):
        source = parse_plugin_source("acme/agent-plugins")
        assert source.location == "https://github.com/acme/agent-plugins.git"
        assert source.is_local is False
        assert source.ref is None

    def test_is_not_classified_as_a_path(self):
        """The trap this module exists to avoid.

        ``skills.registry.is_path_entry`` returns True for anything
        containing a separator, so reusing it here would turn every
        ``owner/repo`` into a relative directory lookup.
        """
        from conductor.skills.registry import is_path_entry

        assert is_path_entry("acme/agent-plugins") is True
        assert parse_plugin_source("acme/agent-plugins").is_local is False

    def test_carries_a_ref(self):
        source = parse_plugin_source("acme/agent-plugins#v1.4.0")
        assert source.ref == "v1.4.0"
        assert source.location == "https://github.com/acme/agent-plugins.git"

    def test_cache_key_is_host_owner_repo(self):
        source = parse_plugin_source("acme/agent-plugins#v1.4.0")
        assert source.cache_key == PurePosixPath("github.com/acme/agent-plugins")

    def test_strips_a_git_suffix(self):
        assert parse_plugin_source("acme/plugins.git").repo == "plugins"


class TestUrlSources:
    """Explicit-scheme URLs, with credentials and ports discarded."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://github.com/acme/p.git", "github.com/acme/p"),
            ("http://git.internal/acme/p", "git.internal/acme/p"),
            ("ssh://git@gitlab.com/acme/p.git", "gitlab.com/acme/p"),
            ("git://example.org/acme/p", "example.org/acme/p"),
        ],
    )
    def test_cache_key(self, raw, expected):
        assert parse_plugin_source(raw).cache_key == PurePosixPath(expected)

    def test_credentials_and_port_never_reach_the_cache_key(self):
        """A token in a URL must not become a directory name on disk."""
        source = parse_plugin_source("https://user:secret@git.internal:8443/acme/p.git")
        assert source.cache_key == PurePosixPath("git.internal/acme/p")
        assert "secret" not in str(source.cache_key)

    def test_nested_paths_collapse_into_the_owner_slot(self):
        """Self-hosted forges nest arbitrarily; the key stays 3 segments."""
        source = parse_plugin_source("https://gitlab.internal/group/sub/team/p.git")
        assert source.cache_key == PurePosixPath("gitlab.internal/group_sub_team/p")

    def test_repository_at_the_host_root_gets_a_placeholder_owner(self):
        source = parse_plugin_source("https://git.internal/p.git")
        assert source.cache_key == PurePosixPath("git.internal/_/p")

    def test_ref_is_split_off(self):
        source = parse_plugin_source("https://github.com/acme/p.git#main")
        assert source.ref == "main"
        assert source.location == "https://github.com/acme/p.git"

    def test_file_urls_are_remote_but_keyed_locally(self):
        source = parse_plugin_source("file:///srv/plugins")
        assert source.is_local is False
        assert source.cache_key.parts[0] == "_local"


class TestScpStyleSources:
    """``user@host:path`` — the form an SSH remote is usually written in."""

    def test_parses(self):
        source = parse_plugin_source("git@github.com:acme/p.git")
        assert source.is_local is False
        assert source.cache_key == PurePosixPath("github.com/acme/p")
        assert source.location == "git@github.com:acme/p.git"

    def test_carries_a_ref(self):
        source = parse_plugin_source("git@github.com:acme/p.git#3f2a1c9")
        assert source.ref == "3f2a1c9"

    def test_a_windows_drive_letter_is_a_path_not_a_host(self):
        r"""``C:\src\plugins`` must not parse as host ``C``."""
        source = parse_plugin_source(r"C:\src\plugins")
        assert source.is_local is True


class TestLocalPaths:
    """Local sources are recognised by prefix, never by separator."""

    @pytest.mark.parametrize("raw", ["./vendor/plugins", "../plugins", "~/src/plugins", "/srv/p"])
    def test_recognised(self, raw):
        assert parse_plugin_source(raw).is_local is True

    def test_a_ref_on_a_local_path_is_refused(self):
        """There is nothing to check out, so a ref would silently do nothing."""
        with pytest.raises(PluginSourceError, match="local path with a"):
            parse_plugin_source("./vendor/plugins#v1.0.0")

    def test_location_is_left_unresolved(self):
        """Anchoring needs the workflow directory, which the parser lacks."""
        assert parse_plugin_source("./vendor/plugins").location == "./vendor/plugins"


class TestPinning:
    """Only a full SHA is immutable."""

    def test_full_sha_is_pinned(self):
        sha = "9c4e1f2a8b3d5e7091a2c4b6d8e0f1a3c5b7d9e1"
        assert parse_plugin_source(f"acme/p#{sha}").is_pinned is True

    @pytest.mark.parametrize("ref", ["v1.4.0", "main", "9c4e1f2", "release/1.x"])
    def test_everything_else_floats(self, ref):
        assert parse_plugin_source(f"acme/p#{ref}").is_pinned is False

    def test_an_abbreviated_sha_is_not_treated_as_pinned(self):
        """It is ambiguous by construction and cannot be expanded offline."""
        assert parse_plugin_source("acme/p#9c4e1f2a8b3d").is_pinned is False

    def test_no_ref_floats(self):
        assert parse_plugin_source("acme/p").is_pinned is False


class TestRejections:
    """A source that matches nothing is refused rather than guessed at."""

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_empty(self, raw):
        with pytest.raises(PluginSourceError, match="non-empty"):
            parse_plugin_source(raw)

    def test_unrecognised_form(self):
        with pytest.raises(PluginSourceError, match="not a recognised plugin source"):
            parse_plugin_source("just-a-word")

    def test_empty_ref(self):
        with pytest.raises(PluginSourceError, match="empty '#ref'"):
            parse_plugin_source("acme/p#")

    def test_ref_with_no_location(self):
        with pytest.raises(PluginSourceError, match="no location"):
            parse_plugin_source("#v1.0.0")

    def test_url_with_no_host(self):
        with pytest.raises(PluginSourceError, match="no host"):
            parse_plugin_source("https:///acme/p")

    def test_three_segment_path_is_not_owner_repo(self):
        """``a/b/c`` is neither the GitHub shorthand nor a recognised URL."""
        with pytest.raises(PluginSourceError, match="not a recognised plugin source"):
            parse_plugin_source("acme/group/plugins")


class TestDescribe:
    """The one-line description used in validate output."""

    def test_remote_shows_the_source_verbatim(self):
        assert parse_plugin_source("acme/p#v1.0.0").describe() == "acme/p#v1.0.0"

    def test_local_is_marked_as_such(self):
        assert parse_plugin_source("./vendor/p").describe() == "./vendor/p (local path)"


class TestCacheKeyContainment:
    """A cache key is joined into a path and created with ``parents=True``.

    A ``..`` reaching it writes outside the plugin cache root — into the
    sibling registry cache, for instance. Refused rather than sanitised:
    a repository genuinely named ``..`` does not exist, so this is always
    malformed or hostile.
    """

    @pytest.mark.parametrize(
        "raw",
        [
            "https://../evil/foo.git",
            "https://github.com/../evil",
            "https://github.com/acme/..",
            "foo/..",
            "acme/.",
        ],
    )
    def test_relative_components_are_refused(self, raw):
        with pytest.raises(PluginSourceError):
            parse_plugin_source(raw)

    def test_a_relative_local_path_is_unaffected(self):
        """``../shared/plugins`` is a path, not a cache key.

        It is anchored on the workflow file's directory and read in
        place, so it never reaches the cache — the same trusted-input
        treatment a ``plugins:`` path already gets.
        """
        source = parse_plugin_source("../shared/plugins")
        assert source.is_local is True

    def test_a_legitimate_dot_inside_a_name_is_fine(self):
        """``..`` is refused; a dot *within* a segment is ordinary."""
        source = parse_plugin_source("acme/my.plugins")
        assert source.repo == "my.plugins"

    def test_no_key_segment_is_ever_a_traversal(self):
        for raw in ("acme/p", "https://gitlab.com/group/sub/p.git", "git@host.io:a/b.git"):
            assert not any(part in {".", ".."} for part in parse_plugin_source(raw).cache_key.parts)


class TestRemoteHelperTransports:
    """``git-remote-ext`` runs its argument as a shell command.

    ``ext::sh -c '...'`` must not parse as an scp-style remote: declaring
    a source is meant to consent to fetching a repository, not to running
    a command. Modern git blocks the transport by default, but the
    default is configurable — so the parser refuses the shape and
    ``fetch.py`` additionally pins ``protocol.ext.allow=never``.
    """

    @pytest.mark.parametrize(
        "raw", ['ext::sh -c "echo pwn"', "transport::runthis", "ext::whatever"]
    )
    def test_refused(self, raw):
        with pytest.raises(PluginSourceError, match="not a recognised plugin source"):
            parse_plugin_source(raw)

    def test_an_ordinary_scp_remote_still_parses(self):
        source = parse_plugin_source("git@github.com:acme/p.git")
        assert source.location == "git@github.com:acme/p.git"


class TestCredentialRedaction:
    """A token in a source must not reach a console, log, or bug report."""

    def test_display_redacts_a_url_credential(self):
        source = parse_plugin_source("https://x-token:ghp_SECRET@github.com/acme/p.git#main")
        assert "ghp_SECRET" not in source.display
        assert source.display == "https://***@github.com/acme/p.git#main"

    def test_describe_uses_the_redacted_form(self):
        source = parse_plugin_source("https://u:ghp_SECRET@github.com/acme/p.git")
        assert "ghp_SECRET" not in source.describe()

    def test_the_cache_key_never_carried_it_either(self):
        source = parse_plugin_source("https://u:ghp_SECRET@github.com/acme/p.git")
        assert "ghp_SECRET" not in str(source.cache_key)

    def test_git_stderr_is_redacted_too(self):
        """git quotes the remote URL back on most failures."""
        from conductor.plugins.sources import redact_credentials

        message = "fatal: unable to access 'https://u:ghp_SECRET@github.com/a/b.git/'"
        assert "ghp_SECRET" not in redact_credentials(message)

    def test_a_source_without_credentials_is_unchanged(self):
        source = parse_plugin_source("acme/p#v1.0.0")
        assert source.display == "acme/p#v1.0.0"
