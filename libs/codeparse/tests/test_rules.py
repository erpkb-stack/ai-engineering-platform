"""aeoi_codeparse diff rules (ADR-024): the cases an independent review found, each as a small
before/after pair. A diff is made with difflib exactly as the synthetic data makes it."""

from __future__ import annotations

import difflib

import aeoi_codeparse as cp
import pytest


def diff(before: str, after: str) -> str:
    return "".join(difflib.unified_diff(before.splitlines(keepends=True),
                                        after.splitlines(keepends=True), "a/x", "b/x", n=3))  # fmt: skip


def rules(lang: cp.Lang, before: str, after: str) -> list[tuple[str, str]]:
    return [(f.rule, f.symbol) for f in cp.analyze_diff(lang, "x", after, diff(before, after))]


JAVA_BEFORE = """class S {
    long total(Cart cart) {
        long t = 0;
        for (Item item : cart.items()) {
            t += item.price();
        }
        return t;
    }
}
"""


# ------------------------------------------------------------------ pre-image
@pytest.mark.parametrize(("before", "after"), [
    (JAVA_BEFORE, JAVA_BEFORE.replace("t += item.price();", "t += item.price() * 2;\n            t -= 1;")),
    ("a\nb\nc\n", "a\nc\n"),  # pure removal
    ("a\n", "a\nb\nc\n"),  # pure addition at the end
    ("", "x\n"),  # new file
    ("x\n" * 40 + "y\n", "x\n" * 40 + "z\n" + "x\n"),  # two far-apart hunks
])  # fmt: skip
def test_pre_image_rebuilds_the_file_before_the_change(before: str, after: str) -> None:
    assert cp.pre_image(after, diff(before, after)) == before


def test_a_diff_that_does_not_fit_the_file_is_refused() -> None:
    patch = diff("a\nb\n", "a\nc\n")
    with pytest.raises(cp.PatchMismatchError):
        cp.pre_image("a\nX\n", patch)  # the file was rewritten after the diff (redaction, cut)


# ------------------------------------------------------------------ loop calls
def test_a_query_added_in_a_loop_body_is_found() -> None:
    after = JAVA_BEFORE.replace("t += item.price();",
                                "t += orderRepository.count(item.sku());")  # fmt: skip
    assert rules("java", JAVA_BEFORE, after) == [("query_in_loop", "orderRepository.count")]


def test_a_query_in_the_loop_header_runs_once_and_is_not_a_finding() -> None:
    after = JAVA_BEFORE.replace("for (Item item : cart.items())",
                                "for (Item item : itemRepository.findAll(10))")  # fmt: skip
    assert rules("java", JAVA_BEFORE, after) == []


def test_reindenting_a_query_that_was_already_in_the_loop_is_not_new() -> None:
    before = JAVA_BEFORE.replace("t += item.price();", "t += orderRepository.count(item.sku());")
    after = before.replace("t += orderRepository.count(item.sku());",
                           "t +=  orderRepository.count(item.sku());")  # fmt: skip
    assert rules("java", before, after) == []


def test_a_query_moved_into_a_loop_is_new() -> None:
    before = JAVA_BEFORE.replace("        long t = 0;\n",
                                 "        long t = orderRepository.count(cart.id());\n")  # fmt: skip
    after = JAVA_BEFORE.replace("t += item.price();", "t += orderRepository.count(cart.id());")
    assert rules("java", before, after) == [("query_in_loop", "orderRepository.count")]


def test_python_and_go_loops() -> None:
    py_b = "def f(items, client):\n    out = []\n    for i in items:\n        out.append(i)\n    return out\n"
    py_a = py_b.replace("out.append(i)", "out.append(client.get(i))")
    assert rules("python", py_b, py_a) == [("remote_call_in_loop", "client.get")]
    go_b = "package p\n\nfunc F(ids []string) int {\n\tn := 0\n\tfor _, id := range ids {\n\t\tn += len(id)\n\t}\n\treturn n\n}\n"
    go_a = go_b.replace("n += len(id)", "n += db.Count(id)")
    assert rules("go", go_b, go_a) == [("query_in_loop", "db.Count")]


def test_known_limit_a_query_inside_a_lambda_is_not_seen() -> None:
    """Documented [P] limit (ADR-024): stream/forEach lambdas are a function boundary."""
    after = JAVA_BEFORE.replace("t += item.price();",
                                "cart.items().forEach(i -> orderRepository.count(i.sku()));")  # fmt: skip
    assert rules("java", JAVA_BEFORE, after) == []


