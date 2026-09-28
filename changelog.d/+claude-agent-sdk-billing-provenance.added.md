`claude-agent-sdk` executions now record where their model usage is billed
from (a Claude Code subscription, a metered API key, or unknown), and cost
figures are labelled to match. Subscription usage is shown as an
**API-equivalent estimate** rather than as a charge; mixed workflows say so and
break the executions down; providers that do not report a billing source are
displayed exactly as before. The label appears in the console usage summary,
per-agent progress lines, budget messages, `conductor status`, the web
dashboard, and the Fleet Runs, Run Detail and History screens, and is carried as
`billing_mode` on agent completion events and as `billing` in MCP run-status
payloads. A billing source is only claimed when it can be proven: an inherited
cloud-backend selector, gateway token, custom endpoint, or settings tier gives
`unknown`. `limits.budget_usd` still applies to the estimated cost.
