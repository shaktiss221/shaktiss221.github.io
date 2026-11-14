---
title: "Vector search under the hood: HNSW, IVF, quantization and the recall trade-off"
date: 2026-11-14
summary: How vector databases find the nearest embeddings among millions without comparing against all of them. Covers embeddings and distance metrics, why exact search and tree indexes fail in high dimensions, recall as the core metric, IVF clustering, HNSW graphs, product/scalar/binary quantization, DiskANN for SSD-scale data, filtered search, hybrid search with BM25, updates and deletes, and how to choose between pgvector and a dedicated engine.
tags: databases, distributed systems
daily: true
---

Semantic search, recommendations, deduplication and retrieval for LLM applications all reduce to the same query: given a vector, find the k stored vectors closest to it. With a few thousand vectors you can compare against all of them. With a hundred million 1,536-dimensional vectors, that's roughly 600 GB of floats to scan per query, which no latency budget allows. Vector indexes solve this by giving up exactness: they return *approximately* the nearest neighbours, quickly, and expose knobs that trade recall for speed and memory. This reading explains how the main families of approximate nearest neighbour (ANN) index work (inverted file indexes, HNSW graphs, quantization, and SSD-based graphs like DiskANN), what goes wrong with filters and updates, and how to reason about the trade-offs when you pick and tune a system. It draws on the HNSW, product quantization, DiskANN and FAISS papers, the pgvector documentation, and the ANN-Benchmarks project.

## 1. Embeddings, distance, and the query

An **embedding** is a fixed-length vector of floats produced by a model so that similar inputs end up near each other. Text embedding models commonly produce 384 to 3,072 dimensions; OpenAI's `text-embedding-3-small` produces 1,536, for example. The model defines what "similar" means; the index only knows geometry.

Three distance measures cover almost every use:

- **Euclidean (L2) distance**: straight-line distance.
- **Inner (dot) product**: larger means more similar. Used directly by many recommendation models.
- **Cosine similarity**: the dot product of normalised vectors, so it ignores length. If you normalise vectors to unit length when you store them, cosine, inner product and L2 all produce the same ranking, which is why many systems normalise once and use the cheapest operation.

Use the metric the embedding model was trained for. Using L2 on a model meant for cosine, without normalisation, quietly degrades results without any error.

The query is **k-nearest neighbours (kNN)**: return the k vectors with the smallest distance to the query vector, usually with some filter ("only this tenant's documents", "only in stock") and sometimes combined with keyword search.

### What it costs to do exactly

Exact kNN is a scan: compute the distance to every vector, keep the best k in a heap. It's O(N·d) per query. For 1 million 768-dimensional float32 vectors that's 3 GB of data touched per query; SIMD makes each distance fast, but the memory bandwidth bound remains. Exact search is the right answer surprisingly often (small collections, or after a strong filter leaves only thousands of candidates), and it's always the ground truth you measure approximate indexes against.

## 2. Why classic indexes don't help

For low-dimensional data (maps, 2D or 3D points) there are excellent exact structures: k-d trees, R-trees, quad trees. They partition space so a query can skip whole regions. In high dimensions they stop working, a group of effects often called the **curse of dimensionality**:

- **Distances concentrate.** For many data distributions, as dimensions grow, the gap between the nearest and the farthest point shrinks relative to the distances themselves. "Close" and "far" become harder to tell apart.
- **Space partitioning can't prune.** A k-d tree splits on one dimension per level. With 768 dimensions and a few million points, the tree is only around 20 levels deep, so most dimensions are never split on, and the ball around the query intersects almost every cell. The search ends up visiting most of the data.

So the field gave up on exactness. ANN indexes accept that some true neighbours will be missed in exchange for examining a small fraction of the data.

## 3. Recall: the metric everything is tuned against

The quality measure is **recall@k**: of the true k nearest neighbours (from exact search), what fraction did the index return? Recall@10 of 0.95 means that on average 9.5 of the true top 10 were found.

