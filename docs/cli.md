# trk command reference

This file is generated from `trk <verb> --help`. Do not edit it. Regenerate it with
`PYTHONPATH=src python scripts/gen_cli_docs.py` after a verb changes; a test fails until you do.

Every verb accepts `--as ACTOR` (who is writing), `--session ID` and `--json` (machine-readable output).
Exit codes: 0 ok, 1 refused by a rule, 2 usage error, 3 not found, 4 conflict.
For what the verbs mean, read [README.md](../README.md) and [CONTEXT.md](../CONTEXT.md).

## trk init

```text
usage: trk init [-h] [--as ACTOR] [--session SESSION] [--json] --me ACTOR
                [--display DISPLAY] [--prefix PREFIX]

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --me ACTOR         the default human actor
  --display DISPLAY  display name for --me
  --prefix PREFIX    id prefix (default tr)
```

## trk new

```text
usage: trk new [-h] [--as ACTOR] [--session SESSION] [--json]
               [--form {spec-bound,self-contained,decision,ask}]
               [--subtype {research,prototype,grilling,task}] [--parent ID]
               [--first | --last | --before ID | --after ID] [--owner ACTOR]
               [--client NAME] [--urgent] [--jira URL] [--repo R]
               [--section HANDLE=@FILE|HANDLE=-] [--criterion TEXT]
               {program,project,work-item} title

positional arguments:
  {program,project,work-item}
  title

options:
  -h, --help            show this help message and exit
  --as ACTOR            writing actor
  --session SESSION     writing session id
  --json                print JSON
  --form {spec-bound,self-contained,decision,ask}
                        work items: the form
  --subtype {research,prototype,grilling,task}
                        decisions: the subtype
  --parent ID           parent item (default: top level)
  --first               first among its siblings
  --last                last among its siblings (default)
  --before ID           just before sibling ID
  --after ID            just after sibling ID
  --owner ACTOR         the actor driving it (default: inherited)
  --client NAME         the client it belongs to (children inherit it)
  --urgent              lift to the front of the frontier
  --jira URL            JIRA issue URL
  --repo R              a repo it touches (repeatable)
  --section HANDLE=@FILE|HANDLE=-
                        a section body from a file or stdin (repeatable)
  --criterion TEXT      an acceptance criterion or destination statement, in
                        order (repeatable)
```

## trk ask

```text
usage: trk ask [-h] [--as ACTOR] [--session SESSION] [--json] --to ROLE
               [--question @FILE|-] [--urgent] [--option LABEL]
               [--recommend N] [--ledger N] [--first | --last | --before ID |
               --after ID]
               ID title

positional arguments:
  ID                  the item that waits on the answer
  title

options:
  -h, --help          show this help message and exit
  --as ACTOR          writing actor
  --session SESSION   writing session id
  --json              print JSON
  --to ROLE           a function role (see `trk roles`) or parent
  --question @FILE|-  the question's body, from a file or stdin (required)
  --urgent            multiply its urgency score by 10 in the waiting list
  --option LABEL      a choice the answerer can pick with `trk answer --option
                      N`, numbered from 1 in the order given (repeatable);
                      stored in the ask's options section
  --recommend N       the number of the --option to recommend
  --ledger N          tie it to ledger row ledger-N of its parent ticket
  --first             first among its siblings
  --last              last among its siblings (default)
  --before ID         just before sibling ID
  --after ID          just after sibling ID
```

## trk readdress

```text
usage: trk readdress [-h] [--as ACTOR] [--session SESSION] [--json] --to ROLE
                     ASK

positional arguments:
  ASK

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --to ROLE          a function role (see `trk roles`) or parent
```

## trk waiting

