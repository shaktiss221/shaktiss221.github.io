---
week: 1
phase: Foundations
title: Work AI-first
goal: Use AI on real work every day, and get a first mental model of what a large language model is.
---

## What an LLM actually is
learn: 20 | Andrej Karpathy, Intro to Large Language Models: Part 1, LLMs | https://www.youtube.com/watch?v=zjkBMFhNj_g
do: 7 | Set up an AI coding assistant (Claude Code, Cursor or similar) on a personal repo and ask it to explain one file you know well. Note what it got wrong.
note: 3 | Create a repo called `ai-first-lab` with an `ai-first-log.md`. Write three tasks from this week's work that you will try with AI first.

## Where LLMs are heading
learn: 20 | Andrej Karpathy, Intro to Large Language Models: Part 2, the future of LLMs | https://www.youtube.com/watch?v=zjkBMFhNj_g
do: 7 | Give the assistant one task from your list: a design-doc outline, a migration script or a test file. Edit its output instead of starting from a blank page.
note: 3 | Log how long it took, what you kept and what you rewrote.

## How LLMs get attacked
learn: 15 | Andrej Karpathy, Intro to Large Language Models: Part 3, LLM security | https://www.youtube.com/watch?v=zjkBMFhNj_g
do: 12 | Paste the diff of a recent PR and ask for a review against your team's standards. Compare its comments with the human review you got.
note: 3 | Which kinds of issues does AI catch well, and which does it miss?

## Coding with an agent, properly
learn: 15 | Anthropic, Claude Code: best practices for agentic coding | https://www.anthropic.com/research/claude-code-best-practices
do: 12 | Write a project instructions file (`CLAUDE.md` or your tool's equivalent) for your side repo: build commands, conventions, what not to touch. Re-run yesterday's review with it.
note: 3 | Did the instructions change the output?

## Prompts are specs
learn: 15 | Claude docs: prompt engineering overview | https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/overview
do: 12 | Take one vague prompt you used this week and rewrite it as a spec: context, task, constraints, output format and one example. Run both and compare.
note: 3 | Write the one prompting rule you will use from now on.

## Prompting techniques that matter
learn: 15 | Claude docs: prompting best practices | https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices
do: 12 | Try three techniques on the same task: give examples, ask it to reason step by step, and wrap inputs in XML-style tags. Keep the version that works best as a template.
note: 3 | Save the template in your lab repo.
also: Anthropic's interactive prompt engineering tutorial | https://github.com/anthropics/prompt-eng-interactive-tutorial

## Week 1 review: your AI-first log
do: 20 | Read your log. Pick one recurring task at work (status updates, PR descriptions, test scaffolding, on-call summaries) and write the exact prompt you will use for it every time next week.
note: 10 | Write 150 words: where AI saved time, where it cost time, and what you will change.
