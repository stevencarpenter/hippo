# Claude auto-memory chunking spike results

Generated from the synthetic corpus by `mise run bench:auto-memory-spike`.
This measures deterministic FTS5 behavior; semantic/vector quality requires the later live-model benchmark.

| Strategy | Chunks | Hit@1 | MRR |
|---|---:|---:|---:|
| whole-file | 6 | 1.000 | 1.000 |
| markdown-heading | 13 | 1.000 | 1.000 |
| token-window | 12 | 1.000 | 1.000 |

## Per-query results

### whole-file

- `q-architecture`: expected `project_architecture.md`, top `project_architecture.md`, reciprocal rank 1.000
- `q-busy`: expected `debugging.md`, top `debugging.md`, reciprocal rank 1.000
- `q-release`: expected `workflow.md`, top `workflow.md`, reciprocal rank 1.000
- `q-candor`: expected `feedback_candor.md`, top `feedback_candor.md`, reciprocal rank 1.000
- `q-index`: expected `MEMORY.md`, top `MEMORY.md`, reciprocal rank 1.000

### markdown-heading

- `q-architecture`: expected `project_architecture.md`, top `project_architecture.md`, reciprocal rank 1.000
- `q-busy`: expected `debugging.md`, top `debugging.md`, reciprocal rank 1.000
- `q-release`: expected `workflow.md`, top `workflow.md`, reciprocal rank 1.000
- `q-candor`: expected `feedback_candor.md`, top `feedback_candor.md`, reciprocal rank 1.000
- `q-index`: expected `MEMORY.md`, top `MEMORY.md`, reciprocal rank 1.000

### token-window

- `q-architecture`: expected `project_architecture.md`, top `project_architecture.md`, reciprocal rank 1.000
- `q-busy`: expected `debugging.md`, top `debugging.md`, reciprocal rank 1.000
- `q-release`: expected `workflow.md`, top `workflow.md`, reciprocal rank 1.000
- `q-candor`: expected `feedback_candor.md`, top `feedback_candor.md`, reciprocal rank 1.000
- `q-index`: expected `MEMORY.md`, top `MEMORY.md`, reciprocal rank 1.000