```text
usage: trk waiting [-h] [--as ACTOR] [--session SESSION] [--json]
                   [--me ACTOR | --all] [--root ID] [--client NAME]

Each open ask scores held_up x cache factor x 10 if urgent, computed on read:
held_up is the open items it holds up (its parent, and what the parent blocks
through `blocks` edges); the factor rises from 1 to 10 over the first 60
minutes of waiting, while the asker's cache is warm, then drops to 1. A `trk
readdress` restarts the wait; a role, owner or move re-route does not. Ties go
to the lower id. A RED-read ask whose ticket has no complete review-brief is
held: it is left out for its addressee and listed, marked held, for its asker
and (text only) with --all; its wait starts when the brief is complete.

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --me ACTOR         asks resolving to ACTOR (default: the writing actor)
  --all              every open ask
  --root ID          only asks under this item
  --client NAME      only asks whose effective client is NAME
```

## trk answer

```text
usage: trk answer [-h] [--as ACTOR] [--session SESSION] [--json]
                  [--outcome TEXT] [--option N] [--answer @FILE|- |
                  --from-comment COMMENT]
                  id

positional arguments:
  id

options:
  -h, --help            show this help message and exit
  --as ACTOR            writing actor
  --session SESSION     writing session id
  --json                print JSON
  --outcome TEXT        the answer's gist in one line (required unless
                        --option)
  --option N            answer with option N of the ask's options section:
                        outcome "Option N: <label>", body --answer or else the
                        label
  --answer @FILE|-      the answer's body, from a file or stdin
  --from-comment COMMENT
                        confirm a reply (a page comment, or reply-N from `trk
                        reply`) as the answer
```

## trk reply

```text
usage: trk reply [-h] [--as ACTOR] [--session SESSION] [--json] --body @FILE|-
                 ASK

positional arguments:
  ASK

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --body @FILE|-     the reply's text, from a file or stdin
```

## trk withdraw

```text
usage: trk withdraw [-h] [--as ACTOR] [--session SESSION] [--json]
                    --reason TEXT
                    id

positional arguments:
  id

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --reason TEXT      why, in one line
```

## trk edit

```text
usage: trk edit [-h] [--as ACTOR] [--session SESSION] [--json] [--title TITLE]
                [--owner ACTOR | --no-owner] [--client NAME|none]
                [--archive PATH|none] [--urgent | --no-urgent] [--jira URL |
                --no-jira] [--add-repo R] [--remove-repo R]
                id

positional arguments:
  id

options:
  -h, --help           show this help message and exit
  --as ACTOR           writing actor
  --session SESSION    writing session id
  --json               print JSON
  --title TITLE
  --owner ACTOR        set its own owner
  --no-owner           clear its own owner so it inherits again
  --client NAME|none   set its own client, or `none` to inherit again
  --archive PATH|none  where its worktrees' tmp/ is archived, <tree> standing
                       for the tree's directory name (children inherit it), or
                       `none` to inherit again
  --urgent
  --no-urgent
  --jira URL
  --no-jira
  --add-repo R
  --remove-repo R
```

## trk repos

```text
usage: trk repos [-h] {absolutize} ...

positional arguments:
  {absolutize}
    absolutize  make every relative repo absolute once (resolved against
                ~/projects)

options:
  -h, --help    show this help message and exit
```

## trk repos absolutize

```text
usage: trk repos absolutize [-h] [--as ACTOR] [--session SESSION] [--json]
                            [--dry-run]

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --dry-run          print what would change and what cannot be placed
```

## trk move

```text
usage: trk move [-h] [--as ACTOR] [--session SESSION] [--json] [--parent ID |
                --top] [--first | --last | --before ID | --after ID]
                id

positional arguments:
  id

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --parent ID        the new parent (default: its parent now)
  --top              lift it to the top level
  --first            first among its siblings
  --last             last among its siblings (default)
  --before ID        just before sibling ID
  --after ID         just after sibling ID
```

## trk close

