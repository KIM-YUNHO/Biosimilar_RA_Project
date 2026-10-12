| query | mode | k | ranking | recall | ctx_recall | full (ctx) | ctx_tokens (median) |
|---|---|---|---|---|---|---|---|
| ko | routed | 5 | hybrid | 31.5% | 40.7% | 3/22 | 6331 |
| ko | routed | 5 | hybrid@30+rerank | 40.7% | 51.9% | 6/22 | 5974 |
| ko | routed | 10 | hybrid | 44.4% | 51.9% | 6/22 | 8099 |
| ko | routed | 10 | hybrid@30+rerank | 53.7% | 61.1% | 11/22 | 9684 |
| ko | routed_sub | 5 | hybrid | 29.6% | 40.7% | 4/22 | 5779 |
| ko | routed_sub | 5 | hybrid@30+rerank | 38.9% | 50.0% | 6/22 | 5974 |
| ko | routed_sub | 10 | hybrid | 42.6% | 53.7% | 8/22 | 8099 |
| ko | routed_sub | 10 | hybrid@30+rerank | 55.6% | 64.8% | 11/22 | 9684 |
| ko+gl | routed | 5 | hybrid | 40.7% | 48.1% | 6/22 | 6219 |
| ko+gl | routed | 5 | hybrid@30+rerank | 46.3% | 57.4% | 8/22 | 5723 |
| ko+gl | routed | 10 | hybrid | 46.3% | 61.1% | 10/22 | 8509 |
| ko+gl | routed | 10 | hybrid@30+rerank | 55.6% | 64.8% | 11/22 | 9273 |
| ko+gl | routed_sub | 5 | hybrid | 40.7% | 50.0% | 6/22 | 6219 |
| ko+gl | routed_sub | 5 | hybrid@30+rerank | 38.9% | 51.9% | 8/22 | 5723 |
| ko+gl | routed_sub | 10 | hybrid | 48.1% | 63.0% | 10/22 | 8509 |
| ko+gl | routed_sub | 10 | hybrid@30+rerank | 57.4% | 66.7% | 12/22 | 9273 |

re-ranker: 94 pairs scored in 43 s (0.45 s/pair on this CPU; cache holds 5223 pairs)
