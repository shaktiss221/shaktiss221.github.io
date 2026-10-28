---
week: 5
phase: Retrieval (RAG)
title: Embeddings and search
goal: Find the right context for the model: embeddings, chunking, vector search in Postgres and hybrid search.
---

## The patterns behind LLM products
learn: 20 | Eugene Yan, Patterns for building LLM-based systems and products (read the retrieval section) | https://eugeneyan.com/writing/llm-patterns/
note: 10 | List three places at work where a model would need private data it doesn't have. These are your retrieval use cases.

## Embeddings by hand
learn: 5 | Sentence Transformers: README | https://github.com/huggingface/sentence-transformers
do: 22 | Collect 30 to 50 paragraphs of public payments docs (refund rules, dispute policies, FAQs) into a folder. Embed them with a small open model and search them by cosine similarity in NumPy.
note: 3 | Which query returned something surprising?

## Chunking
do: 25 | Split the same docs into small (about 200 tokens) and large (about 800 tokens) chunks with some overlap. Run five queries against both and compare the top three results.
note: 5 | Write your default chunking rule and why.

## Vectors in Postgres
learn: 10 | pgvector: README | https://github.com/pgvector/pgvector
do: 17 | Run Postgres with pgvector in Docker, store your chunks and embeddings, and query with the `<=>` operator. Add an HNSW index and compare query plans with `EXPLAIN`.
note: 3 | What is the same as any other Postgres index, and what isn't?

## Hybrid search
do: 25 | Add Postgres full-text search (`tsvector`) next to the vector search and merge the two result lists with reciprocal rank fusion. Test with exact terms such as error codes, where keywords beat embeddings.
note: 5 | When did keywords win, and when did vectors win?

## Contextual retrieval
learn: 15 | Anthropic, Introducing contextual retrieval | https://www.anthropic.com/engineering/contextual-retrieval
do: 12 | Have the model write a one-sentence context for each chunk ("This is from the chargeback policy, section on timelines") and prepend it before embedding. Re-run your five queries.
note: 3 | Did retrieval improve?

## Week 5 build: policy search
do: 25 | Package a `search(query)` function that returns the top chunks using hybrid search and contextual chunks. Write 15 questions with the chunk that should answer each, and measure how often it appears in the top five (recall@5).
note: 5 | Record your recall@5. It is your baseline.
