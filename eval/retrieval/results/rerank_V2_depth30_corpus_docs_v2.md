| query | mode | k | ranking | recall | ctx_recall | full (ctx) | ctx_tokens (median) |
|---|---|---|---|---|---|---|---|
| ko | routed | 5 | hybrid | 29.6% | 37.0% | 3/22 | 5268 |
| ko | routed | 5 | hybrid@30+rerank | 40.7% | 50.0% | 4/22 | 5777 |
| ko | routed | 10 | hybrid | 38.9% | 48.1% | 5/22 | 7161 |
| ko | routed | 10 | hybrid@30+rerank | 51.9% | 59.3% | 9/22 | 8438 |
| ko | routed_sub | 5 | hybrid | 27.8% | 37.0% | 3/22 | 5268 |
| ko | routed_sub | 5 | hybrid@30+rerank | 38.9% | 48.1% | 4/22 | 5723 |
| ko | routed_sub | 10 | hybrid | 37.0% | 50.0% | 6/22 | 7324 |
| ko | routed_sub | 10 | hybrid@30+rerank | 51.9% | 61.1% | 9/22 | 8438 |
| ko+gl | routed | 5 | hybrid | 37.0% | 44.4% | 4/22 | 4845 |
| ko+gl | routed | 5 | hybrid@30+rerank | 42.6% | 55.6% | 7/22 | 5689 |
| ko+gl | routed | 10 | hybrid | 44.4% | 55.6% | 8/22 | 7488 |
| ko+gl | routed | 10 | hybrid@30+rerank | 51.9% | 59.3% | 9/22 | 8779 |
| ko+gl | routed_sub | 5 | hybrid | 37.0% | 46.3% | 4/22 | 3971 |
| ko+gl | routed_sub | 5 | hybrid@30+rerank | 35.2% | 50.0% | 8/22 | 5544 |
| ko+gl | routed_sub | 10 | hybrid | 46.3% | 57.4% | 8/22 | 7488 |
| ko+gl | routed_sub | 10 | hybrid@30+rerank | 53.7% | 61.1% | 10/22 | 8779 |

re-ranker: 1325 pairs scored in 1200 s (0.91 s/pair on this CPU; cache holds 3857 pairs)
