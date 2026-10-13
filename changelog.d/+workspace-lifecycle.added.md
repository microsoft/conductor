**Retained workspace lifecycle, honest resume digest gate, and restart policy**:
Workflows running on the Docker execution backend can now retain container
workspace volumes across failures and resumptions using `workflow.workspace`
(`persistence: on-failure` or `durable`). On resume, Conductor enforces an
honest digest gate that verifies bundle content (when recorded) and manifest
semantic digests against the checkpoint's resume contract before attaching
or altering retained Docker volumes. Resumption also respects step-level
`restart:` policies (`rerun` or `fail`), controlling whether interrupted
in-flight steps re-execute or fail closed.
