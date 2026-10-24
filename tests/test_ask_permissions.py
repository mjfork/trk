"""The ask permission matrix (spec 02, Testing decisions): for answer, withdraw and readdress, each of
the asker, the resolved addressee (human and agent), the parent's owner, an unrelated human and an
unrelated agent is either allowed or refused with rule `ask-permission`.

The fixture and the table live here from the start; the tickets that add the verbs fill them in:
ARGV gains one entry per verb, and ROWS one (verb, addressed, who, allowed) row per cell.
"""

import pytest

from gated import DEST

# The cast, by part. `addressed` picks which of the two asks a row runs against.
ASKER = "worker"  # agent; opens both asks
HUMAN_ADDRESSEE = "kim"  # holds product
AGENT_ADDRESSEE = "bot"  # holds engineering
PARENT_OWNER = "michael"  # owns the program both asks sit under
UNRELATED_HUMAN = "sam"
UNRELATED_AGENT = "stray"

ACTORS = (
    (ASKER, "agent"), (HUMAN_ADDRESSEE, "human"), (AGENT_ADDRESSEE, "agent"),
    (UNRELATED_HUMAN, "human"), (UNRELATED_AGENT, "agent"),
)

# verb -> argv after the ask id, given pytest's tmp_path (for @file bodies).
ARGV = {
    "readdress": lambda tmp_path: ("--to", "parent"),  # resolves to michael, never to the asker
    "answer": lambda tmp_path: ("--outcome", "This way", "--answer", f"@{_answer_file(tmp_path)}"),
    "withdraw": lambda tmp_path: ("--reason", "not needed"),
}


def _answer_file(tmp_path):
    path = tmp_path / "answer.md"
    path.write_text("This way.\n")
    return path

# (verb, addressed: "human" | "agent", who, allowed)
ROWS = [
    # readdress: the asker, the parent's effective owner, the resolved addressee and any human.
    *[("readdress", addressed, who, allowed)
      for addressed in ("human", "agent")
      for who, allowed in ((ASKER, True), (PARENT_OWNER, True), (UNRELATED_HUMAN, True), (UNRELATED_AGENT, False))],
    ("readdress", "human", HUMAN_ADDRESSEE, True),
    ("readdress", "agent", AGENT_ADDRESSEE, True),
    ("readdress", "human", AGENT_ADDRESSEE, False),  # an agent the ask does not resolve to
    # answer: the resolved addressee or any human; an agent only an ask that resolves to itself.
    *[("answer", addressed, who, allowed)
      for addressed in ("human", "agent")
      for who, allowed in ((ASKER, False), (PARENT_OWNER, True), (UNRELATED_HUMAN, True), (UNRELATED_AGENT, False))],
    ("answer", "human", HUMAN_ADDRESSEE, True),
    ("answer", "agent", AGENT_ADDRESSEE, True),
    ("answer", "human", AGENT_ADDRESSEE, False),
    ("answer", "agent", HUMAN_ADDRESSEE, True),  # a human proxy
    # withdraw: the asker or the parent's effective owner only.
    *[("withdraw", addressed, who, allowed)
      for addressed in ("human", "agent")
      for who, allowed in ((ASKER, True), (PARENT_OWNER, True), (HUMAN_ADDRESSEE, False), (AGENT_ADDRESSEE, False),
                           (UNRELATED_HUMAN, False), (UNRELATED_AGENT, False))],
]


@pytest.fixture
def matrix(trk, tmp_path):
    """Program P owned by michael, decision D under it, and two open asks under D from worker: one to
    product (kim, human) and one to engineering (bot, agent). Returns {"human": ask id, "agent": ask id}."""
    trk("init", "--me", PARENT_OWNER)
    for actor, kind in ACTORS:
        assert trk("actor", "add", actor, "--kind", kind).code == 0
    assert trk("roles", "set", "product", HUMAN_ADDRESSEE).code == 0
    assert trk("roles", "set", "engineering", AGENT_ADDRESSEE).code == 0
    p = trk("new", "program", "P", *DEST, "--json").json()["item"]["id"]
    d = trk("new", "work-item", "D", "--form", "decision", "--subtype", "task", "--parent", p, "--json").json()["item"]["id"]
    question = tmp_path / "question.md"
    question.write_text("Which way?\n")
    opened = {}
    for addressed, role in (("human", "product"), ("agent", "engineering")):
        result = trk("ask", d, "--to", role, f"To {role}?", "--question", f"@{question}", "--as", ASKER, "--json")
        assert result.code == 0, result.err
        opened[addressed] = result.json()["item"]["id"]
    return opened


def test_the_fixture_casts_every_part(trk, matrix):
    for addressed, addressee, kind in (("human", HUMAN_ADDRESSEE, "human"), ("agent", AGENT_ADDRESSEE, "agent")):
        item = trk("show", matrix[addressed], "--json").json()["item"]
        assert (item["asker"], item["addressee"], item["addressee_kind"]) == (ASKER, addressee, kind)
        assert item["effective_owner"] == PARENT_OWNER


@pytest.mark.parametrize("verb, addressed, who, allowed", ROWS)
def test_permission(trk, matrix, tmp_path, verb, addressed, who, allowed):
    result = trk(verb, matrix[addressed], *ARGV[verb](tmp_path), "--as", who, "--json")
    if allowed:
        assert result.code == 0, result.err
    else:
        assert (result.code, result.error()["rule"]) == (1, "ask-permission")
