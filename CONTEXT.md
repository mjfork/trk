# Agent-first issue tracker

A single-machine tracker for agentic work, organised as a hierarchy of programs, projects and work items, that meshes with herdr Spaces and feeds a status web UI.

## Language

### Items and hierarchy

**Item**:
Any tracked thing in the hierarchy; every item has exactly one kind and at most one parent.
_Avoid_: Issue, bead, node

**Kind**:
The level of an item: program, project or work item, ranked in that order. A child's kind never ranks above its parent's.
_Avoid_: Type, level

**Program**:
An item that groups related projects toward one delivered outcome, stated as its destination; may contain programs, projects and work items. It closes when its destination holds and every child is closed.
_Avoid_: Initiative, epic

**Project**:
An item that maps onto one spec and is delivered through many tickets; may contain projects and work items.
_Avoid_: Feature, epic

**Work item**:
An item small enough to be completed within a single ticket; may contain work items, such as asks or questions needed to finish it.
_Avoid_: Task, issue, subtask

**Form**:
The shape of a work item, which decides how it behaves: spec-bound ticket, self-contained ticket, decision or ask.
_Avoid_: Type, subtype, issue type

**Section**:
A named part of an item's body, addressed by a stable handle; an item's kind and form decide which sections it is expected to have.
_Avoid_: Field, block, heading

**Handoff**:
A checkpoint record a session keeps with an item so a later session can restart the work; append-only, one per checkpoint, never a section and never required.
_Avoid_: Note, checkpoint section, status update

**Map**:
The body of a program: its destination (the delivered outcome, as checkable statements), notes and fog, with decisions so far and out of scope built from its closed children; charted by wayfinder.
_Avoid_: Roadmap, plan

**Spec**:
The body of a project: problem, solution, user stories, implementation and testing decisions, acceptance criteria (end-to-end checks), out of scope.
_Avoid_: PRD, design doc

**Ticket**:
A buildable work item, sized to one fresh agent session, with acceptance criteria and blocking edges; either a spec-bound ticket or a self-contained ticket. Only tickets may be built.
_Avoid_: Story, task, JIRA issue

**Spec-bound ticket**:
A ticket that is a vertical slice of its project's spec; buildable only once that spec is accepted. Defined by where its spec lives, not by where the ticket sits.
_Avoid_: Project ticket, slice ticket

**Self-contained ticket**:
A ticket that is its own spec (current vs desired behaviour, acceptance criteria); buildable once judged self-contained, whether or not it sits under a project.
_Avoid_: Self-specified ticket, standalone ticket, orphan ticket

**Bug report**:
Everyday wording for a self-contained ticket describing broken behaviour (repro, expected vs actual, acceptance criteria); not a form of its own.
_Avoid_: Defect, incident

**Decision**:
A work item that resolves a question rather than building anything (a wayfinder ticket), typed research, prototype, grilling or task; claimed from the frontier, it does not block its parent.
_Avoid_: Spike, wayfinder ticket, investigation

