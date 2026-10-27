---
name: trk-create-program
description: Chart a new trk program with the user, wayfinder-style: nail the problem and a checkable destination, sweep breadth-first for the open decisions, create the map with its decisions and projects, and launch the research.
disable-model-invocation: true
---

# Create a trk program

A program is a wayfinder **map** kept in trk (vocabulary: `CONTEXT.md` at the root of the trk repo): a **destination** of checkable statements, `notes`, and **fog** (`not-yet-specified`), with decisions and projects beneath it. Charting is one session's work, and it resolves no decision itself. Working through the map afterwards is a later session's job, one decision at a time.

Load the `trk` skill before any trk call; its rules (plain `trk` for the live store, `--as worker`, `--json` reads) apply throughout.

Every question to the user: one per turn, about the work (never CLI mechanics), with your recommended answer as option 1 and what it unblocks.

**Site guidance.** When you load this skill, run `trk guidance trk-create-program` once. If it prints text, follow it as part of this skill. Site guidance adds rules and may narrow these ones (a stricter limit, a site's own tool or path, a named actor). It never loosens a rule here and never overrides a trk refusal: where it conflicts, this skill and trk win, and you report the conflict to whoever started you. If it prints nothing, there is no site guidance.

## 1. Hear the work

If the invocation carries no description, ask the user what the work is and wait. Done when you have the problem, why it matters, and their picture of done in their words. Note any JIRA ticket or epic URL for `--jira`.

**Sensitivity screen.** Every trk page is visible to everyone the publisher shares it with. Keep customer names, customer models and customer data out of titles and sections; write "a customer-supplied model". If the work itself is sensitive, say so and stop.

## 2. Nail the destination

Stay at destination altitude: ask only questions that change a destination line. The usual target is a **breadth word** ("widest", "all", "regularly", "accurate") that cannot be checked: offer a measure that makes it checkable, e.g. "widest collection of models" → "every cell of an inventory derived from the reference product is covered or excluded with an accepted reason". Anything about *how* goes to fog for step 3.

Done when every breadth word in the user's description has a checkable measure, usually two or three questions. If they say you are drifting into implementation, draft immediately.

Then draft and show in full:

- **Title**: names the outcome, not the activity.
- **Destination**: three to six `--criterion` statements, each one a reviewer could later pass or fail against evidence.
- **notes**: why it matters, the reference or oracle if there is one, and the measurement stance (e.g. counts are reported, not targeted).

Get their yes on the destination before going on. The single most consequential line is the alternative to offer (e.g. "should a mismatch block a release or only report?").

## 3. Sweep for the open decisions

Now go breadth-first across the whole way from here to the destination, never deep on one thread. Walk each destination line and ask what must be known or chosen before it can be built: facts outside this directory, choices only a person can make, behaviour only a prototype shows, chores that block a discussion. Then sweep for **landmines**: the unknown unknowns (privacy, licensing, the reference itself being wrong, who has authority to accept). Look things up yourself before asking the user; ask them only what the territory cannot answer, one question per turn.

Sort each finding:

- **Decision** when the question can be stated precisely now, even if it cannot be answered yet.
- **Fog** when it cannot yet be phrased that sharply. Fog is coarser than a decision; leave it unsliced.
- **Out of scope** when it lies beyond the destination. Name it in `notes` as a scope boundary.

If the sweep finds no fog and the whole way fits one session, a program is the wrong shape: say so and ask the user how to proceed.

Done when every destination line has its decisions or fog named. Show the sorted list with the subtype of each decision before creating anything.

## 4. Create and wire

Write bodies to `tmp/<slug>/` in the current project, then:

1. `new program "<title>" --owner driver --as worker --criterion … --section notes=@… --section not-yet-specified=@…` (plus `--jira <URL>`).
2. One decision per sharp question: `research` for facts to find, `grilling` for a choice a person must make, `prototype` for a question code answers, `task` for a chore that must happen first with no answer to find. Each `question` body stands alone: the question, what a good answer contains, and the agreed starting answer if any.
3. A project for each piece of work whose shape is already clear enough to spec (one project can serve several destination lines; one line can need two projects). Give each a `question` section, its spec-writing brief: what the spec must settle and which destination lines (`ac-<n>`) it serves. Cut no tickets: they come after a spec is accepted.

   **Slice the projects vertically, the way their tickets will be sliced** (the rule and its one exception are in the `trk` skill, "Specs, projects and programs"). A project is a complete path to something a person can see working, however narrow: never a layer ("the store work", "the API"), never a phase ("build", then "test", then "document"), and never one per team. A layer or a phase cannot be demoed, so its destination lines can only be checked by reading code, and the program learns nothing until every sibling lands. If a destination line can only be reached by several layers at once, that is one project, not one per layer.
4. Second pass: `link <a> blocks <b>` wherever b needs a's answer first, decisions to projects and projects to projects.

`decisions-so-far` and `out-of-scope` are built from closed children's outcomes; leave them to trk.

Done when `trk ready --root <id> --json` shows the unblocked decisions and projects you expect.

## 5. Launch the research

For each unblocked `research` decision, dispatch a background subagent. Each one: loads the `trk` skill and the `research` skill, claims its decision (`claim <id> --as worker`), investigates against primary sources, writes its findings to `tmp/<slug>/research/<id>.md`, sets the decision's `resolution` section with the answer and the sources it rests on, and writes a handoff. It leaves the decision **open**: the user sees each answer before it closes, so closing is the first move of the next working session.

## 6. Report

Give the user the program title and id, its page link (`trk show <id> --json` → `.item.page.url`, or `trk pages --json`; null until the publisher's first run), the frontier from `trk ready --root <id>`, and which research is running, items named by title with ids. If the invoking prompt names an orchestrator, tell it with SendMessage that the program exists, including the id. Then stop.