```text
usage: trk close [-h] [--as ACTOR] [--session SESSION] [--json]
                 --resolution {done,wont-do,superseded,out-of-scope,duplicate}
                 --outcome TEXT [--by ID] [--of ID]
                 id

positional arguments:
  id

options:
  -h, --help            show this help message and exit
  --as ACTOR            writing actor
  --session SESSION     writing session id
  --json                print JSON
  --resolution {done,wont-do,superseded,out-of-scope,duplicate}
  --outcome TEXT        the result in one line
  --by ID               superseded: the replacement
  --of ID               duplicate: the original
```

## trk link

```text
usage: trk link [-h] [--as ACTOR] [--session SESSION] [--json]
                A {blocks,relates,supersedes} B

positional arguments:
  A
  {blocks,relates,supersedes}
  B

options:
  -h, --help            show this help message and exit
  --as ACTOR            writing actor
  --session SESSION     writing session id
  --json                print JSON
```

## trk unlink

```text
usage: trk unlink [-h] [--as ACTOR] [--session SESSION] [--json]
                  A {blocks,relates,supersedes} B

positional arguments:
  A
  {blocks,relates,supersedes}
  B

options:
  -h, --help            show this help message and exit
  --as ACTOR            writing actor
  --session SESSION     writing session id
  --json                print JSON
```

## trk reopen

```text
usage: trk reopen [-h] [--as ACTOR] [--session SESSION] [--json]
                  [--reason TEXT] [--to-stage STAGE]
                  id

positional arguments:
  id

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --reason TEXT      asks: why the answer did not settle it; tickets closed at
                     verified: why they go back (required for both)
  --to-stage STAGE   a ticket closed at verified: the earlier stage it goes
                     back to (required)
```

## trk spec

```text
usage: trk spec [-h] {accept,draft} ...

positional arguments:
  {accept,draft}
    accept        accept the spec
    draft         put the spec back to draft

options:
  -h, --help      show this help message and exit
```

## trk spec accept

```text
usage: trk spec accept [-h] [--as ACTOR] [--session SESSION] [--json] PROJECT

positional arguments:
  PROJECT

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
```

## trk spec draft

```text
usage: trk spec draft [-h] [--as ACTOR] [--session SESSION] [--json]
                      [--reason TEXT]
                      PROJECT

positional arguments:
  PROJECT

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --reason TEXT      why the spec goes back to draft (required)
```

## trk ready

```text
usage: trk ready [-h] [--as ACTOR] [--session SESSION] [--json] [--root ID]
                 [--owner ACTOR] [--client NAME] [--limit N] [--stage STAGE]

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --root ID          only this subtree
  --owner ACTOR      only items this actor effectively owns
  --client NAME      only items whose effective client is NAME
  --limit N          at most N items
  --stage STAGE      only tickets at this stage (framed, specd, built, merged,
                     verified)
```

## trk claim

```text
usage: trk claim [-h] [--as ACTOR] [--session SESSION] [--json] [--next]
                 [--root ID] [--owner ACTOR] [--force] [--worktree PATH]
                 [--lease DUR]
                 [id]

positional arguments:
  id                 the item to claim

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --next             claim the first item `ready` returns
  --root ID          --next: only this subtree
  --owner ACTOR      --next: only items this actor effectively owns
  --force            claim a program (a root's orchestrator holding it)
  --worktree PATH    also record PATH as the item's worktree (trk worktree
                     set)
  --lease DUR        lease length (default: the store's lease_minutes): 45m,
                     2h, 1h30m

See also `trk claim renew` and `trk claim release`.
```

## trk claim renew

```text
usage: trk claim renew [-h] [--as ACTOR] [--session SESSION] [--json]
                       [--lease DUR]

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --lease DUR        renew every live claim now, whether due or not, and store
                     this length: 45m, 2h, 1h30m

Without --lease only claims with under half their lease left are renewed; the
rest are listed as not due.
```

## trk claim release

```text
usage: trk claim release [-h] [--as ACTOR] [--session SESSION] [--json] [id]

positional arguments:
  id                 only the claim on this item

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
```

## trk claims

