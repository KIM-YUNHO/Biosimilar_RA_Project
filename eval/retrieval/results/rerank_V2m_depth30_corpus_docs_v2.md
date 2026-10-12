| query | mode | k | ranking | recall | ctx_recall | full (ctx) | ctx_tokens (median) |
|---|---|---|---|---|---|---|---|
| ko | routed | 5 | hybrid | 35.2% | 42.6% | 3/22 | 6331 |
| ko | routed | 5 | hybrid@30+rerank | 42.6% | 53.7% | 7/22 | 6510 |
| ko | routed | 10 | hybrid | 48.1% | 55.6% | 7/22 | 8120 |
| ko | routed | 10 | hybrid@30+rerank | 53.7% | 61.1% | 11/22 | 9869 |
| ko | routed_sub | 5 | hybrid | 35.2% | 44.4% | 5/22 | 6189 |
| ko | routed_sub | 5 | hybrid@30+rerank | 40.7% | 53.7% | 7/22 | 6510 |
| ko | routed_sub | 10 | hybrid | 44.4% | 55.6% | 9/22 | 8120 |
| ko | routed_sub | 10 | hybrid@30+rerank | 51.9% | 61.1% | 10/22 | 9869 |
| ko+gl | routed | 5 | hybrid | 37.0% | 44.4% | 5/22 | 6237 |
| ko+gl | routed | 5 | hybrid@30+rerank | 46.3% | 59.3% | 9/22 | 7175 |
| ko+gl | routed | 10 | hybrid | 46.3% | 61.1% | 10/22 | 9649 |
| ko+gl | routed | 10 | hybrid@30+rerank | 59.3% | 68.5% | 11/22 | 9570 |
| ko+gl | routed_sub | 5 | hybrid | 38.9% | 48.1% | 5/22 | 6237 |
| ko+gl | routed_sub | 5 | hybrid@30+rerank | 37.0% | 51.9% | 8/22 | 7175 |
| ko+gl | routed_sub | 10 | hybrid | 46.3% | 61.1% | 10/22 | 9649 |
| ko+gl | routed_sub | 10 | hybrid@30+rerank | 55.6% | 64.8% | 12/22 | 9570 |

re-ranker: 1272 pairs scored in 912 s (0.72 s/pair on this CPU; cache holds 5129 pairs)
