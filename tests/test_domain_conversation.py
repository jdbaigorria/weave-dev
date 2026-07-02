"""Tests for the branching conversation domain.

Focus: history reconstruction of the active branch (the §15 question) and that
branching keeps alternate branches independent.
"""

import pytest

from weave.domain import ConversationError, ConversationTree, Interrupt, Message, Turn


def _texts(messages: list[Message]) -> list:
    return [m.content for m in messages]


class TestAppend:
    def test_empty_tree_has_no_active_leaf_and_empty_history(self):
        tree = ConversationTree()
        assert tree.active_leaf_id is None
        assert tree.history() == []
        assert tree.roots() == []
        assert tree.leaves() == []

    def test_append_linear_builds_a_chain(self):
        tree = ConversationTree()
        t1 = tree.append([Message.user("hi"), Message.assistant("hello")])
        t2 = tree.append([Message.user("bye"), Message.assistant("ciao")])

        assert t1.parent_id is None  # root
        assert t2.parent_id == t1.id
        assert tree.active_leaf_id == t2.id
        assert _texts(tree.history()) == ["hi", "hello", "bye", "ciao"]

    def test_history_reconstructs_only_the_active_path(self):
        tree = ConversationTree()
        tree.append([Message.user("a")])
        tree.append([Message.user("b")])
        tree.append([Message.user("c")])
        assert _texts(tree.history()) == ["a", "b", "c"]


class TestBranching:
    def test_branch_from_creates_a_sibling_alternate(self):
        tree = ConversationTree()
        t1 = tree.append([Message.user("q1"), Message.assistant("a1")])
        t2 = tree.append([Message.user("q2"), Message.assistant("a2")])

        # redo t2 differently: a sibling sharing t2's parent (t1)
        alt = tree.branch_from(t2.id, [Message.user("q2-edit"), Message.assistant("a2b")])

        assert alt.parent_id == t1.id
        assert tree.active_leaf_id == alt.id
        # active history follows the alternate branch, not the original t2
        assert _texts(tree.history()) == ["q1", "a1", "q2-edit", "a2b"]
        # the original branch is intact and independent
        assert _texts(tree.history(t2.id)) == ["q1", "a1", "q2", "a2"]

    def test_branch_at_root_creates_a_second_root(self):
        tree = ConversationTree()
        r1 = tree.append([Message.user("start-A")])
        r2 = tree.branch_from(r1.id, [Message.user("start-B")])

        assert r2.parent_id is None
        assert {t.id for t in tree.roots()} == {r1.id, r2.id}
        assert _texts(tree.history(r1.id)) == ["start-A"]
        assert _texts(tree.history(r2.id)) == ["start-B"]

    def test_switch_to_changes_the_active_branch(self):
        tree = ConversationTree()
        t1 = tree.append([Message.user("q1")])
        t2 = tree.append([Message.user("q2")])
        alt = tree.branch_from(t2.id, [Message.user("q2-alt")])

        tree.switch_to(t2.id)
        assert tree.active_leaf_id == t2.id
        assert _texts(tree.history()) == ["q1", "q2"]

        # appending now continues from t2 → a sibling of alt under t1
        t3 = tree.append([Message.user("q3")])
        assert t3.parent_id == t2.id
        assert _texts(tree.history()) == ["q1", "q2", "q3"]
        assert alt.parent_id == t1.id  # untouched


class TestNavigation:
    def test_children_and_leaves(self):
        tree = ConversationTree()
        t1 = tree.append([Message.user("root")])
        a = tree.append([Message.user("a")])  # child of t1
        tree.switch_to(t1.id)
        b = tree.branch_from(a.id, [Message.user("b")])  # sibling of a, child of t1

        assert {t.id for t in tree.children(t1.id)} == {a.id, b.id}
        assert {t.id for t in tree.children(None)} == {t1.id}
        assert {t.id for t in tree.leaves()} == {a.id, b.id}

    def test_path_returns_root_to_turn(self):
        tree = ConversationTree()
        t1 = tree.append([Message.user("1")])
        t2 = tree.append([Message.user("2")])
        ids = [t.id for t in tree.path(t2.id)]
        assert ids == [t1.id, t2.id]

    def test_get_returns_none_for_unknown(self):
        tree = ConversationTree()
        assert tree.get("nope") is None


