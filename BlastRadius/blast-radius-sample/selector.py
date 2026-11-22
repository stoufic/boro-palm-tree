"""Select sqlglot tests from a patch using a build-time coverage map."""
from __future__ import annotations
import json, re, sys
from pathlib import Path

DATA, MAP, MAX_WORKERS = Path("/data"), Path("/app/coverage-map.json"), 8

def _python_change_is_effective(content: str, line_number: int) -> bool:
    stripped = content.strip()
    if not stripped:
        return False
    if not stripped.startswith("#"):
        return True
    # Ordinary Python comments cannot affect execution. Encoding declarations
    # in the first two lines can make a module fail to import, so retain them.
    return line_number <= 2


def patch_changes(text: str) -> dict[str, dict]:
    """Return changed baseline lines from a unified diff.

    Coverage belongs to the pinned pre-change source, so removed line numbers
    and insertion anchors are used. This also handles deletions and renames,
    which the starter's `+++`-only parser silently lost.
    """
    result = {}
    old_path = new_path = None
    old_line = new_line = 0
    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            old_path = new_path = None
        elif raw.startswith("--- "):
            value = raw[4:].split("\t", 1)[0]
            old_path = value[2:] if value.startswith("a/") else None
        elif raw.startswith("+++ "):
            value = raw[4:].split("\t", 1)[0]
            new_path = value[2:] if value.startswith("b/") else None
            path = old_path or new_path
            if path:
                result.setdefault(path, {
                    "lines": set(), "semantic": False, "paths": set(),
                    "removed": [], "added": [], "comments": [],
                })
                result[path]["paths"].update(p for p in (old_path, new_path) if p)
                if old_path != new_path:
                    result[path]["semantic"] = True
        elif raw.startswith("@@"):
            match = re.search(r"-(\d+)(?:,\d+)? \+(\d+)(?:,\d+)?", raw)
            if match:
                old_line, new_line = map(int, match.groups())
        elif (old_path or new_path) and raw[:1] in {" ", "+", "-"}:
            path = old_path or new_path
            change = result[path]
            if raw.startswith(" "):
                old_line += 1
                new_line += 1
                continue
            is_addition = raw.startswith("+")
            line_number = new_line if is_addition else old_line
            effective = not path.endswith(".py") or _python_change_is_effective(raw[1:], line_number)
            if effective:
                change["semantic"] = True
                change["added" if is_addition else "removed"].append((line_number, raw[1:]))
                if old_path:
                    # A pure insertion sits between old_line - 1 and old_line.
                    change["lines"].add(max(1, old_line - (1 if is_addition else 0)))
                    change["lines"].add(max(1, old_line))
            elif raw[1:].strip().startswith("#"):
                change["comments"].append((old_line, is_addition))
            if is_addition:
                new_line += 1
            else:
                old_line += 1
    return result


