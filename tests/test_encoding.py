from __future__ import annotations

import pytest

from sanity_client._encoding import (
    encode_form_component,
    encode_query_string,
    encode_uri_component,
    form_urlencode,
    js_json_dumps,
    js_number,
    js_string,
    query_pairs,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, "0"),
        (-0.0, "0"),
        (1.0, "1"),
        (-1.5, "-1.5"),
        (0.1, "0.1"),
        (100.0, "100"),
        (1e20, "100000000000000000000"),
        (1e21, "1e+21"),
        (1.5e21, "1.5e+21"),
        (1e-6, "0.000001"),
        (1e-7, "1e-7"),
        (1.25e-7, "1.25e-7"),
        (123456.789, "123456.789"),
        (5e-324, "5e-324"),
        (1.7976931348623157e308, "1.7976931348623157e+308"),
        (float("nan"), "null"),
        (float("inf"), "null"),
    ],
)
def test_js_number_matches_ecmascript(value: float, expected: str) -> None:
    assert js_number(value) == expected


def test_js_json_dumps_is_compact_and_keeps_unicode() -> None:
    assert js_json_dumps({"a": [1, 2.0, None, True, False], "b": "ø日😀"}) == '{"a":[1,2,null,true,false],"b":"ø日😀"}'


def test_js_json_dumps_keeps_insertion_order() -> None:
    assert js_json_dumps({"z": 1, "a": 2, "m": 3}) == '{"z":1,"a":2,"m":3}'


def test_js_json_dumps_escapes_like_json_stringify() -> None:
    assert js_json_dumps('quote " back \\ nl \n tab \t ctl \x01') == '"quote \\" back \\\\ nl \\n tab \\t ctl \\u0001"'


def test_js_json_dumps_escapes_lone_surrogates() -> None:
    assert js_json_dumps("a\ud800b") == '"a\\ud800b"'


def test_js_json_dumps_leaves_line_separators_raw() -> None:
    # JSON.stringify does not escape U+2028 / U+2029.
    assert js_json_dumps("  ") == '"  "'


def test_js_json_dumps_uses_to_json() -> None:
    class Thing:
        def to_json(self) -> object:
            return {"x": 1}

    assert js_json_dumps([Thing()]) == '[{"x":1}]'


def test_js_json_dumps_rejects_unknown_types() -> None:
    with pytest.raises(TypeError):
        js_json_dumps({"a": object()})


def test_js_json_dumps_tuple_is_array() -> None:
    assert js_json_dumps((1, "a")) == '[1,"a"]'


def test_js_json_dumps_numeric_keys_become_strings() -> None:
    assert js_json_dumps({1: "a", 2.5: "b"}) == '{"1":"a","2.5":"b"}'


@pytest.mark.parametrize(
    ("raw", "encoded"),
    [
        ("abc-_.*", "abc-_.*"),
        ("a b", "a+b"),
        ("~", "%7E"),
        ("$", "%24"),
        ("'()!", "%27%28%29%21"),
        ("&=+/?#", "%26%3D%2B%2F%3F%23"),
        ("ø", "%C3%B8"),
        ("😀", "%F0%9F%98%80"),
    ],
)
def test_encode_form_component_matches_url_search_params(raw: str, encoded: str) -> None:
    assert encode_form_component(raw) == encoded


def test_encode_form_component_replaces_lone_surrogates() -> None:
    assert encode_form_component("\udc00") == "%EF%BF%BD"


@pytest.mark.parametrize(
    ("raw", "encoded"),
    [("doc 2", "doc%202"), ("a~b*c'(d)!", "a~b*c'(d)!"), ("a/b?c", "a%2Fb%3Fc"), ("ø", "%C3%B8")],
)
def test_encode_uri_component(raw: str, encoded: str) -> None:
    assert encode_uri_component(raw) == encoded


def test_form_urlencode_joins_pairs() -> None:
    assert form_urlencode([("a", "1"), ("b c", "d e")]) == "a=1&b+c=d+e"


def test_js_string_template_rules() -> None:
    assert js_string(True) == "true"
    assert js_string(False) == "false"
    assert js_string(None) == "null"
    assert js_string(2.0) == "2"
    assert js_string(["a", None, 1]) == "a,,1"
    assert js_string("x") == "x"


def test_query_pairs_drops_none_and_repeats_lists() -> None:
    assert query_pairs({"a": None, "b": ["x", None, "y"], "c": True, "d": 1}) == [
        ("b", "x"),
        ("b", "y"),
        ("c", "true"),
        ("d", "1"),
    ]


def test_encode_query_string_orders_tag_first_and_flags_last() -> None:
    qs = encode_query_string(
        "*",
        {"x": 1},
        [("includeMutations", False), ("returnQuery", False), ("tag", "t"), ("visibility", "query"), ("skip", None)],
    )
    assert qs == "?tag=t&query=*&%24x=1&visibility=query&returnQuery=false&includeMutations=false"


def test_encode_query_string_skips_falsy_options() -> None:
    assert encode_query_string("*", None, [("a", ""), ("b", 0), ("c", False)]) == "?query=*"
