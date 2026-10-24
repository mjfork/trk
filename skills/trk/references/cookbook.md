# trk cookbook

Copy-paste patterns. `T` stands for plain `trk` (the live
store); write it out in full when you run a command. `A` is your actor
(`worker` unless you hold a role). Every read takes `--json`.

## Look around

```bash
T tree --json                         # open top-level items and their trees
T tree <id> --all --json              # include closed children (with outcomes)
T show <id> --json                    # why_not, gate (next, missing), handoff, open_asks
T section get <id> <handle> --json
T criteria <id> --json                # criteria with evidence status
T evidence <id> --json                # each row and whether it counts
T waiting --all --json                # open asks
T actors ; T roles ; T config
T show <id> --json                    # .item.page.url is the item's published page (null until published)
T pages --json                        # every published page with its url
```

## Find and take work

```bash
T ready --root <program> --json
T ready --root <program> --stage specd --json
T claim --next --root <program> --as A --json     # {"claimed": item | null}
T claim <id> --as A --json                        # exit 4: someone else holds it
T claims --session "$TRK_SESSION" --json
T claim renew --session "$TRK_SESSION"            # only without the hooks
T claim release <id> --as A
T handoff show <id> --json                        # exit 3 when there is none
```

## Chart a program

```bash
T new program "<title>" --as A \
  --criterion "<checkable destination statement>" \
  --section notes=@tmp/notes.md --section not-yet-specified=@tmp/fog.md
T new work-item "<question title>" --form decision --subtype research \
  --parent <program> --as A --section question=@tmp/q.md
T new project "<title>" --parent <program> --as A
T link <a> blocks <b> --as A          # b waits on a
T section set <program> not-yet-specified --file tmp/fog.md --as A
```

At most one `HANDLE=-` per call (it reads stdin).

## Resolve a decision

```bash
T section set <decision> resolution --file tmp/resolution.md --as A
T close <decision> --resolution done --outcome "<one line that reads alone>" --as A
T close <id> --resolution out-of-scope --outcome "<why>" --as A
```

## Write and accept a spec

```bash
for h in problem solution user-stories implementation-decisions testing-decisions out-of-scope; do
  T section set <project> $h --file tmp/spec/$h.md --as A
done
T criterion add <project> "<end-to-end check>" --as A
# from a different session than the writer:
T evidence add <project> --kind judgment --gate accept --verdict pass \
  --note "<review summary>" --as A
T spec accept <project> --as A
```

## Cut tickets

```bash
T new work-item "<title>" --form spec-bound --parent <project> --as A \
  --section what-to-build=@tmp/t1.md --criterion "<check 1>" --criterion "<check 2>"
# after every ticket exists:
T link <t1> blocks <t2> --as A
```

Bug report (self-contained):

```bash
T new work-item "<title>" --form self-contained --parent <project-or-program> --as A \
  --section current-behaviour=@tmp/cur.md --section desired-behaviour=@tmp/want.md \
  --criterion "<check>"
T advance <id> --judgment "<why it is self-contained>" --as A
```

## Build a ticket

```bash
T ledger add <id> --kind fork --decided "…" --alternative "…" --why "…" --as A
T evidence add <id> --kind pr --gate built --ref <branch|PR URL|none> --sha <head> --note "…" --as A
T evidence add <id> --kind test-run --gate built --ref "<command>" --sha <head> --verdict pass --note "…" --as A
T advance <id> --as A                                   # -> built
T blast-radius <id> --base <branch point> --as A        # a WRITE (records the lane); never omit --as
T evidence add <id> --kind merge --gate merged --ref <branch> --sha <merge sha> --note "…" --as A
T advance <id> --as A                                   # -> merged
T skip <id> merged --reason "changes no repo" --as A    # from built, instead of merge, after pr --ref none
```

## Verify and finish

```bash
T evidence add <id> --kind test-run --gate verified --criterion ac-1 \
  --ref "<command or run>" --sha <sha containing the merge> --verdict pass --note "…" --as A
T section set <id> resolution --file tmp/resolution.md --as A
T advance <id> --outcome "<one sentence: what shipped>" --as A     # closes done
```

## Review (separate session only)

```bash
T ledger list <id> --json
T ledger add <id> --origin auditor --kind fork --decided "…" --alternative "…" --why "…" --as auditor
T ledger audit <id> --note "<what the audit covered>" --as auditor
T ledger verdict <id> <n> sound --note "…" --as auditor     # sound|risky|wrong
T ledger settle <id> <n> fixed --sha <fix sha> --as A
T ask <id> --to engineering --ledger <n> "<question>" --question @tmp/q.md --as A
```

## Ask, wait, answer

```bash
T ask <id> --to engineering "<question title>" --question @tmp/ask.md --as A
T waiting --me <actor> --json
T section get <ask> answer --json
T withdraw <ask> --reason "<why>" --as A
T answer <ask> --outcome "<gist>" --answer @tmp/answer.md --as <answerer>   # only for "this is my answer"
T reply <ask> --body - --as <person>        # a person's free text: a proposed answer, the ask stays open
T answer <ask> --from-comment reply-<n> --outcome "<gist>" --as <asker>   # confirm a reply (or a page comment id)
```

## Handoff

```bash
T handoff add <id> --file tmp/handoff.md --as A    # first line = summary
T handoff show <id> --all --json
```

## Close projects and programs

```bash
T evidence add <project> --kind judgment --gate close --criterion ac-1 --verdict pass \
  --note "<delivering items by title and id>" --as A
T close <project> --resolution done --outcome "<one sentence>" --as A
```

## Fix a mistake

```bash
T evidence retract <id> <evidence-id> --note "<why>" --as A
T criterion edit <id> ac-<n> "<text>" --as A
T criterion retire <id> ac-<n> --reason "<why>" --as A
T reopen-stage <id> <stage> --reason "<why>" --as A
T edit <id> --title "…" --urgent --as A
T move <id> --parent <new parent> --as A
```

## A fix lands in a follow-up PR

The ticket head is the latest `pr` row at the built gate, and verdicts count only at or below it. When a RISKY/WRONG row is fixed in a later PR, move the head first: the builder records the follow-up's `pr` row and a `test-run` at its sha, then settles the row `fixed` at the fix sha, and the auditor re-checks there. Never record a verdict you don't hold to make settle and the gate agree; report the mismatch instead.

```bash
T evidence add <id> pr --gate built --ref <follow-up PR url> --sha <its head sha> --as worker
T evidence add <id> test-run --gate built --ref <run> --sha <same sha> --as worker
T ledger settle <id> <n> fixed --sha <fix sha> --as worker
```

## Notes with symbols (± ( ) ' " +)

The permission guard refuses plain `trk` commands whose inline text holds those characters. Every one-line
text flag (`--note`, `--outcome`, `--decided`, `--why`, `--alternative`, `--judgment`, `--reason`) also takes
`@file` or `-` (stdin, one flag per call); the file's trailing newline is dropped, the length cap applies to the text read.

```bash
printf '%s\n' 'Flow ±0.2 % (rated) at 20 °C' > tmp/note.txt
T evidence add <id> --kind test-run --gate built --ref "<run>" --sha <head> --verdict pass --note @tmp/note.txt --as A
```

A note that really starts with `@` goes through a file too.
