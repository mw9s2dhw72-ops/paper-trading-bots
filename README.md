# Paper-trading state

Written by the Paper trade workflow every few minutes. This is simulated GBP only.

- `state.json` — positions, stops, genomes, evolution schedule
- `trades.jsonl` — paper fills (append-only)
- `equity.json` — mark-to-market snapshots, downsampled as they age

The seed is the book as of 2026-11-04 15:19 UTC (78 paper trades). Do not force-push.
