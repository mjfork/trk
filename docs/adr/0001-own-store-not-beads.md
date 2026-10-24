# Our own SQLite store in Python, not a Beads wrapper

We build our own store (one machine-wide SQLite database in WAL mode with a strictly append-only event journal) behind a Python CLI installed with `uv tool install`, instead of wrapping Beads (`bd`, Dolt) or beads_rust (`br`, SQLite + JSONL, already installed here). Our invariants — a child's kind never ranks above its parent's, an open ask blocks its parent, stage gates require evidence, the change feed carries root item and addressee — would sit in a layer any plain `bd update` / `br update` bypasses, and herdr's change feed would depend on someone else's event table (in `br` it cascade-deletes with the issue; in `bd` compaction rewrites history) and their migration cadence.

## Considered options

- **Wrap `bd`**: Dolt store, ~100 commands, frequent schema migrations, history-rewriting compaction, no typed levels.
- **Wrap or fork `br`** (v0.1.7): closest fit (SQLite, `ready`, events table, parent-child deps), but `issue_type` is a free string, events are not append-only and lack root/addressee, nothing models asks, stages or evidence, and it is a young external project.
- **Rust for our own CLI**: single binary and faster startup, but herdr plugins are spawned processes in any language, so there is no shared-crate advantage; Python wins on iteration and readability.

## Consequences

- **Borrowed from Beads, as ideas only**: non-sequential ids (so a later second writer can't collide; format decided with the CLI verbs); a small fixed set of typed edges; a `ready` query that becomes the frontier; claims with an expiring lease so a dead session releases its item; the event journal, but never cascaded or compacted, and the same journal feeds `changes --since <seq>`.
- **Dropped**: JSONL-in-git as source of truth (an export/backup command is fine), deferral, Dolt.
- **One store per machine**, not per repo: items record the repos they touch; programs span repos and herdr Spaces are machine-wide.
- **The SQLite schema and the JSON change feed are the contract**, not the Python code, so a later rewrite (e.g. Rust) doesn't break herdr.
