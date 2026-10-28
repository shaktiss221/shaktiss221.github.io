---
week: 11
phase: Under the hood and capstone
title: Open models, fine-tuning and serving
goal: Run models yourself, see training at small scale, and know when fine-tuning beats prompting.
---

## Run a model locally
learn: 5 | Ollama: README | https://github.com/ollama/ollama
do: 22 | Run a small open model on your laptop and categorise the same 20 transactions from week 3. Compare accuracy and speed with the API model.
note: 3 | When would a small local model be good enough?

## Build a GPT from scratch (part 1)
learn: 25 | Andrej Karpathy, Neural Networks: Zero to Hero, the "Let's build GPT" lecture: first 25 minutes | https://github.com/karpathy/nn-zero-to-hero
note: 5 | What does the simple bigram model teach you about next-token prediction?

## Build a GPT from scratch (part 2)
learn: 25 | Andrej Karpathy, "Let's build GPT": the next 25 minutes (self-attention in code) | https://github.com/karpathy/nn-zero-to-hero
note: 5 | Connect the code to the 3Blue1Brown picture from week 2.
also: nanoGPT | https://github.com/karpathy/nanoGPT

## Prompting, RAG or fine-tuning?
learn: 15 | Hugging Face PEFT (LoRA): README | https://github.com/huggingface/peft
do: 12 | For your transaction categoriser, write down when you would fine-tune instead of prompting: data volume, latency, cost and consistency.
note: 3 | What data would you need at work to fine-tune well?

## A small LoRA fine-tune
do: 27 | In a free Colab notebook, fine-tune a small open model with LoRA on 200 labelled transactions. Getting it started is enough today; finish on the review day.
note: 3 | Note the training time and memory used.
also: Hugging Face LLM course | https://huggingface.co/learn/llm-course/chapter1/1

## Serving models
learn: 15 | vLLM: README | https://github.com/vllm-project/vllm
do: 12 | Estimate the cost per million tokens of serving a small model on a rented GPU versus calling an API. List your assumptions.
note: 3 | At what volume, if any, does self-hosting win?

## Week 11 review: finish and compare
do: 25 | Finish the LoRA fine-tune and evaluate it on the same 20 transactions as the API and local models.
note: 5 | Write a one-paragraph prompt vs RAG vs fine-tune decision rule you would defend in an interview.
