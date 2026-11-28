---
title: "Inside the query optimizer: how databases choose a plan, and why they get it wrong"
date: 2026-11-28
summary: How a SQL query becomes an execution plan, using PostgreSQL as the running example. Covers parsing and rewriting, access paths and join algorithms, the cost model and its constants, statistics and selectivity estimation, the independence assumption and extended statistics, join ordering from System R dynamic programming to Postgres's genetic optimizer and Cascades, what the Join Order Benchmark found about cardinality errors, prepared statements and generic plans, reading EXPLAIN ANALYZE, and a practical playbook for fixing bad plans.
tags: databases
daily: true
---

SQL is declarative: you say *what* rows you want, and the database decides *how* to get them. That decision is made by the query optimizer (Postgres calls it the planner), and it is one of the oldest and hardest pieces of a database. For one query with five joins there can be thousands of valid plans, and the fastest may be a million times faster than the slowest. The optimizer has to pick one in milliseconds, using a model of the data that is always partly wrong. When it picks well, nobody notices. When it picks badly, a query that ran in 20 ms yesterday takes 40 seconds today, nothing in the code has changed, and someone gets paged. This piece explains how optimizers work, using PostgreSQL as the running example and drawing on the System R paper, the Postgres documentation and the 2015 study "How Good Are Query Optimizers, Really?". It ends with a practical method for diagnosing and fixing bad plans.

## 1. From SQL text to a plan

A query goes through roughly four stages in Postgres (other databases use the same steps under other names):

1. **Parse.** The text becomes a parse tree. Syntax errors are caught here.
2. **Analyze and rewrite.** Names are resolved against the catalog, so `orders` becomes a specific table and `id` a specific column with a type. Views are expanded into their definitions, and rules are applied. The result is a *query tree*.
3. **Plan/optimize.** The planner considers many ways to run the query tree, estimates the cost of each, and keeps the cheapest. The output is a *plan tree* of physical operators such as Seq Scan, Index Scan, Hash Join and Sort.
4. **Execute.** The executor runs the plan tree. Each node pulls rows from its children: this is the iterator or "Volcano" model, named after Goetz Graefe's Volcano system.

Optimizers usually split stage 3 into two parts. **Logical rewrites** transform the query into an equivalent one that is usually better whatever the data looks like. Examples are pushing filters down below joins, flattening subqueries into joins, removing redundant joins and folding constants. **Cost-based search** then chooses among alternatives whose merit depends on the data: which index, which join algorithm, which join order. The first part follows rules; the second depends on estimates, and it is where the trouble starts.

## 2. The building blocks: access paths and join algorithms

A plan is assembled from a small set of operators. Knowing their cost profiles is most of what you need to read a plan.

**Scanning one table.**

- **Sequential scan** reads every page of the table in order. Sequential I/O is cheap per page, and the cost is predictable. It is the best choice when you need a large fraction of the table.
- **Index scan** walks a B-tree to the matching entries, then fetches each matching row from the table (the "heap"). Each fetch can be a random page read. It is excellent for a few rows and terrible for many.
- **Index-only scan** answers the query from the index alone when every needed column is in it and the visibility map says the pages are all-visible.
- **Bitmap scan** is the middle ground. Postgres collects matching row locations from one or more indexes into a bitmap, sorts them by page, then reads the heap in physical order. It can also combine indexes with AND and OR.

The crossover between these depends on **how many rows match**. That number is called *selectivity* (the fraction of rows that pass a filter) or *cardinality* (the number of rows), and the optimizer has to estimate it.

**Joining two inputs.**

- **Nested loop join:** for each row of the outer input, look up matches in the inner input. With an index on the inner side, each lookup is cheap, so this is ideal when the outer side is small. Without an index, it is O(N×M). It also has no start-up cost, so it returns the first row immediately, which matters with `LIMIT`.
- **Hash join:** build a hash table on the smaller input, then stream the larger input and probe it. Cost is roughly linear in both inputs, but it needs memory (`work_mem` in Postgres); if the hash table doesn't fit, it spills to disk in batches. It works only for equality joins.
- **Merge join:** sort both inputs on the join key (or read them already sorted from an index), then walk them together. It suits large inputs that are already ordered and handles range-style merge conditions.

The planner's job is to choose, for every table, an access path, and for every join, an algorithm and an order. Its main input for each choice is the estimated number of rows flowing through each node.

## 3. The cost model

To compare plans, the optimizer needs a single number for each. Postgres expresses cost in abstract units anchored to the cost of reading one page sequentially. The main knobs and their defaults are:

| Parameter | Default | Meaning |
|---|---|---|
| `seq_page_cost` | 1.0 | Reading one page sequentially |
| `random_page_cost` | 4.0 | Reading one page at a random location |
| `cpu_tuple_cost` | 0.01 | Processing one row |
| `cpu_index_tuple_cost` | 0.005 | Processing one index entry |
| `cpu_operator_cost` | 0.0025 | Evaluating one operator or function |
| `effective_cache_size` | 4GB | A hint for how much data is likely cached (no memory is allocated) |

A sequential scan of a table with 10,000 pages and 1,000,000 rows, with one filter, is costed at roughly `10,000 × 1.0 + 1,000,000 × 0.01 + 1,000,000 × 0.0025 = 22,500`. An index scan's cost depends on how many index entries and heap pages it expects to touch, with each heap page charged at `random_page_cost`, adjusted for how well the table's physical order correlates with the index order.

In `EXPLAIN` output every node shows two costs, for example `cost=0.43..8.45`. The first is the **start-up cost**, the cost before the first row can be returned; the second is the **total cost** of returning all rows. A Sort has a large start-up cost, because it has to read all its input before emitting anything, while a Seq Scan has almost none. With `LIMIT 10`, the planner prefers plans with low start-up cost.

Two things are worth knowing about the cost model:

- **The defaults assume spinning disks.** A `random_page_cost` of 4.0 reflects that random reads were far more expensive than sequential reads on hard drives, even after allowing for caching. On SSDs or when the working set is in memory, many teams lower it to around 1.1 to 1.5, which makes index scans look relatively cheaper. Change it based on measurement, not folklore.
- **Costs are not milliseconds.** They are for comparing plans for the same query on the same server. Comparing the cost of two different queries tells you little.

Perhaps surprisingly, research suggests the cost model is usually *not* the main source of bad plans. The row estimates fed into it are.

## 4. Statistics and selectivity estimation

The planner doesn't look at the data when planning. It looks at **statistics** collected by `ANALYZE`, which autovacuum runs automatically after enough rows change. For each column, Postgres stores in `pg_statistic` (readable through the `pg_stats` view):

- **`null_frac`:** the fraction of values that are NULL.
- **`n_distinct`:** the estimated number of distinct values, stored as a negative fraction of rows when it scales with table size.
- **Most common values (MCVs)** and their frequencies.
- A **histogram** of the remaining values, with buckets of equal population.
- **`correlation`:** how closely the physical row order follows the column's sort order, which matters for index-scan costs.

The level of detail is controlled by `default_statistics_target` (default 100). That means up to 100 MCVs and 100 histogram buckets per column, built from a random sample of 300 × target rows, which is 30,000 rows at the default. You can raise the target for individual columns with `ALTER TABLE ... ALTER COLUMN ... SET STATISTICS`.

With these statistics the planner estimates selectivity:

- **`status = 'shipped'`:** if `'shipped'` is an MCV, use its recorded frequency directly. If not, spread the remaining frequency evenly across the remaining distinct values.
- **`created_at > '2026-11-01'`:** find where the value falls in the histogram and interpolate within the bucket.
- **`a = $1` with an unknown parameter:** use a generic estimate, such as 1 / `n_distinct`.
- **Expressions it can't analyze**, like `lower(email) = 'x'` without statistics on that expression: fall back to hard-coded default selectivities. Defaults like these are a frequent source of bad guesses.

**Join cardinality** is estimated from the inputs' cardinalities and the distinct counts of the join columns. The basic formula assumes every value on the smaller-domain side finds matches, roughly `|R| × |S| / max(ndistinct(R.a), ndistinct(S.a))`, refined with MCV lists where available.

### The independence assumption

For a query with several filters, the planner needs the combined selectivity. Unless told otherwise, it assumes the columns are **independent** and multiplies:

`sel(city = 'Paris' AND country = 'FR') = sel(city = 'Paris') × sel(country = 'FR')`

If 2% of orders are from Paris and 2.5% from France, it estimates 0.05%. But every Paris order is from France, so the true answer is 2%, a 40x underestimate. Real data is full of such correlations: city and country, model and manufacturer, `status = 'refunded'` and `refund_amount > 0`, `tenant_id` and nearly everything in a multi-tenant table.

Postgres 10 and later can fix this with **extended statistics**:

```sql
CREATE STATISTICS orders_city_country (dependencies, ndistinct, mcv)
  ON city, country FROM orders;
ANALYZE orders;
```