```text
usage: trk claims [-h] [--as ACTOR] [--session SESSION] [--json] [--item ID]

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --item ID          only the claim on this item
```

## trk sweep

```text
usage: trk sweep [-h] [--as ACTOR] [--session SESSION] [--json]

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
```

## trk tree

```text
usage: trk tree [-h] [--as ACTOR] [--session SESSION] [--json] [--all] [id]

positional arguments:
  id

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --all              include closed items, each with its outcome
```

## trk actor

```text
usage: trk actor [-h] {add,edit,link} ...

positional arguments:
  {add,edit,link}
    add            register a person or agent role
    edit           change an actor's display name
    link           link an actor to a page commenter's author id (page
                   comments)

options:
  -h, --help       show this help message and exit
```

## trk actor add

```text
usage: trk actor add [-h] [--as ACTOR] [--session SESSION] [--json]
                     --kind {human,agent} [--display DISPLAY]
                     id

positional arguments:
  id                    a lowercase slug

options:
  -h, --help            show this help message and exit
  --as ACTOR            writing actor
  --session SESSION     writing session id
  --json                print JSON
  --kind {human,agent}
  --display DISPLAY     display name (default: the id)
```

## trk actor edit

```text
usage: trk actor edit [-h] [--as ACTOR] [--session SESSION] [--json]
                      --display DISPLAY
                      id

positional arguments:
  id

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --display DISPLAY
```

## trk actor link

```text
usage: trk actor link [-h] [--as ACTOR] [--session SESSION] [--json]
                      --service-user ID
                      id

positional arguments:
  id

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --service-user ID  the author id `trk pages authors` lists
```

## trk actors

```text
usage: trk actors [-h] [--as ACTOR] [--session SESSION] [--json]

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
```

## trk roles

```text
usage: trk roles [-h] [--as ACTOR] [--session SESSION] [--json]

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON

See also `trk roles set`.
```

## trk roles set

```text
usage: trk roles set [-h] [--as ACTOR] [--session SESSION] [--json] role actor

positional arguments:
  role               a lowercase slug
  actor              a registered actor

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
```

## trk config

```text
usage: trk config [-h] [--as ACTOR] [--session SESSION] [--json]

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON

See also `trk config set`.
```

## trk config set

```text
usage: trk config set [-h] [--as ACTOR] [--session SESSION] [--json] key VALUE

positional arguments:
  key                the setting: default-owner, release-verify-job, shared-
                     agent, template-parent
  VALUE              an actor; for template-parent, an item id; for release-
                     verify-job, a workflow job name

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
```

## trk worktree

```text
usage: trk worktree [-h] {set,clear} ...

positional arguments:
  {set,clear}
    set        record the item's worktree, replacing one
    clear      remove the item's worktree

options:
  -h, --help   show this help message and exit
```

## trk worktree set

```text
usage: trk worktree set [-h] [--as ACTOR] [--session SESSION] [--json]
                        --path P [--branch B]
                        id

positional arguments:
  id

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --path P           the tree's directory (resolved with realpath)
  --branch B         its branch (default: read from git)

--session is the writer's session; the tree records it too.
```

## trk worktree clear

```text
usage: trk worktree clear [-h] [--as ACTOR] [--session SESSION] [--json] id

positional arguments:
  id

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
```

## trk where

```text
usage: trk where [-h] [--as ACTOR] [--session SESSION] [--json] path

positional arguments:
  path

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
```

## trk show

```text
usage: trk show [-h] [--as ACTOR] [--session SESSION] [--json] [--no-sections]
                id

positional arguments:
  id

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --no-sections      omit section bodies
```

## trk section

```text
usage: trk section [-h] {set,rm,get} ...

positional arguments:
  {set,rm,get}
    set         write a section (stdin by default)
    rm          remove a section
    get         print a section's raw body

options:
  -h, --help    show this help message and exit
```

## trk section set

