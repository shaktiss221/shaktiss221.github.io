---
week: 9
phase: Agents and MCP
title: Workflows and agents
goal: Know when to use a fixed workflow and when to let the model decide, and build both.
---

## Building effective agents (part 1)
learn: 25 | Anthropic, Building effective agents: up to the end of the workflow patterns | https://www.anthropic.com/engineering/building-effective-agents
note: 5 | Write the difference between a workflow and an agent in one sentence.

## Building effective agents (part 2)
learn: 20 | Anthropic, Building effective agents: agents and the appendix | https://www.anthropic.com/engineering/building-effective-agents
do: 7 | Classify everything you have built so far as a workflow or an agent.
note: 3 | Where would an agent be overkill?

## Prompt chaining: dispute intake
do: 25 | Build a chain for a payment dispute: classify the dispute, fetch the transaction with your ledger tools, find the relevant policy with your search, then draft a response. Check the output between steps.
note: 5 | Where did you add a check between steps, and why?

## Routing
do: 25 | Route disputes by type (failed debit, double charge, merchant dispute, fraud) to specialised prompts. Add an "unsure" route that goes to a human.
note: 5 | How accurate was the router on 10 test disputes?

## Evaluator and optimiser
do: 25 | Add a loop: draft a response, critique it against policy and tone rules, then revise. Cap it at two revisions and log every round.
note: 5 | Did the second revision help, or just add cost?

## Context engineering
learn: 25 | Anthropic, Effective context engineering for AI agents | https://www.anthropic.com/research/effective-context-engineering-for-ai-agents
note: 5 | What should never go into your dispute agent's context window?

## Week 9 review: agent failure modes
do: 20 | Run 10 disputes end to end and log every failure: loops, wrong tools, missing evidence, overconfident answers. Add a step limit, a token budget and a hand-off-to-human path.
note: 10 | Write a short note: what would make an agent trustworthy enough for payments?
