| query | mode | k | ranking | recall | ctx_recall | full (ctx) | ctx_tokens (median) |
|---|---|---|---|---|---|---|---|
| ko | routed | 5 | hybrid | 37.0% | 46.3% | 4/22 | 6331 |
| ko | routed | 5 | hybrid@30+rerank | 44.4% | 57.4% | 8/22 | 6510 |
| ko | routed | 10 | hybrid | 50.0% | 57.4% | 8/22 | 8120 |
| ko | routed | 10 | hybrid@30+rerank | 57.4% | 64.8% | 12/22 | 9869 |
| ko | routed_sub | 5 | hybrid | 37.0% | 48.1% | 6/22 | 6264 |
| ko | routed_sub | 5 | hybrid@30+rerank | 40.7% | 55.6% | 7/22 | 6510 |
| ko | routed_sub | 10 | hybrid | 48.1% | 59.3% | 10/22 | 8120 |
| ko | routed_sub | 10 | hybrid@30+rerank | 53.7% | 64.8% | 11/22 | 9869 |
| ko+gl | routed | 5 | hybrid | 38.9% | 48.1% | 6/22 | 6090 |
| ko+gl | routed | 5 | hybrid@30+rerank | 50.0% | 63.0% | 10/22 | 7175 |
| ko+gl | routed | 10 | hybrid | 50.0% | 64.8% | 11/22 | 9649 |
| ko+gl | routed | 10 | hybrid@30+rerank | 61.1% | 70.4% | 12/22 | 9570 |
| ko+gl | routed_sub | 5 | hybrid | 40.7% | 51.9% | 6/22 | 6237 |
| ko+gl | routed_sub | 5 | hybrid@30+rerank | 40.7% | 55.6% | 9/22 | 6910 |
| ko+gl | routed_sub | 10 | hybrid | 50.0% | 64.8% | 11/22 | 9649 |
| ko+gl | routed_sub | 10 | hybrid@30+rerank | 59.3% | 68.5% | 13/22 | 9570 |

re-ranker: 136 pairs scored in 88 s (0.64 s/pair on this CPU; cache holds 5359 pairs)
