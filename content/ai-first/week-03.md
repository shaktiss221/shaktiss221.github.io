---
week: 3
phase: Build with model APIs
title: Calling models from code
goal: Get comfortable with the Messages API: conversations, tokens, cost, structured output, retries and caching.
---

## Your first API call
learn: 10 | Anthropic Python SDK: README | https://github.com/anthropics/anthropic-sdk-python
do: 17 | Create an API key with a small spending limit, set up a Python environment (`uv` or `venv`) and send your first message from a script. Commit it to `ai-first-lab`.
note: 3 | Note the model name, latency and token counts from the response.

## Conversations and system prompts
do: 25 | Build a 40-line command-line chat that keeps message history and reads its system prompt from a file. Try a "senior payments reviewer" persona on a design question.
note: 5 | What happens to cost as the conversation grows? (The whole history is sent on every turn.)

## Tokens, limits and cost
do: 25 | Log input tokens, output tokens, latency and cost for every call your chat makes. Set `max_tokens` and a stop sequence, and watch what changes.
note: 5 | Estimate the monthly cost of 10,000 such calls a day.

## Structured output with a tool
learn: 10 | Claude docs: tool use overview | https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview
do: 17 | Use one tool with a JSON schema to extract `{merchant, amount, currency, date, category}` from 10 messy transaction SMS texts. Validate the result with `pydantic`.
note: 3 | Which fields fail most often?

## Failure handling, payments-style
do: 25 | Wrap your client with timeouts, retries with exponential backoff and jitter, and a request ID in every log line. Simulate a timeout and a rate-limit error.
note: 5 | Which of your payments reliability habits transfer directly?

## Prompt caching
learn: 12 | Claude docs: prompt caching | https://platform.claude.com/docs/en/build-with-claude/prompt-caching
do: 15 | Put a long, fixed system prompt (your category rules) behind a cache breakpoint. Compare latency and cost for 10 calls before and after.
note: 3 | When would caching not help?

## Week 3 build: transaction categoriser
do: 25 | Combine the week into one command-line tool: read a bank-statement CSV, categorise each row with structured output, retry on failure and print the total cost. Hand-check 20 rows and record the accuracy.
note: 5 | Skim a Java library for the same job and note how you would port it.
also: Java option: Spring AI | https://github.com/spring-projects/spring-ai
also: More worked examples: Claude cookbooks | https://github.com/anthropics/claude-cookbooks
