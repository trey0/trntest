# Documentation style

Touchstone: [Google's "Documentation Best Practices"](https://google.github.io/styleguide/docguide/best_practices.html).
Its core line applies directly here: **say what you mean, simply and directly.** Docs are for
future readers who need to use or modify the code, not a record of how we got here. Treat docs like
a bonsai tree, not an archive — small, current, and pruned, not a large collection in various states
of disrepair. When a doc or docstring is stale or no longer earns its length, cut it; don't leave it
"just in case."

## Docstrings define the interface, not the history

A docstring's job is to let a caller use the function/class without reading its body: what it does,
its arguments and return value, and exceptions it deliberately raises. That's it.

Out of scope for a docstring:
- **The implementation.** If understanding correct usage requires understanding the implementation,
  that's usually a sign the interface should be simplified, not that the docstring should explain
  the internals.
- **The development history.** No "this used to do X, but Y broke, so now it does Z", no dated
  findings, no "confirmed empirically on `<date>`". That belongs in a commit message, not in code
  that has to be read every time someone calls the function.
- **Sharp edges, as a default.** If a function has a surprising failure mode, the first move is to
  fix it (better default, validation, a clearer error) — not to document around it. Only document a
  sharp edge you've concluded isn't worth fixing.

If there's real material that doesn't fit — a non-obvious rationale, a comparison of approaches, a
worked example — it belongs in one of these, not stacked into the docstring:
- A **comment block** near the code it actually explains, for a future editor of this specific code —
  inside the function body if it's about implementation details, above the function if it's about the
  function as a whole (e.g. why it exists, why it's shaped the way it is).
- An **overview/tutorial doc** under `docs/`, if it's about how to use a whole area of the API rather
  than one function's contract.
- **Nowhere.** Most of the time, this is the right answer. Before relocating verbatim, ask whether
  the material would be missed if it were deleted outright. A lot of it won't be.

## No references to `docs/history.md` from anywhere else

`docs/history.md` is a narrative development log — background reading, not a reference. Nothing
outside `docs/history.md` itself (and `docs/plan.md`'s "if you're curious" pointer to it) should cite
it, and especially not "see `docs/history.md`'s dated entry" — a dated entry in a 4000+ line file is
close to unsearchable and sends a reader on a scavenger hunt for something that, per `AGENTS.md`,
was never meant to be required to understand current behavior in the first place.

If a fact from history is actually load-bearing for understanding the code today (why a default is
what it is, why an approach was rejected), state that fact directly, in your own words, where it's
needed — don't outsource it to history.md by reference.

## Voice

Write like a taciturn developer, not a chatty one: get to the point, then stop. The existing
docstrings in this repo lean heavily toward the chatty end — full justification trails, hedges,
parentheticals inside parentheticals. That voice is why they grew this long in the first place, not
just an absence of pruning. When writing or editing a docstring, comment, or doc, default to the
shortest version that's still correct and complete, and only add a sentence back if its absence would
actually mislead or cost a reader real time.

## General

- Don't duplicate documentation that already exists elsewhere in the repo; link to it once instead of
  restating it in every place it's relevant.
- Prefer deleting over hedging. A doc that's wrong or out of date is worse than no doc.
