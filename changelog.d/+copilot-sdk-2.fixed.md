**Copilot agents work with Copilot CLI 1.0.113 and later again.** Those CLIs
speak Copilot SDK protocol 4, which no `github-copilot-sdk` 1.x release
supports, so every Copilot agent failed at startup with `SDK protocol version
mismatch: SDK supports versions 3-3, but server reports version 4` whenever the
SDK reached such a CLI (for example through `COPILOT_CLI_PATH`, or an external
`runtime_url`). The Copilot provider now uses the SDK's renamed 2.x release,
`microsoft-copilot-sdk>=2.0.1,<3` (still imported as `copilot`), which speaks
protocol 4.

Behaviour is otherwise unchanged. The Copilot runtime still runs as a child
process that inherits Conductor's environment (`COPILOT_CLI_PATH`, CAPI and
integration-id variables) rather than SDK 2.x's new in-process default, so
dead-runtime recovery and crash isolation keep working, and `runtime.provider`
custom routing still sends every model to the configured endpoint.

SDK 2.x cannot talk to protocol-3 CLIs (Copilot CLI 1.0.112 and earlier). If
`COPILOT_CLI_PATH` or `runtime_url` points at one, Conductor now stops with a
non-retryable error naming the CLI to update instead of retrying; unset
`COPILOT_CLI_PATH` to let the SDK download a matching CLI. Install-script
upgrades replace the old SDK automatically. In an environment you manage
yourself, `pip uninstall github-copilot-sdk` before upgrading, because both
distributions install the same `copilot` package. Existing Docker and ACA
runner images keep their own SDK and CLI pair and keep working; rebuild them
to move to SDK 2.x, and rebuild any image that supplies its own Copilot CLI
newer than 1.0.112.
