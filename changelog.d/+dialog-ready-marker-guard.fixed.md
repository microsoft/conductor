**Dialog mode no longer ends an interview on "ready for the next question"**:
a trailing `[READY_TO_CONTINUE]` is withheld when the agent's message still
asks or announces a question, the agent is told not to combine the two, and
the continue prompt names the agent and says which replies end the dialog
(`yes`, or a dismiss keyword; anything else is sent to the agent, and an
empty reply re-asks). The opening banner lists every dismiss keyword, and
`dialog_completed` carries `agent_question_outstanding` when a dialog closed
under an unanswered question from the agent. On the web path the leave-dialog
control and a dismiss keyword at the continue proposal both end the dialog as
a dismissal, as on the terminal.
