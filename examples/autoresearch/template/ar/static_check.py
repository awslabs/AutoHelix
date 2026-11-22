"""The gate on train.py's source: size, imports, no embedded blobs.

    python ar/static_check.py [train.py]

Exits 0 when train.py passes, else 1 with one line per problem:
- at most MAX_BYTES bytes;
- imports only from MODULES (and their submodules), prepare's training-side names
  (PREPARE_NAMES) and ar.export; no relative imports, no `import prepare` (use
  `from prepare import ...`);
- no `__import__`, `exec`, `eval` or `compile` (the builtin; `torch.compile` is fine);
- no string or bytes literal over MAX_STRING characters, no list, tuple, set or dict
  literal over MAX_ITEMS items: data and weights don't belong in train.py.
"""

import ast
import sys

MAX_BYTES = 64 * 1024
MAX_STRING = 2000
MAX_ITEMS = 256
MODULES = {"os", "sys", "gc", "math", "time", "dataclasses", "functools", "itertools", "collections",
           "typing", "contextlib", "warnings", "random", "enum", "copy", "operator", "numpy", "torch",
           "triton", "kernels"}
PREPARE_NAMES = {"MAX_SEQ_LEN", "TIME_BUDGET", "VOCAB_SIZE", "BOS_TOKEN", "SPECIAL_TOKENS", "Tokenizer",
                 "make_dataloader", "get_token_bytes"}
DYNAMIC = {"__import__", "exec", "eval", "compile"}


def problems(source):
    if len(source.encode()) > MAX_BYTES:
        yield f"train.py is {len(source.encode())} bytes; the limit is {MAX_BYTES}"
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        yield f"line {e.lineno}: syntax error: {e.msg}"
        return
    for node in ast.walk(tree):
        line = f"line {getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if alias.name == "prepare":
                    yield f"{line}: import prepare: use `from prepare import {', '.join(sorted(PREPARE_NAMES))}`"
                elif top not in MODULES and alias.name != "ar.export":
                    yield f"{line}: import {alias.name}: not allowed (allowed: {', '.join(sorted(MODULES))}, ar.export)"
        elif isinstance(node, ast.ImportFrom):
            names = {alias.name for alias in node.names}
            if node.level:
                yield f"{line}: relative imports aren't allowed"
            elif node.module == "prepare":
                if names - PREPARE_NAMES:
                    yield (f"{line}: from prepare import {', '.join(sorted(names - PREPARE_NAMES))}: not allowed "
                           f"(allowed: {', '.join(sorted(PREPARE_NAMES))})")
            elif node.module == "ar":
                if names != {"export"}:
                    yield f"{line}: from ar import: only `export` is allowed"
            elif node.module.split(".")[0] not in MODULES and node.module != "ar.export":
                yield f"{line}: from {node.module} import: not allowed"
        elif isinstance(node, ast.Name) and node.id in DYNAMIC:
            yield f"{line}: {node.id} isn't allowed"
        elif isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            if len(node.value) > MAX_STRING:
                yield f"{line}: a {len(node.value)}-character literal; at most {MAX_STRING}"
        elif isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
            items = len(node.keys if isinstance(node, ast.Dict) else node.elts)
            if items > MAX_ITEMS:
                yield f"{line}: a literal of {items} items; at most {MAX_ITEMS}"


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "train.py"
    with open(path, encoding="utf-8") as f:
        found = list(problems(f.read()))
    for problem in found:
        print(f"static_check: {path}: {problem}")
    if not found:
        print(f"static_check: {path}: OK")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
