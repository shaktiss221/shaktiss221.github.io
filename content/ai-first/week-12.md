---
week: 12
phase: Under the hood and capstone
title: "Capstone: payment dispute copilot"
goal: Ship one complete, evaluated AI system in your own domain, and turn it into career evidence.
---

## Capstone design
do: 25 | Write a one-page design for a payment dispute copilot: retrieval over policies, ledger tools over MCP, a routing workflow, evals, guardrails and cost limits. Include a diagram.
note: 5 | What is explicitly out of scope?

## Wire it together
do: 30 | Assemble the pieces from weeks 5 to 10 into one repo with a single entry point and one config file for models, budgets and tools.

## Evals in CI
do: 25 | Add a GitHub Actions workflow that runs the code checks on every push and the full eval set whenever prompts change. Fail the build below your threshold.
note: 5 | Record the score in the README.

## Guardrails and budgets
do: 25 | Add the injection test, data-masking tests, a per-request token budget and a daily cost cap. Re-run all evals.
note: 5 | What would you monitor in the first week of production?

## Write it up
do: 25 | Write the README: the problem, an architecture diagram, eval results, costs, failure modes and next steps. Record a two-minute demo.
note: 5 | Share it with one engineer you respect and ask for blunt feedback.

## Make it career evidence
do: 25 | Add the capstone and the MCP server to your resume and LinkedIn as concrete bullets. Prepare two interview stories: an AI system design, and a fix you drove with evals.
note: 5 | Which roles will you apply to with this?

## Retrospective and the next 90 days
do: 15 | Re-read your AI-first log from day 1. List what you now do with AI by default that you didn't 12 weeks ago.
note: 15 | Pick a direction for the next cycle (AI platform and infrastructure, applied agents, or evals and quality) and write three goals for it.