Every ANN index has a three-way trade-off between:

- **Recall** (quality),
- **Query throughput or latency**, and
- **Memory and build cost**.

The standard way to compare is to plot queries per second against recall for many parameter settings, which is exactly what the **ANN-Benchmarks** project (Aumüller, Bernhardsson and Faithfull) does across algorithms and datasets. A claim like "our database does 10,000 QPS" means little without the recall level and dataset.

Two practical points:

- **Measure recall on your data.** Recall depends on the data's structure. Keep a sample of real queries, compute exact neighbours offline, and check recall whenever you change index parameters, the embedding model, or the data size.
- **Recall isn't relevance.** Perfect recall returns the true nearest vectors, but whether those are the *right documents* depends on the embedding model. A model change can matter far more than going from 95% to 99% recall. Measure both.

## 4. IVF: cluster first, then search a few clusters

The **inverted file index (IVF)** is the simplest ANN structure to understand.

**Build:** run k-means on a sample of vectors to find `nlist` centroids. Assign every vector to its nearest centroid. Each centroid now owns an "inverted list" of the vectors in its cell.

**Query:** compare the query against all centroids, pick the `nprobe` nearest, and scan only the vectors in those lists exactly.

If there are 1,000 lists and you probe 10, you scan about 1% of the data plus the centroid comparisons. The miss case is a true neighbour sitting just across a cell boundary, in a list you didn't probe. Raising `nprobe` raises recall and cost roughly linearly.

Tuning guidance tends to be rules of thumb: pgvector's documentation suggests `lists` of rows/1000 for up to 1 million rows and sqrt(rows) beyond that, with `probes` around sqrt(lists) as a starting point. pgvector's default `ivfflat.probes` is 1, which gives fast queries and often poor recall, a common surprise.

IVF's strengths are a small memory overhead (just the centroids) and a simple, fast build. Its weaknesses:

- **The clustering is fixed at build time.** If the data distribution shifts (new kinds of documents), lists become unbalanced and recall drops. That's why pgvector recommends creating IVFFlat indexes after the table has data, and periodic rebuilds help.
- **Cell boundaries hurt recall** for queries near them, so getting to very high recall needs many probes.

IVF really shines when combined with compression (IVF-PQ, section 6), which is how FAISS handles billion-scale data on GPUs.

## 5. HNSW: navigable small-world graphs

**Hierarchical Navigable Small World** graphs (Malkov and Yashunin, arXiv 2016, IEEE TPAMI 2018) are the most widely deployed ANN index today. They're used in pgvector, Lucene (and so Elasticsearch and OpenSearch), Qdrant, Weaviate, Milvus, Redis and many others.

### The idea: greedy search on a proximity graph

Connect each vector to some of its near neighbours, forming a graph. To search, start at an entry node, look at its neighbours, move to whichever is closest to the query, and repeat until no neighbour is closer. On a well-built graph this greedy walk quickly reaches the query's neighbourhood.

Two problems make a flat graph slow: the walk may need many hops to cross the dataset, and it can get stuck in a local minimum. "Navigable small world" graphs fix the first by including some long-range links, like the small-world networks where any two people are a few acquaintances apart. HNSW fixes both with a hierarchy.

### The hierarchy

Each inserted vector is given a random maximum layer, with probabilities falling exponentially (most nodes exist only on layer 0, a few reach layer 1, fewer still layer 2). The top layers are therefore sparse and their links span long distances; layer 0 contains every node with short, dense links. It's the graph equivalent of a skip list.

<!--diagram:hnsw-layers-1114-->

**Search:** start at the single entry point on the top layer. Greedily walk to the closest node on that layer, then drop to the same node on the layer below and continue. On layer 0, run a wider **beam search** keeping a candidate list of size `ef` (efSearch): expand the best unexplored candidate, add its neighbours, keep the best `ef` seen, and stop when no candidate can improve the list. Return the top k. The paper reports that the hierarchy gives logarithmic complexity scaling in practice.