class TestErrors:
    def test_branch_from_unknown_turn_raises(self):
        tree = ConversationTree()
        with pytest.raises(ConversationError, match="not in this conversation"):
            tree.branch_from("ghost", [Message.user("x")])

    def test_switch_to_unknown_turn_raises(self):
        tree = ConversationTree()
        with pytest.raises(ConversationError):
            tree.switch_to("ghost")

    def test_history_unknown_turn_raises(self):
        tree = ConversationTree()
        with pytest.raises(ConversationError):
            tree.history("ghost")


class TestInterrupts:
    def test_turn_carries_interrupts_and_reports_status(self):
        tree = ConversationTree()
        turn = tree.append(
            [Message.user("do risky thing")],
            interrupts=[Interrupt(id="i1", name="confirm", reason="needs approval")],
        )
        assert turn.is_interrupted is True
        assert turn.interrupts[0].name == "confirm"

    def test_normal_turn_is_not_interrupted(self):
        tree = ConversationTree()
        turn = tree.append([Message.user("hi")])
        assert turn.is_interrupted is False


class TestResume:
    def _interrupted_tree(self) -> tuple[ConversationTree, Turn]:
        tree = ConversationTree()
        tree.append([Message.user("q1"), Message.assistant("a1")])
        paused = tree.append(
            [Message.user("delete prod"), Message.assistant("tool-use: confirm?")],
            interrupts=[Interrupt(id="i1", name="confirm")],
        )
        return tree, paused

    def test_resume_folds_continuation_into_same_turn(self):
        tree, paused = self._interrupted_tree()
        assert tree.is_awaiting_interrupt is True

        resumed = tree.resume([Message.assistant("done, deleted")])

        # same turn, completed in place — not a new turn
        assert resumed.id == paused.id
        assert resumed.parent_id == paused.parent_id
        assert resumed.created_at == paused.created_at
        assert len(tree.turns) == 2
        assert resumed.is_interrupted is False
        assert tree.is_awaiting_interrupt is False
        assert _texts(tree.history()) == [
            "q1",
            "a1",
            "delete prod",
            "tool-use: confirm?",
            "done, deleted",
        ]

    def test_resume_can_re_interrupt(self):
        tree, _ = self._interrupted_tree()
        resumed = tree.resume(
            [Message.assistant("need a second approval")],
            interrupts=[Interrupt(id="i2", name="confirm-again")],
        )
        assert resumed.is_interrupted is True
        assert tree.is_awaiting_interrupt is True
        assert resumed.interrupts[0].id == "i2"

        final = tree.resume([Message.assistant("ok done")])
        assert final.is_interrupted is False
        assert _texts(tree.history())[-3:] == [
            "tool-use: confirm?",
            "need a second approval",
            "ok done",
        ]

    def test_append_on_awaiting_branch_raises(self):
        tree, _ = self._interrupted_tree()
        with pytest.raises(ConversationError, match="awaiting an interrupt"):
            tree.append([Message.user("next question")])

    def test_branch_from_abandons_the_interrupt(self):
        tree, paused = self._interrupted_tree()
        # escape hatch: redo the paused turn instead of resuming it
        alt = tree.branch_from(paused.id, [Message.user("never mind"), Message.assistant("ok")])
        assert tree.is_awaiting_interrupt is False
        assert alt.parent_id == paused.parent_id
        assert _texts(tree.history()) == ["q1", "a1", "never mind", "ok"]

    def test_resume_without_interrupt_raises(self):
        tree = ConversationTree()
        tree.append([Message.user("hi")])
        with pytest.raises(ConversationError, match="not awaiting an interrupt"):
            tree.resume([Message.assistant("yo")])

    def test_resume_empty_tree_raises(self):
        tree = ConversationTree()
        with pytest.raises(ConversationError, match="No active turn"):
            tree.resume([Message.assistant("yo")])

    def test_active_turn_accessor(self):
        tree = ConversationTree()
        assert tree.active_turn is None
        t = tree.append([Message.user("hi")])
        assert tree.active_turn is t


class TestImmutability:
    def test_turn_is_frozen(self):
        tree = ConversationTree()
        turn = tree.append([Message.user("hi")])
        assert isinstance(turn, Turn)
        with pytest.raises(Exception):
            turn.parent_id = "x"  # type: ignore[misc]

    def test_message_is_frozen(self):
        msg = Message.user("hi")
        with pytest.raises(Exception):
            msg.content = "y"  # type: ignore[misc]
