---
week: 2
phase: Foundations
title: How LLMs work
goal: Build an intuition for tokens, embeddings, attention and sampling, enough to reason about cost, latency and failure modes.
---

## Transformers, visually (part 1)
learn: 15 | 3Blue1Brown, Transformers, the tech behind LLMs: first half | https://www.3blue1brown.com/lessons/gpt
do: 10 | Write in your own words what a token, an embedding and next-token prediction are.
note: 5 | Write down one thing that surprised you.

## Transformers, visually (part 2)
learn: 15 | 3Blue1Brown, Transformers, the tech behind LLMs: second half | https://www.3blue1brown.com/lessons/gpt
do: 12 | Install `tiktoken` and count tokens for the same 20 transactions written as JSON, as CSV and as plain text. Which format is cheapest?
note: 3 | Note the ratio. You will use it when you design prompts.
also: tiktoken | https://github.com/openai/tiktoken

## Attention (part 1)
learn: 20 | 3Blue1Brown, Attention in transformers: first half | https://www.3blue1brown.com/lessons/attention
do: 7 | On paper, sketch what attention connects in the sentence "refund the failed UPI payment".
note: 3 | In one line: what does attention let a word "see"?

## Attention (part 2)
learn: 15 | 3Blue1Brown, Attention in transformers: second half | https://www.3blue1brown.com/lessons/attention
do: 12 | Explain attention and context windows to a junior engineer in five sentences. Then ask an AI to critique your explanation.
note: 3 | Why do long prompts cost more and run slower?

## How a model is trained
learn: 25 | Andrej Karpathy, Deep Dive into LLMs like ChatGPT: first 25 minutes (pre-training data and tokenization) | https://www.youtube.com/watch?v=7xTGNNLPyMI
note: 5 | Write the pipeline in one line: data, tokens, pre-training, base model.

## From base model to assistant
learn: 25 | Andrej Karpathy, Deep Dive into LLMs like ChatGPT: the post-training and hallucinations chapters | https://www.youtube.com/watch?v=7xTGNNLPyMI
note: 5 | Write two reasons models make things up, and one mitigation for each.

## Week 2 review: a sampling experiment
do: 20 | In a chat UI or API playground, run one prompt ("categorise this transaction: ...") five times at temperature 0 and five times at 1. Record how much the answers vary.
note: 10 | Write a one-page note, "How an LLM answers my prompt": tokens, embeddings, attention, sampling and post-training. It is your reference for the rest of the path.