**Insert:** run the same search for the new vector with a candidate list of size `efConstruction` on each layer it belongs to, then connect it to up to `M` neighbours chosen with a heuristic that prefers *diverse* neighbours (it skips a candidate that's closer to an already-chosen neighbour than to the new node). The heuristic keeps the graph navigable across clusters instead of linking only within dense regions. Neighbours' link lists are updated and pruned to stay within the limit (layer 0 typically allows 2·M links).

### The knobs

- **M**: links per node. Higher M means better recall, especially for high-dimensional data, but more memory and slower builds. pgvector's default is 16.
- **efConstruction**: beam width during build. Higher means a better graph and a slower build. pgvector's default is 64.
- **efSearch**: beam width at query time. This is the main recall/latency dial and can be changed per query. pgvector's default `hnsw.ef_search` is 40, and since a query can't return more results than ef, it also caps how many results a query can return.

### Costs

- **Memory.** The graph lives in RAM alongside the vectors. Links cost roughly M·2 neighbour IDs per node on layer 0 plus a little for upper layers; at M=16 with 4-byte IDs that's about 128 bytes per vector, small next to a 6 KB float32 vector of 1,536 dimensions. The vectors themselves dominate, which is why quantization matters.
- **Builds are slow** compared with IVF: each insert is a search. Bulk builds of tens of millions of vectors take a long time and lots of memory; giving the build more memory and parallel workers helps a lot (pgvector supports parallel HNSW builds).
- **Random memory access.** Each hop reads an unpredictable node, which is fine in RAM and terrible on disk. HNSW basically assumes the index fits in memory.
- **Deletes are awkward.** Removing a node can break paths through it. Most implementations mark deleted nodes as tombstones (still traversed but not returned) and repair or rebuild later. Heavy churn degrades recall and wastes memory until the index is vacuumed or rebuilt.

HNSW's appeal is that it gives high recall at low latency with little tuning, and supports incremental inserts without a training step.

## 6. Quantization: making vectors smaller

Since the vectors dominate memory and bandwidth, compressing them is the biggest lever. Compression makes distances approximate, so systems usually **rescore**: use compressed vectors to find a larger candidate set, then compute exact distances on the full vectors for those candidates.

### Scalar quantization

Map each float32 coordinate to an int8 (using per-dimension min/max or a learned range). That's 4× smaller with a modest recall loss, and int8 dot products are fast on modern CPUs. Half-precision (float16) halves memory with almost no loss; pgvector supports it with the `halfvec` type.

### Binary quantization

Keep only the sign of each coordinate: one bit per dimension, 32× smaller than float32, and distance becomes a Hamming distance (XOR plus popcount), which is extremely fast. Alone it loses a lot of recall, but as a first-pass filter with float rescoring it works well for some high-dimensional embedding models. It's very model-dependent, so measure.

### Product quantization (PQ)

Product quantization (Jégou, Douze and Schmid, IEEE TPAMI 2011) is the classic way to get large compression ratios while keeping decent accuracy:

1. Split each d-dimensional vector into m sub-vectors (for d=1,536 and m=96, sub-vectors of 16 dimensions).
2. For each sub-space, run k-means to learn 256 centroids (a codebook).
3. Store each vector as m one-byte codes, the ID of the nearest centroid in each sub-space. Here that's 96 bytes instead of 6,144: a 64× reduction.

At query time, **asymmetric distance computation** precomputes a table of distances from each query sub-vector to all 256 centroids in its sub-space (m × 256 entries). The approximate distance to any stored vector is then m table lookups and additions, with no floating-point math over the original vectors. That's why IVF-PQ can scan millions of compressed codes per query quickly, including on GPUs (Johnson, Douze and Jégou's FAISS paper, "Billion-scale similarity search with GPUs", 2017). Google's ScaNN (Guo et al., ICML 2020) improved on PQ for inner-product search with **anisotropic quantization**, which weights errors in the direction that matters for ranking.