# ------------------------------------------------------------------ removed fields
def test_removed_fields_are_parsed_not_pattern_matched() -> None:
    java_b = 'class R {\n    @JsonProperty("a")\n    private Map<String, List<Integer>> address;\n    private int n;\n}\n'
    assert rules("java", java_b, java_b.replace("address", "deliveryAddress")) == [
        ("field_removed", "address")
    ]
    go_b = 'package p\n\ntype R struct {\n\tAddress string `json:"address"`\n\tN int\n}\n'
    assert rules("go", go_b, go_b.replace("Address string", "Street string")) == [
        ("field_removed", "Address")
    ]
    py_b = "from dataclasses import dataclass\n\n\n@dataclass\nclass R:\n    address: str\n    n: int = 0\n"
    found = cp.analyze_diff("python", "x", py_b.replace("    address: str\n", ""),
                            diff(py_b, py_b.replace("    address: str\n", "")))  # fmt: skip
    assert [(f.rule, f.container, f.symbol) for f in found] == [("field_removed", "R", "address")]


def test_a_removed_function_parameter_is_not_a_field() -> None:
    before = "def call(\n    url: str,\n    timeout: float = 5.0,\n) -> None:\n    pass\n"
    after = "def call(\n    url: str,\n) -> None:\n    pass\n"
    assert rules("python", before, after) == []


def test_a_type_change_keeps_the_field() -> None:
    before = "class R {\n    private int n;\n}\n"
    assert rules("java", before, before.replace("private int n;", "private long n;")) == []


# ------------------------------------------------------------------ limits
def test_a_changed_limit_is_reported_with_both_values() -> None:
    before = "class C {\n    static final int TIMEOUT_MS = 200;\n}\n"
    found = cp.analyze_diff("java", "x", before.replace("200", "900"),
                            diff(before, before.replace("200", "900")))  # fmt: skip
    assert [(f.rule, f.symbol, f.detail) for f in found] == [
        ("limit_changed", "TIMEOUT_MS", "TIMEOUT_MS changed from 200 to 900")
    ]


# ------------------------------------------------------------------ Phase 15: flag guard
def guards(lang: cp.Lang, before: str, after: str) -> list[str | None]:
    return [f.guard for f in cp.analyze_diff(lang, "x", after, diff(before, after))
            if f.rule in ("query_in_loop", "remote_call_in_loop")]  # fmt: skip


def test_a_loop_query_behind_a_feature_flag_names_the_flag() -> None:
    after = JAVA_BEFORE.replace(
        "t += item.price();",
        'if (featureFlags.isEnabled("per_item_lookup")) {\n'
        "                t += orderRepository.count(item.sku());\n            }",
    )
    assert guards("java", JAVA_BEFORE, after) == ['featureFlags.isEnabled("per_item_lookup")']


def test_an_unguarded_query_and_one_in_the_else_branch_have_no_guard() -> None:
    plain = JAVA_BEFORE.replace("t += item.price();", "t += orderRepository.count(item.sku());")
    assert guards("java", JAVA_BEFORE, plain) == [None]
    in_else = JAVA_BEFORE.replace(
        "t += item.price();",
        'if (featureFlags.isEnabled("x")) {\n                t += 1;\n            } else {\n'
        "                t += orderRepository.count(item.sku());\n            }",
    )
    assert guards("java", JAVA_BEFORE, in_else) == [None]  # runs when the flag is OFF
    not_a_flag = JAVA_BEFORE.replace(
        "t += item.price();",
        "if (item.price() > 0) {\n                t += orderRepository.count(item.sku());\n            }",
    )
    assert guards("java", JAVA_BEFORE, not_a_flag) == [None]


def test_python_and_go_guards() -> None:
    py_b = "def f(items, client, flags):\n    out = []\n    for i in items:\n        out.append(i)\n    return out\n"
    py_a = py_b.replace(
        "        out.append(i)",
        "        if flags.is_enabled('remote_lookup'):\n            out.append(client.get(i))",
    )
    assert guards("python", py_b, py_a) == ["flags.is_enabled('remote_lookup')"]
    go_b = "package p\n\nfunc F(ids []string) int {\n\tn := 0\n\tfor _, id := range ids {\n\t\tn += len(id)\n\t}\n\treturn n\n}\n"
    go_a = go_b.replace(
        "\t\tn += len(id)", '\t\tif features.Enabled("count") {\n\t\t\tn += db.Count(id)\n\t\t}'
    )
    assert guards("go", go_b, go_a) == ['features.Enabled("count")']


def test_flag_variants() -> None:
    assert cp.flag_variants("feature_flags.order_batching") == {
        "order_batching", "orderBatching", "OrderBatching", "ORDER_BATCHING", "order-batching"
    }  # fmt: skip
