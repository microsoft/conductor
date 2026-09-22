**`dialog.conversation_prompt`**: instructions for the agent holding a dialog
conversation, appended to the built-in dialog system prompt. Previously
`trigger_prompt` was the only field on a `dialog:` block, and it reaches
only the evaluator that decides whether to open the dialog, so a workflow
author had no way to instruct the conversing agent.
