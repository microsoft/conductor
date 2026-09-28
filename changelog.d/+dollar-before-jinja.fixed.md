A `$` written directly before a Jinja expression, as in
`${{ workflow.input.budget }}`, is no longer read as an environment variable
reference, so the workflow loads instead of failing with "Required environment
variable '{ workflow.input.budget ' is not set".