`dependencies` records functional dependencies (city determines country), `ndistinct` records the number of distinct combinations, which helps `GROUP BY` estimates, and `mcv` (Postgres 12 and later) stores the most common *combinations* and their frequencies. Since Postgres 14 you can also create statistics on expressions, such as `lower(email)`.

The diagram below shows how a single correlated-filter misestimate turns into a bad join choice.

<!--diagram:misestimate-plan-1128-->

### Why errors compound

An estimate error at the bottom of a plan doesn't stay there. If the estimate for the first join's input is 40x too low, the join's estimate inherits that error, and the next join multiplies it by its own. Errors grow roughly *exponentially* with the number of joins. By the top of a ten-join plan, the planner can be off by orders of magnitude, and every decision above the first error is made with bad numbers.

## 5. Join ordering: searching the plan space

Joins are commutative and associative, so a query joining *n* tables has a huge number of possible orders. Counting only "left-deep" trees, where each join adds one base table, there are *n*! orders: 120 for 5 tables, 3.6 million for 10. Allowing "bushy" trees, where two joined results can be joined together, makes the count much larger.

### System R and dynamic programming

The classic approach comes from IBM's System R, described by Patricia Selinger and colleagues in "Access Path Selection in a Relational Database Management System" (SIGMOD 1979). It established much of what optimizers still do:

- Estimate each plan's cost from statistics and a cost formula that combines I/O and CPU.
- Build plans bottom-up with **dynamic programming**: find the best way to access each single table, then the best way to join each pair, then each set of three built from the best smaller sets, and so on. Because the cost of joining a set doesn't depend on how the set was built internally, you only need to keep the best plan for each subset.
- Keep "**interesting orders**." A plan that is slightly more expensive but produces rows sorted on a column needed later by a merge join, `ORDER BY` or `GROUP BY` may win overall, so the best plan *per sort order* is kept for each subset.
- Avoid **cross products** unless there is no alternative.

System R considered only left-deep trees. Postgres's standard planner uses the same dynamic-programming idea, also considers bushy trees, and avoids cross products where possible.

### When the search space is too large

Dynamic programming is still exponential in the number of tables. Postgres caps the work in two ways:

- **`join_collapse_limit` and `from_collapse_limit`** (default 8 each) limit how many tables the planner will freely reorder as one group. Beyond that, explicit `JOIN` syntax is partly treated as the order to follow. Setting `join_collapse_limit = 1` makes the planner use the join order exactly as written, which is a blunt but sometimes useful tool.
- **The genetic query optimizer (GEQO)** takes over when a query has at least `geqo_threshold` FROM items (default 12). Instead of an exhaustive search, it uses a genetic algorithm to explore a sample of join orders. Planning becomes faster, but plan quality is less predictable, and the result depends on a random seed (`geqo_seed`). Some practitioners raise the threshold to 16 or 18 on modern CPUs, but planning time grows geometrically beyond that.

### Cascades and transformation-based optimizers

A second family of optimizers is based on Goetz Graefe's Volcano (1993) and Cascades (1995) frameworks. Instead of building plans bottom-up, they start with the query as an expression and apply **transformation rules**: join commutativity, join associativity, pushing a filter through a join, replacing a logical join with a physical hash join. Equivalent expressions are grouped together in a structure called the *memo*, so each subproblem is optimized once. The search is top-down and can prune branches that are already more expensive than the best plan found.

SQL Server's optimizer is based on Cascades, and CockroachDB's cost-based optimizer uses a Cascades-style memo. The design makes it easier to add new rules and operators, since each rule is a self-contained transformation. Whatever the search strategy, though, the optimizer is still ranking plans using estimated cardinalities, so it inherits the same estimation problems.

## 6. What the research says: "How Good Are Query Optimizers, Really?"

In 2015 Viktor Leis and colleagues at TU Munich published a study at VLDB that is now the standard reference on this question. They built the **Join Order Benchmark (JOB)** on the IMDB movie database, chosen because it is "full of correlations and non-uniform data distributions" like real data. Synthetic benchmarks such as TPC-H generate independent, uniform data, which flatters optimizers. JOB has **113 queries with 3 to 16 joins each, 8 on average**. They tested PostgreSQL, the research system HyPer and three commercial systems they didn't name.

Their main findings:

- **Cardinality estimates are often very wrong.** For join results, the errors spanned up to about 10^8 (underestimates) and 10^4 (overestimates). Even with a single join, 16% of PostgreSQL's estimates were wrong by a factor of 10 or more; with two joins it was 32%, and with three, 52%.
- **Errors grow with the number of joins, and systems underestimate.** Every system tested tended to underestimate the size of multi-join results, and none of them detected correlations across joins.
- **Underestimates cause the worst disasters.** The queries that timed out in PostgreSQL were mostly cases where a very low estimate led it to choose a nested loop join *without* an index on the inner side. Such a plan is cheap if the outer side really has 1 row and catastrophic if it has 100,000. Disabling non-index nested loops removed the timeouts. This is why some optimizers treat such plans as risky and avoid them when the estimate is uncertain.
- **Some operators can protect themselves.** A hash join that sizes its hash table from the estimate degrades badly when the estimate is too low; one that resizes at runtime based on the rows it actually receives does not. The paper notes that PostgreSQL 9.5 added this kind of resizing.
- **Cardinality matters more than the cost model.** Tuning the cost model improved runtimes, but even a trivial cost function that just counted rows did reasonably well. Feeding the optimizer *true* cardinalities made a much bigger difference than refining cost formulas.
- **More indexes make the optimizer's job harder.** With only primary-key indexes, most queries ran close to the plan chosen with true cardinalities. When foreign-key indexes were added, more queries became much slower than the ideal plan, because more risky choices were available. The paper doesn't say indexes are bad, only that more options give the optimizer more ways to be wrong.
- **Searching the plan space thoroughly is worth it.** Exhaustive dynamic programming, including bushy trees, found better plans than heuristic or restricted searches, as long as estimates were reasonable.

The practical lesson is the one to remember: **when a plan is bad, look first at the row estimates, not at the cost constants.**

## 7. Plans that change over time: caching and prepared statements

Planning costs CPU, so databases try to reuse plans. That introduces another way to get a bad plan.

In Postgres, a **prepared statement** (an explicit `PREPARE`, or the extended query protocol most drivers use) can run with either:

- a **custom plan**, made for the actual parameter values each time, or
- a **generic plan**, made once without knowing the values and reused.

By default (`plan_cache_mode = auto`), the first five executions use custom plans. From the sixth, Postgres builds a generic plan and compares its *estimated* cost with the average estimated cost of the custom plans so far, after adding a charge for planning to the custom plans. If the generic plan looks no more expensive, it is used from then on.

This works well for queries whose best plan doesn't depend on the values. It goes wrong with **skewed data**. Imagine `WHERE tenant_id = $1`, where one tenant has 40% of the rows and most have a few hundred. If the first five executions are for small tenants, the generic plan (which assumes an average tenant) looks fine and gets locked in, then runs badly for the big tenant. The symptom is a query that is fast in `psql`, where you type literal values and get a custom plan, but slow from the application. The comparison is based on estimates, never on measured runtimes.

Postgres 12 added `plan_cache_mode`, which you can set to `force_custom_plan` for the session, role or transaction to avoid this, at the cost of planning on every execution. Note that drivers add their own layer: the JDBC driver and psycopg 3, for example, switch to server-side prepared statements only after a statement has been executed a few times (their `prepareThreshold` and `prepare_threshold` settings default to 5). So from the application's point of view, the switch to a generic plan can happen around the tenth execution.

Other databases face the same trade-off under different names: SQL Server calls it **parameter sniffing**, and Oracle added **adaptive cursor sharing** to keep several plans for different value ranges.

## 8. Reading EXPLAIN ANALYZE

`EXPLAIN` shows the plan and its estimates. `EXPLAIN (ANALYZE, BUFFERS)` runs the query and adds what actually happened. A typical line looks like this:

```text
Nested Loop  (cost=0.85..1250.40 rows=50 width=48)
             (actual time=0.05..2841.11 rows=40000 loops=1)
  Buffers: shared hit=12034 read=86512
```

What to look for:

1. **Estimated vs actual rows.** Compare `rows=` in the cost section with `rows=` in the actual section on every node. A ratio of 10x or more is worth investigating; find the *lowest* node in the tree where they diverge, because everything above it inherits the error.
2. **`loops`.** For the inner side of a nested loop, `actual rows` and `time` are *per loop*. Multiply by `loops` to get the total. A cheap-looking index scan with `loops=40000` is where the time went.
3. **Buffers.** `shared hit` pages came from Postgres's cache; `read` pages came from the OS cache or disk. Lots of reads on an index scan suggests random I/O the planner didn't expect.
4. **Rows removed by filter.** A scan that reads 1,000,000 rows to return 10 is a candidate for an index or a better one.
5. **Spills.** "Sort Method: external merge" or a Hash node with "Batches: 16" means the operation exceeded `work_mem` and used disk.