def identity_edit_is_equivalent(filename: str, change: dict, cov: dict) -> bool:
    if len(change["removed"]) != 1 or len(change["added"]) != 1:
        return False
    old_line, old = change["removed"][0]
    _, new = change["added"][0]
    if old_line not in cov.get("safe_identity", {}).get(filename, ()):
        return False
    def canonical(value: str) -> str:
        value = re.sub(r"\bis\s+not\b|!=", "<NE>", value)
        return re.sub(r"\bis\b|==", "<EQ>", value)
    return canonical(old) == canonical(new)

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
    def values(index):
        return cov["sets"][index] if index is not None else ()
    def add(index):
        if index is None: return
        ids.update(values(index))
    def add_smallest(*indexes):
        candidates = [index for index in indexes if index is not None]
        if candidates:
            add(min(candidates, key=lambda index: len(values(index))))
    changes = patch_changes(patch.read_text(errors="replace"))
    for filename, change in changes.items():
        lines, paths = change["lines"], change["paths"]
        if filename.endswith(".py") and change["comments"]:
            string_ranges = cov.get("strings", {}).get(filename, ())
            for line, is_addition in change["comments"]:
                anchors = (line - 1, line) if is_addition else (line,)
                inside_string = any(
                    (start < line <= end) if is_addition else (start <= line <= end)
                    for start, end in string_ranges
                )
                if inside_string:
                    change["semantic"] = True
                    change["lines"].update(max(1, anchor) for anchor in anchors)
        for path in paths:
            if path.startswith("tests/"):
                fixture_index = cov.get("fixtures", {}).get(path)
                add(fixture_index)
                if fixture_index is not None:
                    pass
                elif "/fixtures/optimizer/" in path:
                    name = f"test_{Path(path).stem}"
                    ids.update(i for i in by_id if i.startswith("tests/test_optimizer.py::") and i.rsplit("::", 1)[-1] == name)
                elif "/fixtures/" in path:
                    terms = {Path(path).stem, *(part for part in Path(path).parts if part not in {"tests", "fixtures", "optimizer"})}
                    ids.update(t["id"] for t in tests if any(term in t["id"].lower() for term in terms))
                else:
                    ids.update(t["id"] for t in by_file.get(path, ()))
        if not filename.startswith("sqlglot/") or not change["semantic"]:
            continue
        if identity_edit_is_equivalent(filename, change, cov):
            continue

        precise = False
        line_map = cov.get("lines", {}).get(filename, {})
        for line in lines:
            index = line_map.get(str(line))
            if index is not None:
                add(index)
                precise = True

        # If the exact physical line has no event, use the smallest enclosing
        # expression/statement region with coverage.
        for line in lines:
            if line_map.get(str(line)) is not None:
                continue
            candidates = [entry for entry in cov.get("regions", {}).get(filename, ()) if entry[0] <= line <= entry[1]]
            if candidates:
                width = min(end - start for start, end, _ in candidates)
                for start, end, index in candidates:
                    if end - start == width:
                        add(index)
                precise = True

        removed_lines = {line for line, _ in change["removed"]}
        removed_text = "\n".join(value for _, value in change["removed"])
        added_text = "\n".join(value for _, value in change["added"])
        control_pattern = re.compile(r"\b(?:and|or)\b")
        if control_pattern.findall(removed_text) != control_pattern.findall(added_text):
            candidates = [entry for entry in cov.get("conditions", {}).get(filename, ())
                          if any(entry[0] <= line <= entry[1] for line in removed_lines)]
            if candidates:
                width = min(end - start for start, end, _ in candidates)
                for start, end, index in candidates:
                    if end - start == width:
                        add(index)
                precise = True

        symbol_indexes = [index for start, end, index in cov.get("symbols", {}).get(filename, ())
                          if any(start <= line <= end for line in lines)]
        if symbol_indexes:
            symbol_index = min(symbol_indexes, key=lambda index: len(values(index)))
            if len(values(symbol_index)) <= 400:
                add(symbol_index)
                precise = True

        if not precise:
            # Function coverage is a conservative fallback for newly inserted
            # code. Import/module attribution is reserved for top-level edits.
            functions = [entry for entry in cov.get("functions", {}).get(filename, ()) if any(entry[0] <= line <= entry[1] for line in lines)]
            if functions:
                width = min(end - start for start, end, _ in functions)
                for start, end, index in functions:
                    if end - start == width:
                        add(index)
            else:
                add_smallest(cov.get("imports", {}).get(filename), cov.get("modules", {}).get(filename))
    return [by_id[i] for i in ids if i in by_id]

def main() -> None:
    tests, cov = json.loads((DATA / "tests.json").read_text())["tests"], json.loads(MAP.read_text())
    patches, total = sorted((DATA / "commits").glob("*.patch")), 0
    for patch in patches:
        chosen = select(patch, tests, cov); total += len(chosen)
        print(json.dumps({"commit": patch.stem, "workers": balance(chosen)}, separators=(",", ":")))
    print(f"selected {total} test-runs for {len(patches)} commits", file=sys.stderr)

if __name__ == "__main__": main()
