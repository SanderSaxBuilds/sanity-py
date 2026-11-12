"""Byte-exact encoders for what @sanity/client puts on the wire.

The JS client builds query strings with ``URLSearchParams`` and bodies with
``JSON.stringify``. Python's ``urlencode`` and ``json.dumps`` differ from both
in small ways (``~`` and ``*`` are escaped differently, ``1.0`` prints as
``1.0`` instead of ``1``, ``1e-7`` prints as ``1e-07``), and those differences
change cache keys on the API CDN. Everything here follows the JS behaviour.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

__all__ = [
    "QueryValue",
    "encode_form_component",
    "encode_query_string",
    "encode_uri_component",
    "form_urlencode",
    "js_json_dumps",
    "js_number",
    "js_string",
    "query_pairs",
]

QueryValue = Union[str, List[str]]

_LONE_SURROGATE = re.compile("[\ud800-\udfff]")

# application/x-www-form-urlencoded byte set used by URLSearchParams:
# ASCII alphanumerics and "*-._" pass through, space becomes "+".
_FORM_SAFE = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789*-._")
# encodeURIComponent leaves these unescaped.
_URI_COMPONENT_SAFE = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.!~*'()"
)


def _utf8(value: str) -> bytes:
    # A lone surrogate cannot be UTF-8 encoded. Browsers and Node replace it
    # with U+FFFD before encoding, so do the same.
    return _LONE_SURROGATE.sub("�", value).encode("utf-8")


def encode_form_component(value: str) -> str:
    """Encode one key or value the way ``URLSearchParams.toString()`` does."""
    out: List[str] = []
    for byte in _utf8(value):
        if byte in _FORM_SAFE:
            out.append(chr(byte))
        elif byte == 0x20:
            out.append("+")
        else:
            out.append("%{:02X}".format(byte))
    return "".join(out)


def encode_uri_component(value: str) -> str:
    """Same output as JavaScript's ``encodeURIComponent``."""
    out: List[str] = []
    for byte in _utf8(value):
        if byte in _URI_COMPONENT_SAFE:
            out.append(chr(byte))
        else:
            out.append("%{:02X}".format(byte))
    return "".join(out)


def form_urlencode(pairs: Iterable[Tuple[str, str]]) -> str:
    """Join ``(key, value)`` pairs into a ``URLSearchParams`` string."""
    return "&".join(
        encode_form_component(key) + "=" + encode_form_component(value) for key, value in pairs
    )


def js_number(value: float) -> str:
    """Format a float like ECMAScript ``Number.prototype.toString()``.

    Python's ``repr`` already gives the shortest round-trip digits, so only
    the layout changes: no trailing ``.0``, exponents without zero padding,
    and plain notation for magnitudes from ``1e-7`` up to ``1e21``.
    """
    if math.isnan(value) or math.isinf(value):
        # JSON.stringify writes null for these.
        return "null"
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    text = repr(abs(value))
    if "e" in text:
        mantissa, exp_text = text.split("e")
        exponent = int(exp_text)
    else:
        mantissa, exponent = text, 0
    if "." in mantissa:
        int_part, frac_part = mantissa.split(".")
    else:
        int_part, frac_part = mantissa, ""
    digits = (int_part + frac_part).lstrip("0")
    leading_zeros = len(int_part + frac_part) - len((int_part + frac_part).lstrip("0"))
    # value = 0.DIGITS x 10**n
    n = len(int_part) + exponent - leading_zeros
    digits = digits.rstrip("0") or "0"
    k = len(digits)
    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        exp = ("+" if e >= 0 else "-") + str(abs(e))
        body = digits[0] + ("." + digits[1:] if k > 1 else "") + "e" + exp
    return sign + body


def _escape_lone_surrogates(text: str) -> str:
    # JSON.stringify escapes lone surrogates as \udXXX since ES2019.
    return _LONE_SURROGATE.sub(lambda m: "\\u{:04x}".format(ord(m.group(0))), text)


def _json_string(value: str) -> str:
    return _escape_lone_surrogates(json.dumps(value, ensure_ascii=False))


def js_json_dumps(value: Any) -> str:
    """Serialize ``value`` exactly as ``JSON.stringify(value)`` would.

    Supports ``dict`` (string keys, insertion order kept), ``list``,
    ``tuple``, ``str``, ``int``, ``float``, ``bool`` and ``None``. Objects with
    a ``to_json()`` method are serialized through it, which is how patches and
    transactions end up in a request body.
    """
    parts: List[str] = []
    _dump(value, parts)
    return "".join(parts)


def _dump(value: Any, out: List[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        out.append(_json_string(value))
    elif isinstance(value, int):
        out.append(str(value))
    elif isinstance(value, float):
        out.append(js_number(value))
    elif isinstance(value, Mapping):
        out.append("{")
        first = True
        for key, item in value.items():
            if not isinstance(key, str):
                if isinstance(key, (int, float)) and not isinstance(key, bool):
                    key = js_number(float(key)) if isinstance(key, float) else str(key)
                else:
                    raise TypeError(f"object keys must be strings, got {type(key).__name__}")
            if not first:
                out.append(",")
            first = False
            out.append(_json_string(key))
            out.append(":")
            _dump(item, out)
        out.append("}")
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _dump(item, out)
        out.append("]")
    elif hasattr(value, "to_json"):
        _dump(value.to_json(), out)
    else:
        raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def js_string(value: Any) -> str:
    """Template-literal conversion, ``${value}`` in JavaScript."""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, float):
        return js_number(value)
    if isinstance(value, (list, tuple)):
        return ",".join("" if item is None else js_string(item) for item in value)
    return str(value)


def query_pairs(query: Mapping[str, Any]) -> List[Tuple[str, str]]:
    """Expand a query mapping into ordered pairs.

    ``None`` values are dropped, lists become repeated keys (the JS client's
    ``expandQueryArrays``), everything else goes through :func:`js_string`.
    """
    pairs: List[Tuple[str, str]] = []
    for key, value in query.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            for item in value:
                if item is not None:
                    pairs.append((key, js_string(item)))
        else:
            pairs.append((key, js_string(value)))
    return pairs


def encode_query_string(
    query: str,
    params: Optional[Mapping[str, Any]] = None,
    options: Optional[Sequence[Tuple[str, Any]]] = None,
) -> str:
    """Port of ``encodeQueryString`` from @sanity/client.

    Order: ``tag``, ``query``, each ``$param`` as JSON, the remaining truthy
    options, then explicit ``returnQuery=false`` and ``includeMutations=false``.
    Returns the string with its leading ``?``.
    """
    opts = dict(options or ())
    tag = opts.pop("tag", None)
    include_mutations = opts.pop("includeMutations", None)
    return_query = opts.pop("returnQuery", None)
    pairs: List[Tuple[str, str]] = []
    if tag:
        pairs.append(("tag", js_string(tag)))
    pairs.append(("query", query))
    for key, value in (params or {}).items():
        pairs.append(("$" + key, js_json_dumps(value)))
    for key, value in opts.items():
        if value:
            pairs.append((key, js_string(value)))
    if return_query is False:
        pairs.append(("returnQuery", "false"))
    if include_mutations is False:
        pairs.append(("includeMutations", "false"))
    return "?" + form_urlencode(pairs)