Two cautions. `ANALYZE` really runs the statement, so wrap an `UPDATE` or `DELETE` in a transaction you roll back. And timing every node adds overhead; for a very fast query, `EXPLAIN (ANALYZE, TIMING OFF)` gives row counts with less distortion. For problems that happen only in production, the `auto_explain` extension logs plans of queries slower than a threshold, and `pg_stat_statements` tells you which queries are worth looking at in the first place.

## 9. A playbook for fixing bad plans

When a query's plan goes bad, work through these steps roughly in order. Earlier steps fix the cause; later ones work around it.

1. **Confirm it's the plan.** Check `pg_stat_statements` for a jump in mean time, and compare the current plan with a known-good one if you logged it. Lock waits, a cold cache or a noisy neighbour can look like a plan problem.
2. **Refresh statistics.** Run `ANALYZE` on the tables involved. After a bulk load or a big delete, autovacuum may not have caught up, and the planner is working from an old picture of the data. Tables that grow every day (with a `created_at` column, for example) are especially prone to this: queries for "today" fall beyond the last histogram bucket. Recent Postgres versions extrapolate somewhat at the edges of indexed columns, but not perfectly.
3. **Give the planner better statistics.** Raise the statistics target on skewed columns. Add extended statistics on correlated columns, and on expressions used in filters.
4. **Make the query easier to estimate.** Rewrite predicates the planner can't see through: `WHERE date(created_at) = '2026-11-28'` becomes a range on `created_at`. Avoid wrapping indexed columns in functions or casts. Replace an `OR` across different columns with a `UNION ALL` when it lets each part use an index.
5. **Fix the physical design.** Add the index the plan actually needs, maybe a composite or partial one. Remove unused indexes that tempt the planner into risky choices.
6. **Adjust cost parameters for the hardware.** Set `random_page_cost` and `effective_cache_size` to match reality. Change these globally and deliberately, after testing, not as a per-query hack.
7. **Control plan caching.** For skewed parameters, use `plan_cache_mode = force_custom_plan` for the affected role or code path.
8. **Last resort: override the planner.** Postgres deliberately has no built-in query hints. The community view is that hints hide underlying problems and become wrong as data changes. You can still disable an operator for one transaction (`SET LOCAL enable_nestloop = off`), materialize an intermediate result with a CTE (`WITH ... AS MATERIALIZED`, since Postgres 12), or use the `pg_hint_plan` extension. Other databases offer hints or plan baselines (Oracle SQL Plan Management, SQL Server Query Store plan forcing) that pin a known-good plan. Every override is a bet that the data won't change enough to make it wrong; document it and revisit it.

A broader point follows from the research: **plan stability is often more valuable than plan optimality.** A query that is reliably 30 ms is better for a production system than one that is usually 5 ms and occasionally 30 seconds. That's why some teams prefer simpler queries with predictable plans on hot paths, keep joins on those paths to a few tables, and save the many-table analytical queries for systems built for them.

## 10. Where optimizers are heading

Optimizers are still an active research area. Three directions stand out:

- **Adaptive execution.** Instead of committing to a plan up front, the executor watches actual row counts and changes course. Oracle's adaptive plans can switch between nested loop and hash join at runtime; SQL Server has adaptive joins and memory-grant feedback; Spark's Adaptive Query Execution re-plans between stages using real statistics from completed stages.
- **Feedback from past runs.** Using observed cardinalities from earlier executions to correct future estimates. IBM DB2's LEO ("learning optimizer") explored this in the early 2000s, and several commercial systems now feed runtime information back into later planning.
- **Learned components.** Research systems have tried learned cardinality estimators and learned optimizers trained on query history (Bao, from MIT, steers an existing optimizer with hints rather than replacing it). These show promise, but production adoption is cautious, because a model that is right on average and badly wrong occasionally doesn't fix the tail-latency problem.

In the meantime, the most effective tool is still the one Selinger's team relied on in 1979: good statistics, plus engineers who know how to read a plan.

## Key takeaways