```text
usage: trk section set [-h] [--as ACTOR] [--session SESSION] [--json]
                       [--file F]
                       id handle

positional arguments:
  id
  handle

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --file F           read the body from F instead of stdin
```

## trk section rm

```text
usage: trk section rm [-h] [--as ACTOR] [--session SESSION] [--json] id handle

positional arguments:
  id
  handle

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
```

## trk section get

```text
usage: trk section get [-h] [--as ACTOR] [--session SESSION] [--json]
                       id handle

positional arguments:
  id
  handle

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
```

## trk handoff

```text
usage: trk handoff [-h] {add,show} ...

positional arguments:
  {add,show}
    add       append a handoff (stdin by default)
    show      print the latest handoff's raw body

options:
  -h, --help  show this help message and exit
```

## trk handoff add

```text
usage: trk handoff add [-h] [--as ACTOR] [--session SESSION] [--json]
                       [--file F]
                       id

positional arguments:
  id

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --file F           read the body from F instead of stdin
```

## trk handoff show

```text
usage: trk handoff show [-h] [--as ACTOR] [--session SESSION] [--json] [--all]
                        id

positional arguments:
  id

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --all              every handoff, oldest first, each under a header
```

## trk comment

```text
usage: trk comment [-h] {reply,ack} ...

positional arguments:
  {reply,ack}
    reply      reply in a routed comment's thread (the page outbox sends it)
    ack        take a routed comment off `show`'s list

options:
  -h, --help   show this help message and exit
```

## trk comment reply

```text
usage: trk comment reply [-h] [--as ACTOR] [--session SESSION] [--json]
                         --body @file|-
                         COMMENT

positional arguments:
  COMMENT            a comment id, as `trk show <item> --json` lists it

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --body @file|-     the reply's text
```

## trk comment ack

```text
usage: trk comment ack [-h] [--as ACTOR] [--session SESSION] [--json]
                       [--ask ID]
                       COMMENT

positional arguments:
  COMMENT

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --ask ID           the ask that took the point on
```

## trk criterion

```text
usage: trk criterion [-h] {add,edit,retire} ...

positional arguments:
  {add,edit,retire}
    add              add a criterion; prints its handle
    edit             new text at the next version
    retire           retire a criterion, kept struck through

options:
  -h, --help         show this help message and exit
```

## trk criterion add

```text
usage: trk criterion add [-h] [--as ACTOR] [--session SESSION] [--json]
                         [--first | --last | --before ac-N | --after ac-N]
                         id text

positional arguments:
  id
  text

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --first            first in the list
  --last             last in the list (default)
  --before ac-N      just before criterion ac-N
  --after ac-N       just after criterion ac-N
```

## trk criterion edit

```text
usage: trk criterion edit [-h] [--as ACTOR] [--session SESSION] [--json]
                          id ac-N text

positional arguments:
  id
  ac-N
  text

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
```

## trk criterion retire

```text
usage: trk criterion retire [-h] [--as ACTOR] [--session SESSION] [--json]
                            --reason TEXT
                            id ac-N

positional arguments:
  id
  ac-N

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --reason TEXT      why, in one line
```

## trk criteria

```text
usage: trk criteria [-h] [--as ACTOR] [--session SESSION] [--json] id

positional arguments:
  id

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
```

## trk advance

```text
usage: trk advance [-h] [--as ACTOR] [--session SESSION] [--json]
                   [--judgment NOTE] [--outcome TEXT]
                   id

positional arguments:
  id

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --judgment NOTE    a self-contained ticket at framed: judge it complete
                     (writes the judgment row)
  --outcome TEXT     into verified (required there): the result in one line;
                     the ticket closes done
```

## trk skip

```text
usage: trk skip [-h] [--as ACTOR] [--session SESSION] [--json] [--reason TEXT]
                id stage

positional arguments:
  id
  stage              the stage to skip: merged is the only one

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --reason TEXT      why, in one line (required)
```

## trk reopen-stage

