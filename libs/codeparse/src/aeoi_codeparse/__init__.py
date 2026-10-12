"""tree-sitter code parsing (Phase 14, ADR-024): definitions, references and diff rules.

Shared by the rag code indexer (definitions + references at HEAD) and the code-intelligence
agent (rules over a commit's diff). Pure functions: text in, typed results out, no IO.

[P] names are resolved by SIMPLE name, not by type: "who calls findOpenOrders" also matches an
unrelated method with the same name. Real type resolution needs a build (javac / go/types /
a type checker) per language - documented in ADR-024 as the main precision limit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from typing import Literal

from tree_sitter import Language, Node, Parser

Lang = Literal["java", "python", "go"]
EXTENSIONS: dict[str, Lang] = {".java": "java", ".py": "python", ".go": "go"}

LOOPS: dict[str, set[str]] = {
    "java": {"for_statement", "enhanced_for_statement", "while_statement", "do_statement"},
    "python": {"for_statement", "while_statement"},
    "go": {"for_statement"},
}
CALLS = {"java": "method_invocation", "python": "call", "go": "call_expression"}
DB_RECEIVER = re.compile(r"(repo|repository|dao|db|store)$", re.I)
REMOTE_RECEIVER = re.compile(r"client$", re.I)
LIMIT_NAME = re.compile(r"(timeout|retries|retry|pool|limit|max_?size|max_?conn)", re.I)


@cache
def parser(lang: Lang) -> Parser:
    if lang == "java":
        import tree_sitter_java as grammar
    elif lang == "python":
        import tree_sitter_python as grammar  # type: ignore[no-redef]
    else:
        import tree_sitter_go as grammar  # type: ignore[no-redef]
    return Parser(Language(grammar.language()))


@dataclass(frozen=True)
class Symbol:
    kind: str  # class | interface | struct | method | function | field | constant
    name: str
    container: str | None
    start_line: int  # 1-based
    end_line: int


@dataclass(frozen=True)
class Ref:
    kind: str  # call | type | field
    name: str
    line: int
    enclosing: str | None  # the method/function the use is in
    receiver: str | None = None  # text of the object a method is called on


@dataclass(frozen=True)
class Parsed:
    symbols: list[Symbol]
    refs: list[Ref]
    errors: int  # ERROR nodes: tree-sitter is error tolerant, we count instead of failing


def _text(node: Node | None) -> str:
    return node.text.decode("utf-8", "replace") if node is not None and node.text else ""


def _line(node: Node) -> int:
    return node.start_point[0] + 1


def _walk(node: Node) -> list[Node]:
    out, stack = [], [node]
    while stack:
        n = stack.pop()
        out.append(n)
        stack.extend(reversed(n.children))
    return out


def _enclosing(node: Node, lang: Lang) -> str | None:
    kinds = {
        "java": ("method_declaration", "constructor_declaration"),
        "python": ("function_definition",),
        "go": ("function_declaration", "method_declaration"),
    }[lang]
    n = node.parent
    while n is not None:
        if n.type in kinds:
            return _text(n.child_by_field_name("name")) or None
        n = n.parent
    return None


def _container(node: Node, lang: Lang) -> str | None:
    kinds = {
        "java": (
            "class_declaration",
            "interface_declaration",
            "record_declaration",
            "enum_declaration",
        ),
        "python": ("class_definition",),
        "go": (),
    }[lang]
    n = node.parent
    while n is not None:
        if n.type in kinds:
            return _text(n.child_by_field_name("name")) or None
        n = n.parent
    return None


def _symbols(root: Node, lang: Lang) -> list[Symbol]:
    out: list[Symbol] = []

    def add(kind: str, name_node: Node | None, span: Node, container: str | None) -> None:
        name = _text(name_node)
        if name:
            out.append(Symbol(kind, name, container, _line(span), span.end_point[0] + 1))

    for n in _walk(root):
        t = n.type
        if lang == "java":
            if t in ("class_declaration", "record_declaration", "enum_declaration"):
                add("class", n.child_by_field_name("name"), n, _container(n, lang))
            elif t == "interface_declaration":
                add("interface", n.child_by_field_name("name"), n, _container(n, lang))
            elif t in ("method_declaration", "constructor_declaration"):
                add("method", n.child_by_field_name("name"), n, _container(n, lang))
            elif t == "field_declaration":
                mods = _text(next((c for c in n.children if c.type == "modifiers"), None))
                kind = "constant" if "static" in mods and "final" in mods else "field"
                for d in n.children_by_field_name("declarator"):
                    add(kind, d.child_by_field_name("name"), n, _container(n, lang))
        elif lang == "python":
            if t == "class_definition":
                add("class", n.child_by_field_name("name"), n, _container(n, lang))
            elif t == "function_definition":
                c = _container(n, lang)
                add("method" if c else "function", n.child_by_field_name("name"), n, c)
            elif t == "assignment" and n.parent is not None and n.parent.parent is not None:
                left = n.child_by_field_name("left")
                scope = n.parent.parent.type  # expression_statement -> module / block
                if left is not None and left.type == "identifier":
                    if scope == "module" and _text(left).isupper():
                        add("constant", left, n, None)
                elif left is not None and left.type == "attribute":
                    pass  # self.x = ...: instance state, not a declaration we index
            elif t == "expression_statement" and n.parent is not None and n.parent.type == "block":
                # dataclass fields: `name: str` inside a class body
                child = n.children[0] if n.children else None
                if (
                    child is not None
                    and child.type == "assignment"
                    and child.child_by_field_name("type")
                ):
                    c = _container(n, lang)
                    if c:
                        add("field", child.child_by_field_name("left"), n, c)
        else:  # go
            if t == "type_spec":
                body = n.child_by_field_name("type")
                kind = (
                    "interface" if body is not None and body.type == "interface_type" else "struct"
                )
                add(kind, n.child_by_field_name("name"), n, None)
                if body is not None and body.type == "struct_type":
                    owner = _text(n.child_by_field_name("name")) or None
                    for f in _walk(body):
                        if f.type == "field_declaration":
                            for nm in f.children_by_field_name("name"):
                                add("field", nm, f, owner)
            elif t == "function_declaration":
                add("function", n.child_by_field_name("name"), n, None)
            elif t == "method_declaration":
                recv = _text(n.child_by_field_name("receiver"))
                owner = (
                    re.sub(r"[^A-Za-z0-9_ ]", " ", recv).split()[-1] if recv.strip("() ") else None
                )
                add("method", n.child_by_field_name("name"), n, owner)
            elif t == "const_spec":
                for nm in n.children_by_field_name("name"):
                    add("constant", nm, n, None)
    return out


def _call_target(node: Node, lang: Lang) -> tuple[str, str | None]:
    """(called name, receiver text) of a call node."""
    if lang == "java":
        return _text(node.child_by_field_name("name")), _text(
            node.child_by_field_name("object")
        ) or None
    fn = node.child_by_field_name("function")
    if fn is None:
        return "", None
    if lang == "python" and fn.type == "attribute":
        return _text(fn.child_by_field_name("attribute")), _text(fn.child_by_field_name("object"))
    if lang == "go" and fn.type == "selector_expression":
        return _text(fn.child_by_field_name("field")), _text(fn.child_by_field_name("operand"))
    return _text(fn), None


def _refs(root: Node, lang: Lang) -> list[Ref]:
    out: list[Ref] = []
    call = CALLS[lang]
    for n in _walk(root):
        t = n.type
        if t == call:
            name, recv = _call_target(n, lang)
            if name:
                out.append(Ref("call", name, _line(n), _enclosing(n, lang), recv))
        elif t == "type_identifier" or (lang == "python" and t == "identifier" and n.parent is not None
                                        and n.parent.type == "type"):  # fmt: skip
            out.append(Ref("type", _text(n), _line(n), _enclosing(n, lang)))
        elif lang == "java" and t == "field_access":
            out.append(
                Ref("field", _text(n.child_by_field_name("field")), _line(n), _enclosing(n, lang))
            )
        elif (
            lang == "python" and t == "attribute" and (n.parent is None or n.parent.type != "call")
        ):
            out.append(
                Ref(
                    "field",
                    _text(n.child_by_field_name("attribute")),
                    _line(n),
                    _enclosing(n, lang),
                )
            )
        elif (
            lang == "go"
            and t == "selector_expression"
            and (n.parent is None or n.parent.type != "call_expression")
        ):
            out.append(
                Ref("field", _text(n.child_by_field_name("field")), _line(n), _enclosing(n, lang))
            )
    return [r for r in out if r.name]


def parse(lang: Lang, source: str) -> Parsed:
    tree = parser(lang).parse(source.encode())
    root = tree.root_node
    errors = sum(1 for n in _walk(root) if n.type == "ERROR" or n.is_missing)
    return Parsed(_symbols(root, lang), _refs(root, lang), errors)


# ------------------------------------------------------------------ diff rules (code only)
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def added_lines(patch: str) -> set[int]:
    """New-side line numbers of '+' lines in a unified diff."""
    out: set[int] = set()
    new = 0
    for line in patch.splitlines():
        m = HUNK.match(line)
        if m:
            new = int(m.group(3))
            continue
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            out.add(new)
            new += 1
        elif line.startswith("-"):
            continue
        else:
            new += 1
    return out


@dataclass(frozen=True)
class Finding:
    rule: str  # query_in_loop | remote_call_in_loop | limit_changed | field_removed
    path: str
    line: int  # new-side line (0 when the finding is a removal)
    symbol: str  # what changed: the called method, the constant, the removed field
    detail: str  # code-written, one line
    container: str | None = None  # enclosing method / class
    # Phase 15 (ADR-025): the feature-flag condition the finding sits behind, if any - such a
    # change can be switched off; an unguarded one needs a rollback
    guard: str | None = None


def _in_loop(node: Node, lang: Lang) -> bool:
    """Inside a loop's BODY (review: a call in the loop header - `for (o : repo.findAll())` -
    runs once, not once per item). Stops at a function/lambda boundary ([P]: a call inside a
    lambda passed to forEach / a stream / a comprehension is NOT seen - ADR-024 limit)."""
    loops = LOOPS[lang]
    prev, n = node, node.parent
    while n is not None:
        if n.type in loops:
            body = n.child_by_field_name("body")
            if (
                body is not None
                and body.start_byte <= prev.start_byte
                and prev.end_byte <= body.end_byte
            ):
                return True
        if n.type in ("method_declaration", "function_definition", "function_declaration",
                      "constructor_declaration", "lambda_expression", "func_literal"):  # fmt: skip
            return False
        prev, n = n, n.parent
    return False


class PatchMismatchError(ValueError):
    """The diff does not fit the file it claims to produce (cut, redacted or inconsistent)."""


def pre_image(content_after: str, patch: str) -> str:
    """The file BEFORE the change, rebuilt from the file after it and its unified diff.
    Strict: a context or '+' line that does not match the file raises PatchMismatchError, so a cut
    or rewritten input is reported as unchecked instead of producing wrong findings."""
    post = content_after.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    hunk = False
    for raw in patch.splitlines(keepends=True):
        m = HUNK.match(raw)
        if m:
            start, count = int(m.group(3)), int(m.group(4) if m.group(4) is not None else 1)
            upto = start if count == 0 else start - 1
            if upto < i or upto > len(post):
                raise PatchMismatchError(f"hunk at +{start} out of order")
            out.extend(post[i:upto])
            i, hunk = upto, True
            continue
        if not hunk or raw.startswith(("+++", "---", "\\")):
            continue
        tag, body = raw[:1], raw[1:]
        if tag == "-":
            out.append(body)
            continue
        if tag not in ("+", " ") or i >= len(post) or post[i].rstrip("\n") != body.rstrip("\n"):
            raise PatchMismatchError(f"line {i + 1} does not match the diff")
        if tag == " ":
            out.append(post[i])
        i += 1
    out.extend(post[i:])
    return "".join(out)


# ONE positive flag check, nothing else (review: `!flags.isEnabled(x)`, `flag || legacy`,
# `i.isFlagged()`, `featureCount > 0` were taken as guards and the advice was INVERTED):
#   a call to an is-enabled method:  flags.isEnabled("x") / is_enabled('x') / IsEnabled("x")
#   or a bare flag-named value:       featureFlags.orderBatching / FEATURE_X / cfg.flag_y
FLAG_CALL = re.compile(r"^[A-Za-z_][\w.]*\.?(?:isEnabled|is_enabled|IsEnabled|isFeatureEnabled|Enabled|enabled)"
                       r"\(\s*[\"'][\w.\-]+[\"']\s*\)$")  # fmt: skip
FLAG_NAME = re.compile(r"^[A-Za-z_][\w.]*$")
FLAG_WORD = re.compile(r"(flag|feature|toggle)", re.IGNORECASE)
NOT_A_SINGLE_CHECK = re.compile(r"(!|\bnot\b|\|\||\bor\b|&&|\band\b|[<>=?:])")
IF_NODES = {"java": "if_statement", "python": "if_statement", "go": "if_statement"}


def positive_flag_check(cond: str) -> bool:
    """True only for a single, positive feature-flag check: turning that flag OFF is then
    guaranteed to skip the THEN branch. Anything else (negated, combined, compared, a method
    that merely mentions a flag) is not a guard - the safe advice (a redeploy) applies."""
    text = " ".join(cond.split())
    while text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    if not text or NOT_A_SINGLE_CHECK.search(text.replace('"', "").replace("'", "")
                                             if FLAG_CALL.match(text) else text):  # fmt: skip
        return False
    if FLAG_CALL.match(text):
        return True
    return bool(FLAG_NAME.match(text)) and bool(FLAG_WORD.search(text.rsplit(".", 1)[-1]))


def flag_guard(node: Node, lang: Lang) -> str | None:
    """The condition of the nearest enclosing `if` that is ONE positive feature-flag check
    (`positive_flag_check`), when `node` is in its THEN branch (not in the condition, not in an
    else) - up to the function boundary. [P] by name; a flag read into a local variable first
    is not seen (ADR-025): then there is no guard and the advice is a redeploy (safe side)."""
    prev, n = node, node.parent
    while n is not None:
        if n.type == IF_NODES[lang]:
            cond = n.child_by_field_name("condition")
            then = n.child_by_field_name("consequence")
            if (cond is not None and then is not None and positive_flag_check(_text(cond))
                    and then.start_byte <= prev.start_byte and prev.end_byte <= then.end_byte):  # fmt: skip
                text = " ".join(_text(cond).split())
                while text.startswith("(") and text.endswith(")"):
                    text = text[1:-1].strip()
                return text[:160]
        if n.type in ("method_declaration", "function_definition", "function_declaration",
                      "constructor_declaration", "lambda_expression", "func_literal"):  # fmt: skip
            return None
        prev, n = n, n.parent
    return None


GENERIC_FLAG_LEAVES = {"enabled", "on", "active", "value", "flag", "enable", "disabled"}


IDENT_NODES = {"identifier", "field_identifier", "type_identifier", "property_identifier",
               "constant"}  # fmt: skip
STRING_NODES = {"string", "string_literal", "interpreted_string_literal", "raw_string_literal",
                "string_content", "string_fragment"}  # fmt: skip


def names_and_strings(lang: Lang, source: str) -> set[str]:
    """Every identifier and every string literal's text in the source - comments EXCLUDED
    (review: a `// TODO remove orderBatching` comment counted as reading the flag)."""
    out: set[str] = set()
    for n in _walk(parser(lang).parse(source.encode()).root_node):
        if n.type in IDENT_NODES:
            out.add(_text(n))
        elif n.type in STRING_NODES:
            out.add(_text(n).strip("\"'`"))
    return out


def flag_variants(key: str) -> set[str]:
    """`feature_flags.order_batching` -> the names code may use for that flag:
    order_batching, orderBatching, OrderBatching, ORDER_BATCHING, order-batching."""
    parts = key.split(".")
    leaf = parts[-1]
    if leaf.lower() in GENERIC_FLAG_LEAVES and len(parts) > 1:  # features.order_batching.enabled
        leaf = parts[-2]
    words = [w for w in re.split(r"[_\-]", leaf) if w]
    if not words:
        return set()
    camel = words[0].lower() + "".join(w.capitalize() for w in words[1:])
    return {leaf, camel, camel[:1].upper() + camel[1:], "_".join(words).upper(), "-".join(words)}


def _loop_calls(lang: Lang, root: Node) -> list[tuple[Node, str, str, str | None]]:
    """(node, receiver's last name, called name, enclosing function) of every call in a loop body."""
    out = []
    for n in _walk(root):
        if n.type == CALLS[lang] and _in_loop(n, lang):
            name, recv = _call_target(n, lang)
            out.append((n, (recv or "").split(".")[-1], name, _enclosing(n, lang)))
    return out


def calls_in_loops(
    lang: Lang, path: str, content_after: str, patch: str, before: str | None = None
) -> list[Finding]:
    """A database/remote call that is in a loop body NOW, on an added line, and was NOT in a
    loop body of the same function before (review: a re-indented or edited line that already
    queried in a loop is not new). `before` defaults to the rebuilt pre-image."""
    added = added_lines(patch)
    if before is None:
        before = pre_image(content_after, patch)
    old = {
        (r, c, e) for _, r, c, e in _loop_calls(lang, parser(lang).parse(before.encode()).root_node)
    }
    root = parser(lang).parse(content_after.encode()).root_node
    out: list[Finding] = []
    for n, last, name, where in _loop_calls(lang, root):
        if _line(n) not in added or (last, name, where) in old:
            continue
        rule = ("query_in_loop" if DB_RECEIVER.search(last)
                else "remote_call_in_loop" if REMOTE_RECEIVER.search(last) else None)  # fmt: skip
        if rule is None:
            continue
        out.append(Finding(
            rule, path, _line(n), f"{last}.{name}",
            f"{last}.{name}() is called inside a loop in {where or 'the file'} (line {_line(n)}): "
            + ("one database query per item" if rule == "query_in_loop" else "one remote call per item"),
            where, flag_guard(n, lang),
        ))  # fmt: skip
    return out


ASSIGN = re.compile(r"^[-+]\s*(?:public\s+|private\s+|static\s+|final\s+)*(?:[\w<>]+\s+)?"
                    r"([A-Za-z_]\w*)\s*(?::\s*\w+\s*)?=\s*(-?\d+(?:\.\d+)?)\s*;?\s*$")  # fmt: skip


def limits_changed(path: str, patch: str) -> list[Finding]:
    """A numeric limit (timeout, retries, pool size...) whose value changed in this diff."""
    old: dict[str, str] = {}
    new: dict[str, tuple[str, int]] = {}
    newline = 0
    for line in patch.splitlines():
        m = HUNK.match(line)
        if m:
            newline = int(m.group(3))
            continue
        if line.startswith(("+++", "---")):
            continue
        a = ASSIGN.match(line)
        if line.startswith("-"):
            if a and LIMIT_NAME.search(a.group(1)):
                old[a.group(1)] = a.group(2)
            continue
        if line.startswith("+") and a and LIMIT_NAME.search(a.group(1)):
            new[a.group(1)] = (a.group(2), newline)
        newline += 1
    return [
        Finding("limit_changed", path, ln, name, f"{name} changed from {old[name]} to {value}")
        for name, (value, ln) in sorted(new.items())
        if name in old and old[name] != value
    ]


def fields_removed(
    lang: Lang, path: str, content_after: str, patch: str, before: str | None = None
) -> list[Finding]:
    """A field of a class / struct that exists BEFORE the change and not after it (removed or
    renamed): every reader of the old name breaks - the input of change impact. Parsed, not
    pattern-matched (review: a removed function parameter is not a field; Go tags and Java
    annotations are fields). `container` = the owning type."""
    if before is None:
        before = pre_image(content_after, patch)

    def fields(src: str) -> set[tuple[str, str]]:
        return {(x.container or "", x.name) for x in parse(lang, src).symbols
                if x.kind == "field" and x.container}  # fmt: skip

    out = []
    for owner, name in sorted(fields(before) - fields(content_after)):
        out.append(Finding("field_removed", path, 0, name,
                           f"field {owner}.{name} was removed or renamed in "
                           f"{path.rsplit('/', 1)[-1]}", owner))  # fmt: skip
    return out


def accessor_names(field: str) -> set[str]:
    """Names a reader may use for a field across the three languages: address, getAddress,
    Address (Go exported), delivery_address <-> deliveryAddress are NOT merged ([P] limit)."""
    cap = field[:1].upper() + field[1:]
    return {field, cap, f"get{cap}", f"is{cap}"}


def analyze_diff(lang: Lang, path: str, content_after: str, patch: str) -> list[Finding]:
    """All rules over one file's change. Raises PatchMismatchError when the diff does not fit the
    file (the caller reports the file as unchecked)."""
    before = pre_image(content_after, patch)
    return [
        *calls_in_loops(lang, path, content_after, patch, before),
        *limits_changed(path, patch),
        *fields_removed(lang, path, content_after, patch, before),
    ]


def language_of(path: str) -> Lang | None:
    for ext, lang in EXTENSIONS.items():
        if path.endswith(ext):
            return lang
    return None
