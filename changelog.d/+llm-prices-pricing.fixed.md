Corrected the built-in prices for 12 OpenAI and Gemini models that were
billing at a placeholder rate (for example `gpt-5.5` at $2/$8 per million
tokens instead of $5/$30), and added a `pricing` extra
(`pip install 'conductor-cli[pricing]'`) that prices models from the
llm-prices package so new models and price changes do not wait for a
Conductor release.