```text
usage: trk reopen-stage [-h] [--as ACTOR] [--session SESSION] [--json]
                        [--reason TEXT]
                        id stage

positional arguments:
  id
  stage              the earlier stage it goes back to

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --reason TEXT      why, in one line (required)
```

## trk blast-radius

```text
usage: trk blast-radius [-h] [--as ACTOR] [--session SESSION] [--json]
                        [--sha S] [--base REF] [--repo PATH]
                        id

positional arguments:
  id

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --sha S            the commit to rate (default: the head sha)
  --base REF         diff against the merge-base with REF (default: a merge
                     commit's first parent, else the PR's base branch fetched
                     now, else origin/HEAD, else main, else master; an empty
                     default diff is refused, and so is a diff with files the
                     PR never touched)
  --repo PATH        the checkout to diff in (default: the ticket's repo)
```

## trk review record

```text
usage: trk review record [-h] [--as ACTOR] [--session SESSION] [--json]
                         [--ref URL] --sha S --note TEXT
                         id {approved,changes-requested}

positional arguments:
  id
  {approved,changes-requested}

options:
  -h, --help            show this help message and exit
  --as ACTOR            writing actor
  --session SESSION     writing session id
  --json                print JSON
  --ref URL             the GitHub review's URL (default: a direct approval)
  --sha S               the head sha it reviewed
  --note TEXT           what it says, in one line

Without --ref, a person's direct review: --as must be a person, --sha the
head, and the head must have a RED rating and an open RED-read ask for --as,
and the ticket's review-brief must be complete (rule no-brief). The ref stored
is direct:<actor>@<head>.
```

## trk evidence

```text
usage: trk evidence [-h] [--as ACTOR] [--session SESSION] [--json] [--gate G]
                    id

positional arguments:
  id

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --gate G           only rows naming this gate

See also `trk evidence add` and `trk evidence retract`.
```

## trk evidence add

```text
usage: trk evidence add [-h] [--as ACTOR] [--session SESSION] [--json]
                        --kind K --gate G [--criterion ac-N] [--ref R]
                        [--sha S] [--verdict {pass,fail}] [--red-first]
                        --note TEXT
                        id

positional arguments:
  id

options:
  -h, --help            show this help message and exit
  --as ACTOR            writing actor
  --session SESSION     writing session id
  --json                print JSON
  --kind K              judgment, pr, test-run, webtester-run, human-accept,
                        merge, human-review, review, plan or doc
  --gate G              the gate it is evidence for
  --criterion ac-N      the criterion it proves
  --ref R               PR URL, branch, run id, log path, URL…
  --sha S               the commit, 7 to 40 hex chars
  --verdict {pass,fail}
  --red-first           a run that failed on purpose, at a base before the
                        fix, to show the test catches the defect (test-run or
                        webtester-run, gate verified, with --criterion); it
                        counts only beside a passing run of the same
                        criterion, and never decides the criterion's status
  --note TEXT           what it shows, in one line
```

## trk evidence retract

```text
usage: trk evidence retract [-h] [--as ACTOR] [--session SESSION] [--json]
                            --note TEXT
                            id EVIDENCE-ID

positional arguments:
  id
  EVIDENCE-ID

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --note TEXT        why, in one line
```

## trk pages

```text
usage: trk pages [-h] [--json] [--dirty] [--errors]
                 {setup,flush,pull,authors,reply,tidy,tick,timer,render,republish} ...

positional arguments:
  {setup,flush,pull,authors,reply,tidy,tick,timer,render,republish}
    setup               configure publishing and check the page adapter (trk
                        stores no secret)
    flush               publish due pages
    pull                run the comment pull and the outbox
    authors             author ids seen on page comments and their linked
                        actors
    reply               settle a reply that failed for good: send it again, or
                        dismiss it
    tidy                resolve the threads of asks already closed, on one
                        page
    tick                pull, then flush (the timer's command)
    timer               the timer that runs `trk pages tick` every minute (a
                        systemd user timer; a launchd agent on macOS)
    render              render one item's page locally, without network
    republish           send an item's whole page again, ignoring the stored
                        hashes (repair)

options:
  -h, --help            show this help message and exit
  --json                print JSON
  --dirty               only pages waiting to be sent
  --errors              only pages whose last attempt failed, and replies that
                        failed for good
```