### Shrinking the dimensions

Another approach is to use fewer dimensions. **Matryoshka representation learning** (Kusupati et al., NeurIPS 2022) trains embeddings so that a prefix of the vector (say the first 256 of 1,536 dimensions) is itself a useful embedding. Several commercial embedding models support truncation this way. You can search with short vectors and rescore with full ones.

## 7. Beyond RAM: DiskANN and SSD-resident graphs

A billion 768-dimensional float32 vectors are about 3 TB, which is expensive to keep in RAM. **DiskANN** (Subramanya et al., NeurIPS 2019) showed that a graph index can live mostly on SSD. Its abstract reports serving a billion-point dataset with just 64 GB of RAM plus an SSD, at over 5,000 queries per second with under 3 ms mean latency and 95%+ 1-recall@1 on SIFT1B, using a 16-core machine.

The ingredients:

- **Vamana**, a graph construction algorithm that produces a flat (not hierarchical) graph with a small diameter, so searches need few hops. Each hop is an SSD read, so fewer hops matter far more than on RAM.
- **PQ-compressed vectors in RAM** guide the search: the graph walk uses compressed distances to choose which nodes to visit.
- **Full vectors and neighbour lists stored together on SSD**, one node per aligned block, so each hop is one random read that also yields the exact vector for rescoring.
- **Beam search with parallel I/O**: issue reads for several candidates at once to hide SSD latency.

DiskANN-style indexes now appear in several systems (for example Microsoft's own services, and extensions such as pgvectorscale's StreamingDiskANN for Postgres). The general lesson is the same one database storage engines learned long ago: once data exceeds memory, design the structure around the I/O unit and the number of round trips.

A related design pushes further toward **object storage**: keep vectors and index files in S3-like storage, with caches on local SSD and memory for hot data. Latency is higher on cold queries, but cost per stored vector drops a lot, which suits large multi-tenant workloads where most tenants are idle.

## 8. Filtering: where vector search gets hard

Real queries almost always come with conditions: tenant ID, permissions, date ranges, categories. Combining a filter with ANN is one of the hardest practical problems in vector search.

- **Post-filtering:** run ANN for the top k (or a few times k), then drop results that fail the filter. Simple, but if the filter is selective (it matches 1% of rows) most of the candidates get dropped, and you return fewer than k results, or none. Increasing the candidate count helps until it becomes a scan.
- **Pre-filtering:** find the rows that match the filter first, then search only among them. If the matching set is small, exact search over it is fast and perfect. If it's large, you need an ANN index that can respect the filter.
- **In-graph filtering:** traverse the HNSW graph as usual but only *return* nodes that pass the filter. When the filter is selective, the matching nodes may be disconnected from each other in the graph, so the walk spends most of its time on non-matching nodes or gets stuck. Research systems such as **Filtered-DiskANN** (Gollapudi et al., WWW 2023), which builds the graph with label awareness, and **ACORN** (Patel et al., SIGMOD 2024), which builds a denser graph so filtered subgraphs stay connected, address this directly.

What production systems do:

- **pgvector** originally applied filters after the index scan, which led to the "fewer than k rows" problem with selective `WHERE` clauses. Version 0.8.0 (October 2024) added **iterative index scans**, which keep scanning the index until enough rows pass the filter (with limits you can configure). Postgres's planner can also choose a B-tree index on the filter column and then compute exact distances on the matching rows, which is the right plan for very selective filters. Partial indexes or partitioning per tenant are common for multi-tenant data.
- **Dedicated engines** (Qdrant, Weaviate, Milvus, Elasticsearch, Vespa and others) estimate filter selectivity and switch between a filtered graph walk and a brute-force scan over matching IDs, and some add extra graph links for frequently filtered fields.
- **Multi-tenancy** is often best handled structurally: a separate index or partition per large tenant, so the filter disappears from the search entirely.

When evaluating a system, test recall and latency with filters at realistic selectivities (50%, 5%, 0.1%), not just unfiltered benchmarks.

