from __future__ import annotations

from typing import Callable

import pytest

from sanity_client import Patch, SanityClient, Transaction
from sanity_client._encoding import js_json_dumps
from sanity_client.patch import get_selection

from .conftest import Recorder, json_body


def test_selection_forms() -> None:
    assert get_selection("a") == {"id": "a"}
    assert get_selection(["a", "b"]) == {"query": "*[_id in $ids]", "params": {"ids": ["a", "b"]}}
    assert get_selection({"query": "*", "params": {"x": 1}}) == {"query": "*", "params": {"x": 1}}
    assert get_selection({"query": "*", "params": None}) == {"query": "*"}
    assert get_selection({"id": "z"}) == {"id": "z"}
    with pytest.raises(ValueError, match="Unknown selection"):
        get_selection(42)


def test_set_merges_and_unset_replaces() -> None:
    patch = Patch("a").set({"x": 1}).set({"y": 2}).unset(["p"]).unset(["q"])
    assert patch.serialize() == {"id": "a", "set": {"x": 1, "y": 2}, "unset": ["q"]}


def test_operation_order_is_first_use() -> None:
    patch = Patch("a").inc({"n": 1}).set({"t": "x"}).inc({"m": 2})
    assert js_json_dumps(patch) == '{"id":"a","inc":{"n":1,"m":2},"set":{"t":"x"}}'


def test_unset_needs_a_list_of_strings() -> None:
    with pytest.raises(TypeError):
        Patch("a").unset("title")
    with pytest.raises(TypeError):
        Patch("a").unset([1])  # type: ignore[list-item]


def test_assign_needs_a_mapping() -> None:
    with pytest.raises(TypeError, match="object of properties"):
        Patch("a").set(["x"])  # type: ignore[arg-type]


def test_insert_validation() -> None:
    with pytest.raises(ValueError, match='"before", "after", "replace"'):
        Patch("a").insert("middle", "x[0]", [1])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="selector"):
        Patch("a").insert("after", 3, [1])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="items"):
        Patch("a").insert("after", "x[-1]", "abc")


@pytest.mark.parametrize(
    ("start", "count", "expected"),
    [(1, 2, "items[1:3]"), (-2, 1, "items[-3:]"), (0, None, "items[0:-1]"), (3, -1, "items[3:-1]"), (2, 0, "items[2:2]")],
)
def test_splice_ranges(start: int, count: int, expected: str) -> None:
    patch = Patch("a").splice("items", start, count)
    assert patch.serialize()["insert"] == {"replace": expected, "items": []}


def test_serialize_is_a_copy() -> None:
    patch = Patch("a").set({"x": {"y": 1}})
    out = patch.serialize()
    out["set"]["x"]["y"] = 2
    assert patch.serialize()["set"]["x"]["y"] == 1


def test_reset_clone_and_equality() -> None:
    patch = Patch("a").set({"x": 1})
    clone = patch.clone()
    assert clone == patch
    clone.set({"y": 2})
    assert clone != patch
    assert patch.reset().serialize() == {"id": "a"}
    assert "Patch(" in repr(patch)


def test_commit_without_client_raises() -> None:
    with pytest.raises(RuntimeError, match="No `client`"):
        Patch("a").set({"x": 1}).commit()
    with pytest.raises(RuntimeError, match="No `client`"):
        Transaction().delete("a").commit()


def test_transaction_validations() -> None:
    trx = Transaction()
    with pytest.raises(TypeError):
        trx.create("not a doc")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match='"_id"'):
        trx.create_if_not_exists({"_type": "x"})
    with pytest.raises(ValueError, match="not a valid document ID"):
        trx.create_or_replace({"_id": "bad id", "_type": "x"})
    with pytest.raises(ValueError, match="not a valid document ID"):
        trx.delete("a..b")
    with pytest.raises(TypeError, match="must return the patch"):
        trx.patch("a", lambda p: None)  # type: ignore[arg-type,return-value]
    assert len(trx) == 0


def test_transaction_serialize_clone_reset() -> None:
    trx = Transaction(transaction_id="t1").create({"_type": "a"}).delete("b")
    assert trx.transaction_id == "t1"
    assert trx.serialize() == [{"create": {"_type": "a"}}, {"delete": {"id": "b"}}]
    clone = trx.clone()
    clone.delete("c")
    assert len(trx) == 2 and len(clone) == 3
    assert trx.reset().serialize() == []
    assert "transaction_id='t1'" in repr(trx)


def test_transaction_patch_by_id_with_ops() -> None:
    trx = Transaction().patch("doc", {"set": {"a": 1}})
    assert trx.serialize() == [{"patch": {"id": "doc", "set": {"a": 1}}}]


def test_patch_commit_returns_documents(make_client: Callable[..., SanityClient], recorder: Recorder) -> None:
    result = make_client(token="t").patch("doc-1").set({"x": 1}).commit()
    assert result.transaction_id == "trx-1"
    assert result.document_id == "doc-1"
    assert result.document == {"_id": "doc-1", "_type": "post", "_rev": "rev-1"}
    assert "returnDocuments=true" in str(recorder.last.url)


def test_transaction_commit_uses_builder_id(make_client: Callable[..., SanityClient], recorder: Recorder) -> None:
    make_client(token="t").transaction(transaction_id="mine").delete("a").commit()
    assert json_body(recorder.last)["transactionId"] == "mine"
    make_client(token="t").transaction(transaction_id="mine").delete("a").commit(transaction_id="override")
    assert json_body(recorder.last)["transactionId"] == "override"


def test_mutate_takes_the_transaction_id_of_a_transaction(
    make_client: Callable[..., SanityClient], recorder: Recorder
) -> None:
    client = make_client(token="t")
    client.mutate(client.transaction(transaction_id="from-builder").delete("a"))
    assert json_body(recorder.last)["transactionId"] == "from-builder"


def test_mutate_rejects_other_types(make_client: Callable[..., SanityClient]) -> None:
    with pytest.raises(TypeError):
        make_client(token="t").mutate("delete everything")  # type: ignore[arg-type]