## trk pages setup

```text
usage: trk pages setup [-h] [--as ACTOR] [--session SESSION] [--json]
                       [--adapter COMMAND] --home ID [--team SLUG]

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --adapter COMMAND  the page adapter's command line, e.g. "trk-pages-folder
                     --dir PATH" (default: as set)
  --home ID          the open item the publisher's asks sit under
  --team SLUG        the team every page is shared on (default: as set,
                     tracker)
```

## trk pages flush

```text
usage: trk pages flush [-h] [--as ACTOR] [--session SESSION] [--json] [--now]
                       [--item ID] [--wait]

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --now              treat every dirty page as due, failed ones included
  --item ID          only this item's page, or needs-you:<actor> (repeatable)
  --wait             keep flushing until no page is waiting
```

## trk pages pull

```text
usage: trk pages pull [-h] [--as ACTOR] [--session SESSION] [--json]

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
```

## trk pages authors

```text
usage: trk pages authors [-h] [--as ACTOR] [--session SESSION] [--json]

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
```

## trk pages reply

```text
usage: trk pages reply [-h] [--as ACTOR] [--session SESSION] [--json]
                       [--reason TEXT]
                       {resend,dismiss} REPLY-ID

positional arguments:
  {resend,dismiss}
  REPLY-ID           as `trk pages --errors` lists it

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --reason TEXT      dismiss: why it needs no reply, in one line (required)
```

## trk pages tidy

```text
usage: trk pages tidy [-h] [--as ACTOR] [--session SESSION] [--json]
                      [--dry-run]
                      id

positional arguments:
  id                 an item id, or needs-you:<actor> for a person's Needs-you
                     page

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --dry-run          list the threads it would close and change nothing
```

## trk pages tick

```text
usage: trk pages tick [-h] [--as ACTOR] [--session SESSION] [--json]

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
```

## trk pages timer

```text
usage: trk pages timer [-h] [--as ACTOR] [--session SESSION] [--json]
                       {install,remove,status}

positional arguments:
  {install,remove,status}

options:
  -h, --help            show this help message and exit
  --as ACTOR            writing actor
  --session SESSION     writing session id
  --json                print JSON
```

## trk pages render

```text
usage: trk pages render [-h] [--as ACTOR] [--session SESSION] [--json]
                        [--out F]
                        id

positional arguments:
  id                 an item id, or needs-you:<actor> for a person's Needs-you
                     page

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --out F            write the HTML to F (default -: stdout)
```

## trk pages republish

```text
usage: trk pages republish [-h] [--as ACTOR] [--session SESSION] [--json] id

positional arguments:
  id                 an item id, or needs-you:<actor>

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
```

## trk ledger

```text
usage: trk ledger [-h] {add,list,verdict,settle,audit,withdraw,promote} ...

positional arguments:
  {add,list,verdict,settle,audit,withdraw,promote}
    add                 log a fork, contradiction or reward-hacking flag;
                        prints ledger-<n>
    list                a ticket's ledger rows with their latest verdict,
                        settlement, promotion and asks
    verdict             judge a row from an independent session
    settle              settle a judged row
    audit               record the auditor's pass at the head sha; prints the
                        evidence id; a RED ticket needs its review-brief
                        section first
    withdraw            withdraw an unjudged row with a reason; prints
                        ledger-<n>
    promote             record a row's promotion to an ADR (a person)

options:
  -h, --help            show this help message and exit
```

## trk ledger add