## 9. Hybrid search: vectors plus keywords

Embeddings are good at meaning and bad at exact tokens: product codes, error messages, names, rare terms. Keyword search with **BM25** is the opposite. Most production retrieval systems combine them:

1. Run a BM25 query and a vector query separately.
2. Merge the two ranked lists. The simplest robust method is **reciprocal rank fusion** (Cormack, Clarke and Büttcher, SIGIR 2009): each document scores the sum of 1/(k + rank) over the lists it appears in, with k=60 in the original paper. It needs no score normalisation, which matters because BM25 scores and cosine similarities aren't on the same scale.
3. Optionally **rerank** the top 50 to 100 with a cross-encoder model that reads the query and document together, which is slower but more accurate than either retriever alone.

This is why search engines that already had BM25 and filtering (Elasticsearch, OpenSearch, Vespa) became strong vector databases once they added HNSW, and why Postgres users combine pgvector with full-text search in a single SQL query.

## 10. Writes, updates and the storage engine underneath

Vector indexes are usually discussed as static structures, but production data changes.

- **Inserts** into HNSW are incremental but each is a search, so write throughput is far below a B-tree's. Batch loads are faster if you build the index after loading.
- **Updates** are delete plus insert. A new embedding model means re-embedding and re-indexing everything; plan for that as a full migration with a dual-write or shadow index, because you can't mix vectors from two models in one index.
- **Deletes** leave tombstones that keep consuming memory and slowing searches until compaction.
- **Segment-based engines** (Lucene, and through it Elasticsearch and OpenSearch, plus several vector databases) write new data into small immutable segments, each with its own HNSW graph, and merge segments in the background, much like an LSM tree. A query searches every segment and merges results, so many small segments hurt latency, and each merge rebuilds a graph, which is CPU-heavy. Force-merging read-mostly indexes into fewer segments is a common optimisation.
- **Memory pressure** is the most common production issue. If an HNSW index doesn't fit in RAM, or in Postgres's shared buffers and the OS cache, every hop becomes a disk read and latency jumps by orders of magnitude. Size memory for vectors plus graph, or use quantization or a disk-oriented index.

## 11. Choosing and sizing

A rough decision path:

- **Under about a million vectors, already on Postgres:** pgvector with HNSW is usually enough. You get transactions, joins, backups and filtering in one place. Use `halfvec` or quantization if memory is tight.
- **Tens of millions of vectors with heavy filtering or high QPS:** a dedicated engine or a search engine with vector support, or pgvector with careful partitioning, depending on your team's operational comfort.
- **Hundreds of millions to billions:** compression (PQ, binary with rescoring), disk-based indexes like DiskANN, or GPU indexes (FAISS, NVIDIA cuVS), and sharding.

Back-of-envelope sizing for HNSW in RAM: vectors × (dimensions × bytes per component + about M × 2 × 4 bytes of links) plus overhead. For 10 million 1,536-dimensional float32 vectors: 10M × (6,144 + 128) ≈ 63 GB. With `halfvec` it's about 32 GB, and with int8 about 17 GB, which changes the hardware you need.

Things that matter more than the choice of engine:

- **The embedding model and chunking strategy** usually affect result quality more than the index.
- **A recall test set** built from real queries, so you can tune `efSearch` or `nprobe` against a target (say 95% recall@10) instead of guessing.
- **Latency at your filter selectivities**, not unfiltered.
- **A re-indexing plan** for model upgrades.

## Key takeaways

