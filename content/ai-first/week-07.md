---
week: 7
phase: Evals, security and production
title: Evals
goal: Measure quality the way you measure latency: golden sets, code checks, LLM judges and error analysis.
---

## Why evals decide everything
learn: 25 | Hamel Husain, Your AI product needs evals | https://hamel.dev/blog/posts/evals/
note: 5 | Which of your bots has the least evidence that it works?

## Define success
learn: 10 | Claude docs: define success criteria and build evaluations | https://platform.claude.com/docs/en/test-and-evaluate/develop-tests
do: 17 | Turn your 15 policy questions into a JSONL eval set with the expected facts and source chunk for each. Add 15 more, including five that should be refused.
note: 3 | What makes an eval case hard?

## Code-based checks
do: 25 | Write pytest-style checks that need no model: valid JSON, at least one citation, every citation exists in the index, and refusals on unanswerable questions. Run them over all 30 cases.
note: 5 | Record the pass rate.

## LLM as judge
do: 25 | Write a judge prompt that marks faithfulness (is every claim supported by the cited chunks?) as pass or fail, with a reason. Label 10 cases yourself first, then check how often the judge agrees with you.
note: 5 | Where did the judge disagree with you?

## Evaluating agents
learn: 15 | Anthropic, Demystifying evals for AI agents | https://www.anthropic.com/research/demystifying-evals-for-ai-agents
do: 12 | Add evals to your week 4 ledger bot using `ledger_questions.jsonl`: right tool, right arguments, right final answer.
note: 3 | Which failure is most common: tool choice, arguments or the final answer?

## Eval tooling
learn: 10 | promptfoo: README | https://github.com/promptfoo/promptfoo
do: 17 | Run your policy eval set through promptfoo (or Ragas) and compare with your own script. Keep whichever is simpler.
note: 3 | Write how you would run this in CI on every prompt change.
also: Ragas | https://github.com/explodinggradients/ragas

## Week 7 review: error analysis
do: 25 | Read 20 failing cases end to end and group them into buckets: retrieval missed, wrong chunk, ignored context, formatting. Fix the biggest bucket and re-run.
note: 5 | Record before-and-after scores. This loop is the core skill of an AI engineer.
