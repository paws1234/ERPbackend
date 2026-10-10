# Load test — the ledger-heavy paths (T-6.HARD.01)

**Recorded by**: `tools/load_test.py`. **Reproduce with**:

```
DATABASE_URL=postgresql+psycopg://… python tools/load_test.py --items 400 --postings 20000 --repeats 5 --concurrency 8
```

## The dataset the figures belong to

- items in the catalogue: **400** (each with a barcode and stock)
- postings already in the ledger: **20000**
- demand for MRP: one confirmed order
- a customer with a tier and three pricing rules the engine has to order
- concurrency: **8** workers, each with its own connection and transaction
- repeats per worker: **5**

## The figures

| Path | What it does | Runs | Min | Median | p95 | Max | Target | Within |
|---|---|---|---|---|---|---|---|---|
| `posting` | one balanced journal entry posted through T-0.CORE.01's primitive | 40 | 11 ms | 24 ms | 45 ms | 49 ms | 100 ms | yes |
| `stock_ledger_reads` | on-hand and the movement history for three items — the ledger sum | 40 | 7 ms | 21 ms | 32 ms | 40 ms | 250 ms | yes |
| `statement_generation` | trial balance, profit and loss and balance sheet for the period | 40 | 71 ms | 119 ms | 215 ms | 224 ms | 2000 ms | yes |
| `pos_checkout` | one complete sale: scan ×3, tender, complete (stock issue + posting), receipt | 40 | 301 ms | 393 ms | 440 ms | 474 ms | 2000 ms | yes |
| `mrp_run` | one net-requirements run over the horizon, against the order book and stock | 40 | 14 ms | 43 ms | 56 ms | 62 ms | 5000 ms | yes |

The **95th percentile** is what is compared with the target: a single slow transaction is what a customer experiences, and an average is how a budget stops meaning anything (the rule T-3.POS.06 set for POS latency). Only `pos_checkout`'s target is the plan's own (§6 metric 6, `pos_latency_budget`); the others are stated in `tools/load_test.py`'s `TARGETS_MS` so a miss is checkable rather than a feeling.

## Findings

None: every named path is within its target at the stated dataset and load.

The closest path to its target is `posting`: **45 ms** of a 100 ms target (45 %). Everything else has more headroom, so that is where the next dataset belongs.
