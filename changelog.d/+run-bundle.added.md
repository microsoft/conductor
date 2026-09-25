**Run bundles and offline closure reporting**: added `conductor bundle build`
to package self-contained, content-addressed workflow closures into
`$CONDUCTOR_HOME/cache/bundles/`, the `workflow.bundle` YAML block for declaring
static assets and additional authorized roots, and the offline Bundle Closure
section in `conductor validate --environment`.
