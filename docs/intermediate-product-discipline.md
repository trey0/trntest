# Intermediate-product access discipline

A statement of principles for how this project's generated (non-final, non-source) intermediate
files should be named, stored, and shared across code paths — not an implementation plan.

## Principles

1. **Every intermediate artifact has one well-defined identity.** Two categories, not one:
   - *Single-answer artifacts* — exactly one valid value for a given scope (an entry, or an entry +
     generator). Identified by a fixed label alone. No caller-supplied parameter should be able to
     change what such an artifact contains — if it can, two code paths can silently disagree about
     what "the" artifact for that scope actually is.
   - *Intentional-variant artifacts* — multiple valid values are meant to coexist by design (e.g. a
     hillshade rendered under several photometric-parameter combinations, kept side by side for
     comparison). Identified by label plus whatever parameters/variant actually determine content,
     baked into the identity itself.

2. **Exactly one code path writes any given label.** This should be an auditable, ideally
   cheaply-checkable fact (e.g. discoverable by inspection or a lightweight registry), not something
   that has to be re-derived by reading every caller. A function that legitimately needs to produce
   variants of a label still owns the whole family — the constraint is one *owner*, not one *file*.

3. **Storage is hierarchical, per-scope, and prunable without tracking individual files.**
   Intermediate files that aren't part of a dataset's final output live under one disposable subtree,
   with the path hierarchy itself encoding scope directly — no separate manifest of "which files
   belong to this entry" needs to be maintained in code:
   ```
   <dataset>/_tmp/<entry>/<label>               # entry-scoped (shared across generators)
   <dataset>/_tmp/<entry>/<generator>/<label>   # generator-scoped
   ```
   Pruning is then just deletion at the right level — the whole `_tmp/` subtree, one entry's own
   subtree, or one entry+generator's own subtree — and the directory a label lives under is itself
   the record of whether it's shared (category from principle 1) or generator-specific.

4. **Published artifacts are immutable, and become visible atomically.** A writer produces its
   output at a uniquely-named temporary location and only exposes it at its canonical identity via
   an atomic rename, once complete. Nothing is ever edited in place after publication — a change in
   what should be produced means a new identity (per principle 1), not a mutation of an existing one.
   Together with atomicity, this makes ordinary write/read races and write/write races safe by
   construction: a reader either doesn't see an artifact yet or sees a complete one, never a partial
   one, and two writers racing to produce the same label converge on an equivalent, still-valid
   result rather than a torn file.

5. **Each artifact has a declared access mode per task: writer, reader, or (optionally) deleter.**
   At any point, an artifact is touched by either one writer alone, any number of concurrent readers,
   or one deleter alone — never a mix. Because of principle 4, enforcing "writer completes before
   any reader starts" is mostly a performance concern (avoiding redundant regeneration), not a
   correctness one. Deletion doesn't get that same protection for free — removing a file out from
   under an active reader is a real hazard regardless of atomicity — so deletion, if ever
   implemented, is the one mode that genuinely needs "no active readers remain" before it can run.
   Deleters are not something this project currently plans to implement; this principle just defines
   what their contract would have to be if one existed.

6. **No locks.** Stale locks are a known, real source of confusing-to-diagnose failures. Rely on
   atomicity, immutability, and single-writer auditing instead of runtime mutual exclusion to keep
   concurrent access safe.

7. **Write/read/delete relationships are declared, not implicit.** Which labels a given piece of
   code writes, reads, or deletes should be a legible, checkable fact attached to that code — not
   something only recoverable by reading its implementation.
