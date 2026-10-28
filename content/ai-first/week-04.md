---
week: 4
phase: Build with model APIs
title: Tools and function calling
goal: Let the model call your code: tool definitions, the tool loop, good tool design and safe write actions.
---

## How tool use works
learn: 15 | Claude docs: how tool use works | https://platform.claude.com/docs/en/agents-and-tools/tool-use/how-tool-use-works
do: 12 | Define a `get_exchange_rate(from, to)` tool backed by a hard-coded table. Ask a question that needs it and print the raw `tool_use` block.
note: 3 | Who actually runs the tool: the model or your code?

## The tool loop
learn: 10 | Claude docs: implement tool use | https://platform.claude.com/docs/en/agents-and-tools/tool-use/implement-tool-use
do: 17 | Implement the loop: call the model, run any tools it asks for, send the results back, and repeat until it stops asking. Cap it at five iterations.
note: 3 | What should happen when the cap is hit?

## A mock ledger with tools
do: 27 | Create a SQLite ledger (users, and transactions with status, amount, merchant and created_at) with 500 fake rows. Expose `get_transaction(id)` and `list_transactions(user_id, from, to, status)` as tools.
note: 3 | Ask five questions and log which tools were called.

## Designing tools a model can use
learn: 15 | Anthropic, Writing effective tools for agents | https://www.anthropic.com/research/writing-tools-for-agents
do: 12 | Improve your tool names, descriptions and error messages using the article. Return useful errors such as "no transactions in range; try a wider date range".
note: 3 | Re-run yesterday's five questions. What improved?

## Read tools vs write tools
learn: 10 | Anthropic, The "think" tool | https://www.anthropic.com/research/claude-think-tool
do: 17 | Add a `refund_transaction(id, reason)` tool that only runs after an explicit human "yes" in the command line. Log every write with a request ID.
note: 3 | Write your rule for which tools need human confirmation.

## Parallel calls and bad inputs
do: 25 | Ask a question that needs several tool calls at once, such as comparing spending for three users. Then send malformed tool arguments on purpose and make your code reject them cleanly.
note: 5 | List three validation checks every tool should have.

## Week 4 build: ledger Q&A bot
do: 25 | Polish the bot until it handles questions like "How much did user 42 spend on food last month?" and "Which payments failed yesterday, and why?". Save 10 questions with their correct answers in `ledger_questions.jsonl`; you will reuse them for evals in week 7.
note: 5 | Log everything that broke this week.
also: Java option: LangChain4j tool calling | https://github.com/langchain4j/langchain4j
