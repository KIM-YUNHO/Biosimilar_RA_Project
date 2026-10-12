| query | mode | k | ranking | recall | ctx_recall | full (ctx) | ctx_tokens (median) |
|---|---|---|---|---|---|---|---|
| ko | routed | 5 | hybrid | 34.1% | 43.9% | 3/18 | 4942 |
| ko | routed | 5 | hybrid@30+rerank | 53.7% | 63.4% | 7/18 | 5595 |
| ko | routed | 10 | hybrid | 51.2% | 63.4% | 9/18 | 7206 |
| ko | routed | 10 | hybrid@30+rerank | 70.7% | 73.2% | 11/18 | 7994 |
| ko | routed_sub | 5 | hybrid | 36.6% | 46.3% | 3/18 | 4942 |
| ko | routed_sub | 5 | hybrid@30+rerank | 51.2% | 63.4% | 7/18 | 5595 |
| ko | routed_sub | 10 | hybrid | 53.7% | 68.3% | 10/18 | 7206 |
| ko | routed_sub | 10 | hybrid@30+rerank | 70.7% | 75.6% | 11/18 | 7994 |
| ko+gl | routed | 5 | hybrid | 48.8% | 58.5% | 6/18 | 5149 |
| ko+gl | routed | 5 | hybrid@30+rerank | 56.1% | 70.7% | 9/18 | 6155 |
| ko+gl | routed | 10 | hybrid | 63.4% | 70.7% | 9/18 | 7116 |
| ko+gl | routed | 10 | hybrid@30+rerank | 73.2% | 78.0% | 12/18 | 9584 |
| ko+gl | routed_sub | 5 | hybrid | 46.3% | 58.5% | 7/18 | 4604 |
| ko+gl | routed_sub | 5 | hybrid@30+rerank | 53.7% | 70.7% | 10/18 | 5625 |
| ko+gl | routed_sub | 10 | hybrid | 63.4% | 70.7% | 9/18 | 7116 |
| ko+gl | routed_sub | 10 | hybrid@30+rerank | 73.2% | 78.0% | 12/18 | 9584 |

re-ranker: 2532 pairs scored in 2249 s (0.89 s/pair on this CPU; cache holds 2532 pairs)
