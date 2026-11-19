Corrected the built-in prices for 12 OpenAI and Gemini models that were
billing at a placeholder rate (for example `gpt-5.5` at $2/$8 per million
tokens instead of $5/$30), and added a `pricing` extra
(`pip install 'conductor-cli[pricing]'`, or `--extras pricing` with the
install scripts) that prices models from the llm-prices package at the host
that served them, so new models and price changes do not wait for a
Conductor release. When two providers serve the same
model in one run, each execution is priced at its own host's rate, and an
unknown host stays unpriced.
