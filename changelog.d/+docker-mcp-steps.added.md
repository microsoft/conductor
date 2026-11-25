Direct stdio MCP steps can run with a Docker execution profile, sharing the
run's workspace with other Docker steps. Matching connections can be reused,
and their containers are removed before the workspace is finalized.
