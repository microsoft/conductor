**`claude-agent-sdk` provider: explicit authentication mode selection.** The
new `runtime.provider.auth_mode` field (`"auto"` default, `"subscription"`,
`"api_key"`) selects which credential the `claude` child process uses. `auto`
leaves the inherited environment unchanged, so existing workflows are
unaffected. `subscription` blanks `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`
and `CLAUDE_CODE_OAUTH_TOKEN` in the child environment; `api_key` requires a
non-blank `ANTHROPIC_API_KEY` and blanks the other two. Both explicit modes
refuse an inherited non-blank `CLAUDE_CODE_USE_BEDROCK` / `_VERTEX` /
`_FOUNDRY` cloud-backend selector, naming the variable but never its value;
`auto` keeps it. Both explicit modes also refuse a non-empty
`setting_sources`, at `conductor validate` and at run time, because a Claude
Code settings file's `env` block is applied after Conductor configures the
child environment. Each agent execution captures its environment, working
directory, settings tiers, and CLI path once; a readiness check (`claude auth
status --json`, skipped for `api_key`) and the SDK session both use that
capture — including the same settings tiers, passed to the check as
`--setting-sources` — and Conductor's own environment is never modified. Every
mode, `auto` with an API key included, requires the `claude` CLI to be
installed, checked without running it. The readiness check is not billing
attribution. `conductor doctor --check` shows Conductor's inferred mode
separately from the CLI-reported `authMethod` / `apiProvider` / `apiKeySource`
/ `subscriptionType`, and states that it checked the default provider
configuration rather than any workflow's `auth_mode`. See
`docs/workflow-syntax.md` (Authentication Mode) and `docs/configuration.md`.