```text
usage: trk ledger add [-h] [--as ACTOR] [--session SESSION] [--json] --kind K
                      [--decided TEXT] [--alternative TEXT] [--why TEXT]
                      [--would-have-asked TEXT] [--conflicts-with REF]
                      [--file PATH] [--sha S] [--origin O] [--noise]
                      [--repeat-of TICKET:N] [--topic TEXT] [--refers-to N]
                      id

positional arguments:
  id

options:
  -h, --help            show this help message and exit
  --as ACTOR            writing actor
  --session SESSION     writing session id
  --json                print JSON
  --kind K              fork, contradiction or reward-hack
  --decided TEXT        what was decided, in one line
  --alternative TEXT    what else was weighed (repeatable; a fork needs one)
  --why TEXT            why, in one line
  --would-have-asked TEXT
                        the question you would have asked
  --conflicts-with REF  a contradiction's ADR path or spec:<section>
  --file PATH           a file it touches (repeatable)
  --sha S               the commit, 7 to 40 hex chars
  --origin O            builder (default) or auditor
  --noise               auditor: mark the row noise
  --repeat-of TICKET:N  auditor: the same guess in another ticket's ledger
  --topic TEXT          with --repeat-of: the missing ticket-template field
  --refers-to N         the earlier row this one corrects
```

## trk ledger list

```text
usage: trk ledger list [-h] [--as ACTOR] [--session SESSION] [--json] id

positional arguments:
  id

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
```

## trk ledger verdict

```text
usage: trk ledger verdict [-h] [--as ACTOR] [--session SESSION] [--json]
                          [--note TEXT] [--sha S]
                          id N verdict

positional arguments:
  id
  N
  verdict            sound, risky or wrong; confirmed or refuted on a reward-
                     hack flag

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --note TEXT        why, in one line (required)
  --sha S            the commit judged (default: the head sha)
```

## trk ledger settle

```text
usage: trk ledger settle [-h] [--as ACTOR] [--session SESSION] [--json]
                         [--sha S] [--ask ID] [--adr PATH] [--note TEXT]
                         id N settlement

positional arguments:
  id
  N
  settlement         fixed, deferred (files a ticket for the row), accepted or
                     overruled

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --sha S            fixed: the fix commit
  --ask ID           the answered engineering ask tied to the row (accepted,
                     overruled, and fixed on a contradiction, unless an
                     auditor settles a contradiction it raised after its own
                     sound verdict at the fix)
  --adr PATH         accepted: the ADR amended or superseded because the code
                     wins
  --note TEXT        one line
```

## trk ledger audit

```text
usage: trk ledger audit [-h] [--as ACTOR] [--session SESSION] [--json]
                        --note TEXT
                        id

positional arguments:
  id

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --note TEXT        what the audit covered, in one line
```

## trk ledger withdraw

```text
usage: trk ledger withdraw [-h] [--as ACTOR] [--session SESSION] [--json]
                           --reason TEXT
                           id N

positional arguments:
  id
  N

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --reason TEXT      why, in one line
```

## trk ledger promote

```text
usage: trk ledger promote [-h] [--as ACTOR] [--session SESSION] [--json]
                          --adr PATH
                          id N

positional arguments:
  id
  N

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --adr PATH         the ADR it became
```

## trk backup

```text
usage: trk backup [-h] [--as ACTOR] [--session SESSION] [--json] path

positional arguments:
  path               the new file; never overwritten

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
```

## trk restore

```text
usage: trk restore [-h] [--as ACTOR] [--session SESSION] [--json] path

positional arguments:
  path               a file `trk backup` wrote; read, never changed

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
```

## trk changes

```text
usage: trk changes [-h] [--as ACTOR] [--session SESSION] [--json] --since SEQ
                   [--root ID] [--limit N]

options:
  -h, --help         show this help message and exit
  --as ACTOR         writing actor
  --session SESSION  writing session id
  --json             print JSON
  --since SEQ        exclusive cursor (0: all)
  --root ID          only rows under this top-level item
  --limit N          at most N rows (default 1000)
```