- The optimizer chooses **access paths, join algorithms and join order** by estimating the cost of alternatives. Costs are driven mostly by **estimated row counts**.
- Postgres's cost units are relative to a sequential page read; defaults like **`random_page_cost = 4.0`** assume spinning disks and are often lowered on SSDs.
- Statistics from `ANALYZE` (null fraction, distinct counts, **MCVs, histograms**, correlation) drive selectivity estimates; `default_statistics_target` is **100**.
- Multiplying selectivities assumes **independent columns**, which real data violates. **Extended statistics** (`CREATE STATISTICS`, Postgres 10+, MCV lists in 12+) fix many of these cases.
- Errors **compound across joins**. The Join Order Benchmark found errors of up to about 10^8, systematic **underestimation**, and that true cardinalities help far more than a better cost model.
- **Underestimates are the dangerous direction**: they lead to nested loops that are fine for 50 rows and disastrous for 50,000.
- Postgres searches join orders with **dynamic programming** up to **`join_collapse_limit` = 8** and switches to **GEQO at 12 FROM items**. Cascades-style optimizers (SQL Server, CockroachDB) search with transformation rules and a memo.
- **Generic plans** for prepared statements are considered from the sixth execution and can be bad for **skewed parameters**; `plan_cache_mode` (Postgres 12+) controls this.
- When reading `EXPLAIN ANALYZE`, find the **lowest node where estimated and actual rows diverge**, and remember to multiply by **loops**.
- Fix causes before symptoms: **statistics, query shape and indexes** come before cost parameters, and overrides come last.

## Discussion questions

1. Pick a slow query from your system and run `EXPLAIN (ANALYZE, BUFFERS)`. Where is the first node where estimated and actual rows differ by more than 10x, and what caused it?
2. Which pairs of columns in your main tables are strongly correlated? Would extended statistics change any plans, and how would you check before and after?
3. A query is fast in `psql` and slow from the application. List the possible explanations and how you'd tell them apart.
4. In a multi-tenant table where one tenant has 40% of the rows, what plan problems would you expect, and how would you design the schema, indexes and queries to keep plans stable?
5. Postgres refuses to add built-in hints, while Oracle and SQL Server have them. Which approach would you prefer for a production system, and why?
6. The Join Order Benchmark found that adding foreign-key indexes made some plans worse. How does that change how you think about adding indexes "just in case"?

## Sources

- P. Griffiths Selinger, M. M. Astrahan, D. D. Chamberlin, R. A. Lorie, T. G. Price, ["Access Path Selection in a Relational Database Management System"](https://dl.acm.org/doi/10.1145/582095.582099), SIGMOD 1979 (System R optimizer, dynamic programming, interesting orders)
- V. Leis, A. Gubichev, A. Mirchev, P. Boncz, A. Kemper, T. Neumann, ["How Good Are Query Optimizers, Really?"](https://15721.courses.cs.cmu.edu/spring2020/papers/22-costmodels/p204-leis.pdf), VLDB 2015, and Adrian Colyer's [summary on The Morning Paper](https://blog.acolyer.org/2017/01/30/how-good-are-query-optimizers-really/) (Join Order Benchmark)
- G. Graefe, "Volcano: An Extensible and Parallel Query Evaluation System" (1994) and "The Cascades Framework for Query Optimization" (1995)
- PostgreSQL documentation: [Query Planning configuration](https://www.postgresql.org/docs/current/runtime-config-query.html) (cost constants, collapse limits, GEQO, plan_cache_mode), "How the Planner Uses Statistics", "Using EXPLAIN", `CREATE STATISTICS`, `PREPARE`
- [postgresqlco.nf: geqo_threshold](https://postgresqlco.nf/en/doc/param/geqo_threshold) and [Cybertec: join_collapse_limit](https://v2.pgconfigurator.cybertec-postgresql.com/docs/parameters/join_collapse_limit)
- Christophe Pettus, ["The sixth execution"](https://thebuild.com/blog/the-sixth-execution/) (custom vs generic plan decision, driver prepare thresholds)
- R. Marcus et al., "Bao: Making Learned Query Optimization Practical", SIGMOD 2021; M. Stillger et al., "LEO: DB2's LEarning Optimizer", VLDB 2001
- CockroachDB engineering blog, "How we built a cost-based SQL optimizer" (2018); Microsoft SQL Server documentation on parameter sensitivity and Query Store

*Note on verification: the Join Order Benchmark details and findings, the `geqo_threshold` and `join_collapse_limit` defaults, and the custom-vs-generic plan rule, plan_cache_mode and driver thresholds were read from the linked pages today. Postgres cost-constant defaults, statistics details (sample size, MCVs, extended statistics versions), System R, Volcano/Cascades, adaptive execution features and the learned-optimizer papers come from documentation and literature as I know them and weren't re-fetched. The orders/city example in the diagram is illustrative.*
