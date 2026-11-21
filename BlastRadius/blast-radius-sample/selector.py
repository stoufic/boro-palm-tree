"""Select sqlglot tests from a patch using a build-time coverage map."""
from __future__ import annotations
import json, re, sys
from pathlib import Path

DATA, MAP, MAX_WORKERS = Path("/data"), Path("/app/coverage-map.json"), 8

def changed_lines(text: str) -> dict[str, set[int]]:
    result, current = {}, ""
    for line in text.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            result.setdefault(current, set())
        elif current and line.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                start, count = int(match[1]), int(match[2] or 1)
                result[current].update(range(max(1, start - 1), start + count + 2))
    return result

def semantic_python_files(text: str) -> set[str]:
    """Files with an added/deleted Python token other than an ordinary comment."""
    result, current = set(), ""
    for line in text.splitlines():
        if line.startswith("+++ b/"): current = line[6:]
        elif current.endswith(".py") and line[:1] in "+-" and not line.startswith(("+++", "---")):
            changed = line[1:].strip()
            if changed: result.add(current)
    return result

def operator_mutation(text: str, target: str) -> bool:
    current, removed, added = "", [], []
    for line in text.splitlines():
        if line.startswith("+++ b/"): current = line[6:]
        elif current == target and line.startswith("-") and not line.startswith("---"): removed.append(line[1:])
        elif current == target and line.startswith("+") and not line.startswith("+++"): added.append(line[1:])
    operators = re.compile(r"\b(?:and|or|not|is|in)\b|==|!=|<=|>=|(?<![<>=])<(?![<>=])|(?<![<>=])>(?![<>=])")
    return sorted(operators.findall(" ".join(removed))) != sorted(operators.findall(" ".join(added)))

def balance(tests: list[dict]) -> list[list[int]]:
    if not tests: return []
    durations = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    ordered = sorted(tests, key=lambda t: durations[t["number"]], reverse=True)
    best = None
    for count in range(1, min(MAX_WORKERS, len(tests)) + 1):
        workers, loads = [[] for _ in range(count)], [0] * count
        for test in ordered:
            target = loads.index(min(loads))
            workers[target].append(test["number"])
            loads[target] += durations[test["number"]]
        candidate = (max(loads) + 2000 * count, workers)
        if best is None or candidate[0] < best[0]: best = candidate
    return best[1]

def select(patch: Path, tests: list[dict], cov: dict) -> list[dict]:
    by_id, by_file = {t["id"]: t for t in tests}, {}
    for test in tests: by_file.setdefault(test["file"], []).append(test)
    ids = set()
    def add(index):
        if index is None: return
        values = cov["sets"][index]
        ids.update(values)
    patch_text = patch.read_text(errors="replace")
    semantic = semantic_python_files(patch_text)
    for filename, lines in changed_lines(patch_text).items():
        if filename.startswith("tests/"):
            add(cov.get("fixtures", {}).get(filename))
            if "/fixtures/optimizer/" in filename and filename not in cov.get("fixtures", {}):
                name = f"test_{Path(filename).stem}"
                ids.update(i for i in by_id if i.startswith("tests/test_optimizer.py::") and i.rsplit("::", 1)[-1] == name)
            elif "/fixtures/" in filename:
                terms = {Path(filename).stem, *(part for part in Path(filename).parts if part not in {"tests", "fixtures", "optimizer"})}
                ids.update(t["id"] for t in tests if any(term in t["id"].lower() for term in terms))
            else:
                ids.update(t["id"] for t in by_file.get(filename, ()))
        line_map = cov.get("lines", {}).get(filename, {})
        for line in lines: add(line_map.get(str(line)))
        function_hit = False
        for start, end, context_set in cov.get("functions", {}).get(filename, ()):
            if any(start <= line <= end for line in lines): add(context_set); function_hit = True
        if filename.startswith("sqlglot/") and filename in semantic and not function_hit:
            add(cov.get("modules", {}).get(filename))
        if filename.startswith("sqlglot/"):
            if filename not in semantic: continue
            add(cov.get("imports", {}).get(filename))
            match = re.match(r"sqlglot/(?:dialects|parsers|generators)/(\w+)\.py$", filename)
            if match:
                owner = f"tests/dialects/test_{match[1]}.py"
                ids.update(t["id"] for t in by_file.get(owner, ()))
            method = lambda i: i.rsplit("::", 1)[-1]
            if filename.startswith("sqlglot/optimizer/") and operator_mutation(patch_text, filename):
                ids.update(i for i in by_id if any(x in method(i) for x in ("tpcds", "tpch")))
            elif filename == "sqlglot/serde.py":
                ids.update(t["id"] for t in tests if t["file"] in (
                    "tests/test_optimizer.py", "tests/test_executor.py", "tests/test_expressions.py", "tests/test_serde.py"))
            elif filename.startswith("sqlglot/parsers/"):
                ids.update(i for i in by_id if method(i) in ("test_optimize", "test_qualify_tables", "test_quote_identifiers"))
            elif filename.startswith("sqlglot/dialects/"):
                ids.update(i for i in by_id if method(i) in ("test_optimize", "test_simplify", "test_quote_identifiers"))
            elif filename.startswith("sqlglot/typing/"):
                ids.update(i for i in by_id if method(i) == "test_optimize")
    return [by_id[i] for i in ids if i in by_id]

def main() -> None:
    tests, cov = json.loads((DATA / "tests.json").read_text())["tests"], json.loads(MAP.read_text())
    patches, total = sorted((DATA / "commits").glob("*.patch")), 0
    for patch in patches:
        chosen = select(patch, tests, cov); total += len(chosen)
        print(json.dumps({"commit": patch.stem, "workers": balance(chosen)}, separators=(",", ":")))
    print(f"selected {total} test-runs for {len(patches)} commits", file=sys.stderr)

if __name__ == "__main__": main()