**Ask**:
A leaf work item addressed to one role, posing a question or requesting an action; while open it blocks its parent, and when it closes (answered or withdrawn) the parent's owner is notified. An answer that doesn't settle it reopens the same ask, never a follow-up. Closing its parent is refused while it is open: it is answered or withdrawn on purpose first (Michael's ruling tr-29kgp, 2026-10-22: "Refuse to close until each open question is answered or withdrawn on purpose"). A person's comment on its box is a reply, not yet the answer: the ask stays open until the asker confirms the reply as the answer (`trk answer --from-comment`) or follows up.
_Avoid_: Escalation, needs-you, waiting status

**Asker**:
The actor who opened an ask; besides the parent's owner, the only one who may withdraw it.
_Avoid_: Requester, opener

**Actor**:
A registered person or named agent (such as orchestrator, worker, herdr) that can own items, act on them, or hold a role.
_Avoid_: User, account, assignee, agent role (a role is what an ask is addressed to)

**Owner**:
The actor responsible for driving an item to done, and the one notified when its asks are answered; inherited from the parent when not set, while a top-level item always has its own.
_Avoid_: Assignee, driver

**Addressee**:
The one role an ask is waiting on; only asks have one. The ask stores the role, and which actor that means is worked out whenever someone looks, so a change of owner or of role holder re-routes open asks.
_Avoid_: Assignee, recipient

**Role**:
A named responsibility an ask is addressed to instead of a person: a function role (product, engineering) held by one actor in the tracker's role table, or the relative role parent (the effective owner one level above the blocked item).
_Avoid_: Team, group, position

**Claim**:
A session's expiring lease on an item it is working right now, held on behalf of an actor and renewed by that session, or by herdr on its behalf while herdr shows the session idle, done or blocked (never working); separate from ownership and released when the lease lapses.
_Avoid_: Assignment, lock

**Top-level item**:
An item with no parent; it owns a herdr Space and manages everything beneath it.
_Avoid_: Root, space item

### Lifecycle

**Frontier**:
The items a session can claim and start now: open, unclaimed, not blocked, no open asks beneath them; never programs or asks, projects only while their spec is a draft, spec-bound tickets only once their project's spec is accepted.
_Avoid_: Backlog, queue, ready list

**Urgent**:
A flag that lifts an item to the front of the frontier ahead of the ordinary order (position under its parent); the only priority there is.
_Avoid_: Priority, P0, critical

**Status**:
Whether an item is open or closed; the same two values for every kind. Blocked and in progress are derived, never statuses.
_Avoid_: State, workflow state

**Resolution**:
Why a closed item closed: done, won't do, superseded, out of scope or duplicate.
_Avoid_: Close reason, outcome

**Stage**:
How far a ticket has advanced toward done: Framed, Spec'd, Built, Merged, Verified; it moves forward only when the next stage's gate holds (Merged alone may be skipped, by a ticket that changes no repo), and back only by `reopen-stage` or reopening a ticket closed at Verified, after which the gates above the stage it went back to count only evidence recorded since. Tickets only.
_Avoid_: Phase, status

**Gate**:
What must hold before an item may move forward: a ticket entering its next stage, a project's spec being accepted, or an item closing done.
_Avoid_: Check, guard, requirement

**Acceptance criterion**:
One checkable statement of what done means for a ticket or project, or of a program's destination, addressed by a stable handle that is never reused; a ticket is verified, and a project or program closes done, only when every one of its criteria has passing evidence.
_Avoid_: AC, requirement, test case

**Evidence**:
A recorded, never-edited proof attached to an item toward one gate, such as a PR, a test or webtester run, a merge or an actor's judgment; withdrawn only by a later retraction, and for each criterion the latest counting evidence decides.
_Avoid_: Proof, attachment, artifact

**Outcome**:
A one-sentence result written when an item closes (the answer, what shipped, or why it is out of scope); a program's decisions so far and out of scope lists are built from its children's outcomes.
_Avoid_: Gist, summary, takeaway

**Spec state**:
Whether a project's spec is a draft or accepted. Projects only.
_Avoid_: Approval, sign-off

**Change feed**:
The ordered record of every change to the store, read by consumers such as herdr from a sequence number onward; never compacted, so it can be replayed from the start.
_Avoid_: Event stream, webhook, notifications

**Epoch**:
An opaque id naming one unbroken run of the change feed. Restoring a backup starts a new epoch, because the restore withdraws rows and reuses their sequence numbers; a consumer seeing a new epoch rebuilds from the start.
_Avoid_: Generation, version

### Pages

**Page**:
The generated artifact page of one item (asks excepted), or of one person (their Needs-you page), rendered from the store and kept current by the publisher; shared with everyone on the team, never edited by hand.
_Avoid_: Board, dashboard, report

**Handle**:
The stable name of one part of a page (a section, a box, a row) that comments anchor on; never renamed, and the same thing has the same handle on every page.
_Avoid_: Anchor, element id, section id

**Waiting box**:
The part of every page that lists each open ask beneath its item; commenting on an ask's box replies to it, and the asker confirms the reply as the answer.
_Avoid_: Needs-you list, inbox

**Needs-you page**:
One person's page: every open ask waiting on them across all programs, each in the same box as on its program's page, and every item blocked on something they hold; never deleted, and says so when nothing waits.
_Avoid_: Inbox, my page, dashboard

**Ledger**:
A ticket's append-only record of the forks taken while building it (what was decided, the alternatives, why, what would have been asked), logged live by the builder and completed at review by an auditor; each row is judged by an independent reviewer.
_Avoid_: Decision log, decision (a decision is a work item)

**Verdict**:
An independent reviewer's judgment on one ledger row: sound, risky or wrong (for a reward-hacking flag, confirmed or refuted); never written by the session that built the ticket.
_Avoid_: Review comment, approval

**Blast radius**:
The red, yellow or green rating of each file a ticket changes; its worst file sets the ticket's review lane (green merges unreviewed, yellow needs independent review, red also needs a person's read).
_Avoid_: Risk level, impact

**Reward-hacking flag**:
A ledger row marking a way a check was made to pass without the code being right (regenerated expected output, a mock as proof, a weakened or skipped assertion, a silenced check); once confirmed it blocks merging until fixed and is never overruled.
_Avoid_: Cheat, gaming

### Relationships

**Edge**:
A typed link between two items, one of blocks, relates or supersedes; the parent link is not an edge.
_Avoid_: Dependency, link, relation

**Blocks**:
An edge meaning the blocked item is not ready until the blocker is closed, whatever its resolution; may cross the tree, never forms a cycle.
_Avoid_: Depends on

**Supersedes**:
An edge meaning one item replaces another; closing the replaced item moves what it blocked onto its replacement.
_Avoid_: Replaces, obsoletes

**Site guidance**:
A site's own rules for one trk skill, in a markdown file outside the release (`trk guidance <skill> --path` names it). Each skill tells the agent to read it. The order is trk's refusals and gates, then the release skill, then site guidance: site guidance may add rules and narrow them, never loosen or override them. trk only prints it; no decision reads it.
_Avoid_: Local skill, skill override, patch
