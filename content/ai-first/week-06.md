---
week: 6
phase: Retrieval (RAG)
title: Answering from your data
goal: Turn retrieval into grounded answers: citations, refusing when unsure, reranking, query rewriting and keeping the index fresh.
---

## Grounded answers with citations
do: 25 | Send the top chunks to the model with instructions to answer only from them, cite chunk IDs, and say "I don't know" when the answer isn't there. Try one question your docs can't answer.
note: 5 | Did it refuse, or did it guess?

## Reranking
do: 25 | Retrieve 20 chunks, rerank them with a cross-encoder from Sentence Transformers and keep the top five. Compare recall@5 with your week 5 baseline.
note: 5 | Was the extra latency worth it?
also: Sentence Transformers (cross-encoders) | https://github.com/huggingface/sentence-transformers

## Query rewriting
do: 25 | Have the model rewrite a messy question into two or three search queries, retrieve for each and merge the results. Test with vague questions such as "why was my money taken twice".
note: 5 | Which questions improved most?

## RAG vs a long prompt
do: 25 | For a small doc set, skip retrieval: put all the docs in a cached system prompt and ask the same questions. Compare accuracy, latency and cost with your retrieval pipeline.
note: 5 | Write down when you would choose each.

## Encoders and decoders
learn: 25 | Hugging Face LLM course: chapter 1 | https://huggingface.co/learn/llm-course/chapter1/1
note: 5 | Which kind of model did you use for embeddings, and which for answers?

## Keeping the index fresh
do: 25 | Design incremental re-indexing: when a doc changes, re-chunk and re-embed only what changed. It has the same shape as the change-data-capture pipelines you already run. Write a half-page design.
note: 5 | How would you detect stale answers in production?

## Week 6 build: policy assistant
do: 25 | Combine grounded answers, citations, reranking and query rewriting into one `ask(question)` function. Write the expected answer for each of your 15 questions and score the assistant by hand.
note: 5 | Keep the questions and answers. Next week they become an eval set.