- Exact kNN is a linear scan; tree indexes fail in high dimensions, so practical systems use **approximate** search and measure **recall@k** against exact results.
- **IVF** clusters vectors and scans a few clusters (`nprobe`); it's cheap in memory but sensitive to boundaries and distribution drift.
- **HNSW** builds a layered proximity graph and searches greedily from the top; `M`, `efConstruction` and `efSearch` trade recall against memory, build time and latency. It needs RAM and handles deletes poorly.
- **Quantization** (float16, int8, binary, PQ) shrinks vectors by 2× to 64×; pair it with **rescoring** on full vectors.
- **DiskANN** keeps the graph and full vectors on SSD and compressed vectors in RAM, serving a billion vectors on one machine.
- **Filters** are the hardest part: post-filtering can return too few results, and in-graph filtering struggles with selective filters. Test at realistic selectivity, and consider per-tenant partitions.
- **Hybrid search** with BM25 and reciprocal rank fusion, plus reranking, beats either retriever alone for most text search.

## Discussion questions

1. Your RAG system returns poor answers. How would you tell whether the problem is ANN recall, the embedding model, chunking, or the LLM? What test set would you build?
2. A multi-tenant SaaS has 50,000 tenants, a few with millions of documents and most with hundreds. How would you lay out the vector index, and how does the filter strategy differ for big and small tenants?
3. pgvector's default `hnsw.ef_search` is 40. What happens to a query with `LIMIT 100`, and what happens with a selective `WHERE` clause on an older pgvector version? How does iterative scanning change that?
4. You need to store 500 million 1,024-dimensional vectors. Estimate memory for float32 HNSW, then propose two designs that fit on far less RAM and say what each gives up.
5. Your company is switching embedding models. Outline a migration that avoids downtime and lets you compare result quality before cutting over.
6. When would you choose brute-force exact search over any ANN index?

## Sources

- Yu. A. Malkov, D. A. Yashunin, ["Efficient and robust approximate nearest neighbor search using Hierarchical Navigable Small World graphs"](https://arxiv.org/abs/1603.09320), arXiv 2016 / IEEE TPAMI 2018
- H. Jégou, M. Douze, C. Schmid, "Product Quantization for Nearest Neighbor Search," IEEE TPAMI 2011
- J. Johnson, M. Douze, H. Jégou, "Billion-scale similarity search with GPUs," 2017 (the FAISS paper); FAISS wiki on index selection
- S. J. Subramanya et al., ["DiskANN: Fast Accurate Billion-point Nearest Neighbor Search on a Single Node"](https://papers.nips.cc/paper/2019/hash/09853c7fb1d3f8ee67a61b6bf4a7f8e6-Abstract.html), NeurIPS 2019
- R. Guo et al., "Accelerating Large-Scale Inference with Anisotropic Vector Quantization" (ScaNN), ICML 2020
- S. Gollapudi et al., "Filtered-DiskANN: Graph Algorithms for Approximate Nearest Neighbor Search with Filters," WWW 2023; L. Patel et al., "ACORN: Performant and Predicate-Agnostic Search Over Vector Embeddings and Structured Data," SIGMOD 2024
- A. Kusupati et al., "Matryoshka Representation Learning," NeurIPS 2022
- G. Cormack, C. Clarke, S. Büttcher, "Reciprocal Rank Fusion outperforms Condorcet and individual Rank Learning Methods," SIGIR 2009
- M. Aumüller, E. Bernhardsson, A. Faithfull, "ANN-Benchmarks: A Benchmarking Tool for Approximate Nearest Neighbor Algorithms" (ann-benchmarks.com)
- [pgvector README](https://github.com/pgvector/pgvector) and [CHANGELOG](https://github.com/pgvector/pgvector/blob/master/CHANGELOG.md) (HNSW in 0.5.0, iterative index scans in 0.8.0, defaults)
- Lucene/Elasticsearch documentation on HNSW and segment merging; Qdrant, Weaviate and Milvus documentation on filtering and quantization

*Note on verification: pgvector defaults and version history, the DiskANN figures and the HNSW abstract were read from the linked pages today. The IVF tuning rules of thumb, PQ and RRF details, the ScaNN, Filtered-DiskANN, ACORN and Matryoshka descriptions, and the sizing arithmetic come from the papers and docs as I know them and weren't re-fetched; check your engine's current documentation before tuning.*
