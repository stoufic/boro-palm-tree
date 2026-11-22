"""Build a compact line-to-test map from coverage data."""
import ast, json, os, re
from pathlib import Path
from coverage import CoverageData

ROOT = Path(os.environ.get("SQLGLOT_ROOT", "/opt/sqlglot"))
DATA_FILE = os.environ.get("COVERAGE_FILE", "/tmp/.coverage")
OUTPUT_FILE = Path(os.environ.get("MAP_OUTPUT", "/app/coverage-map.json"))
data, output = CoverageData(basename=DATA_FILE), {
    "lines": {}, "regions": {}, "conditions": {}, "functions": {}, "symbols": {},
    "modules": {}, "imports": {}, "fixtures": {}, "safe_identity": {}, "strings": {},
}
data.read()
records, trees, source_line_maps = [], {}, {}
for measured in data.measured_files():
    path = Path(measured)
    try: relative = path.relative_to(ROOT).as_posix()
    except ValueError: continue
    if not relative.startswith("sqlglot/"): continue
    contexts = data.contexts_by_lineno(measured)
    line_map = {line: {c for c in values if c.startswith("tests/")} for line, values in contexts.items()}
    source_line_maps[relative] = line_map
    try:
        tree = ast.parse(path.read_text())
        trees[relative] = tree
        strings = sorted({(node.lineno, node.end_lineno) for node in ast.walk(tree)
                          if isinstance(node, ast.Constant) and isinstance(node.value, str)
                          and node.end_lineno > node.lineno})
        if strings:
            output["strings"][relative] = strings
        # Map a changed sub-expression to the smallest enclosing executable
        # region. Python reports a line event for a multi-line expression at
        # only one of its lines, so exact line coverage alone is insufficient.
        regions = {}
        for node in ast.walk(tree):
            start, end = getattr(node, "lineno", None), getattr(node, "end_lineno", None)
            if start is None or end is None or end - start > 80:
                continue
            seen = set().union(*(line_map.get(n, set()) for n in range(start, end + 1)))
            if seen:
                regions.setdefault((start, end), set()).update(seen)
        if regions:
            output["regions"][relative] = [
                [start, end, sorted(seen)] for (start, end), seen in regions.items()
            ]
        conditions = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.BoolOp):
                continue
            seen = set().union(*(line_map.get(n, set()) for n in range(node.lineno, node.end_lineno + 1)))
            if seen:
                conditions.setdefault((node.lineno, node.end_lineno), set()).update(seen)
        if conditions:
            output["conditions"][relative] = [
                [start, end, sorted(seen)] for (start, end), seen in conditions.items()
            ]
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                seen = set().union(*(line_map.get(n, set()) for n in range(node.lineno, node.end_lineno + 1)))
                imports = {}
                for child in ast.walk(node):
                    if isinstance(child, ast.ImportFrom) and child.module:
                        for alias in child.names: imports[alias.asname or alias.name] = f"{child.module}.{alias.name}"
                calls = []
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        if isinstance(child.func, ast.Name): calls.append(imports.get(child.func.id, child.func.id))
                        elif isinstance(child.func, ast.Attribute): calls.append(child.func.attr)
                records.append({"file": relative, "module": relative[:-3].replace("/", "."), "name": node.name,
                                "start": node.lineno, "end": node.end_lineno, "contexts": seen, "calls": calls})
    except (OSError, SyntaxError): pass
    useful = {str(line): sorted(values) for line, values in line_map.items() if values}
    if useful: output["lines"][relative] = useful
    module_contexts = set().union(*line_map.values())
    if module_contexts: output["modules"][relative] = sorted(module_contexts)

# Propagate a caller's tests into the functions it calls. This is a static
# caller expansion over the pinned source and catches branch mutations where a
# test reaches the caller but not the original callee path.
by_name = {}
by_full = {}
for record in records:
    by_name.setdefault(record["name"], []).append(record)
    by_full[f'{record["module"]}.{record["name"]}'] = record
for _ in range(4):
    changed = False
    for caller in records:
        for call in caller["calls"]:
            callee = by_full.get(call)
            if callee is None:
                candidates = by_name.get(call, ())
                if len(candidates) == 1: callee = candidates[0]
            if callee is not None:
                before = len(callee["contexts"])
                callee["contexts"].update(caller["contexts"])
                changed |= len(callee["contexts"]) != before
    if not changed: break
for record in records:
    if record["contexts"]:
        output["functions"].setdefault(record["file"], []).append(
            [record["start"], record["end"], sorted(record["contexts"])]
        )

# Class constants execute at import time, before a test context exists. Link
# each constant to the tests that execute production references to its name.
# This catches shared base-class reads such as dialect and tokenizer settings
# without attributing an entire dialect module to every edit.
references = {}
for relative, tree in trees.items():
    line_map = source_line_maps[relative]
    for node in ast.walk(tree):
        name = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
        if not name:
            continue
        start, end = getattr(node, "lineno", 0), getattr(node, "end_lineno", 0)
        seen = set().union(*(line_map.get(n, set()) for n in range(start, end + 1)))
        if seen:
            references.setdefault(name, set()).update(seen)
for relative, tree in trees.items():
    for class_node in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        class_refs = references.get(class_node.name, set())
        for statement in class_node.body:
            targets = []
            if isinstance(statement, ast.Assign):
                targets = [t.id for t in statement.targets if isinstance(t, ast.Name)]
            elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                targets = [statement.target.id]
            for target in targets:
                # Lowercase framework metadata names are too ambiguous across
                # classes; class-name references remain useful for those.
                seen = set(class_refs)
                if target.isupper():
                    seen.update(references.get(target, ()))
                if seen:
                    output["symbols"].setdefault(relative, []).append(
                        [statement.lineno, statement.end_lineno, sorted(seen)]
                    )

# Prove the two common identity/equality equivalences from the pinned source.
# These facts let the runtime selector discard behavior-preserving edits while
# remaining conservative for user-defined classes that overload equality.
classes = {}
for relative, tree in trees.items():
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            classes.setdefault(node.name, []).append((relative, node))
enum_names = {
    name for name, definitions in classes.items()
    if any(any((isinstance(base, ast.Name) and base.id == "Enum") or
               (isinstance(base, ast.Attribute) and base.attr == "Enum") for base in node.bases)
           and not any(isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "__eq__" for item in node.body)
           for _, node in definitions)
}
for _ in range(len(classes)):
    added = {
        name for name, definitions in classes.items() if name not in enum_names
        and any(
            any((isinstance(base, ast.Name) and base.id in enum_names) or
                (isinstance(base, ast.Attribute) and base.attr in enum_names) for base in node.bases)
            and not any(isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "__eq__" for item in node.body)
            for _, node in definitions
        )
    }
    if not added:
        break
    enum_names.update(added)
plain_definitions = {
    (relative, name)
    for name, definitions in classes.items()
    for relative, node in definitions
    if not node.bases and not any(
        isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "__eq__"
        for item in node.body
    )
}

def annotation_names(node):
    return {item.id for item in ast.walk(node) if isinstance(item, ast.Name) and item.id not in {"None", "Any"}}

for relative, tree in trees.items():
    safe = set()
    imported_types = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            source = node.module.replace(".", "/") + ".py"
            for alias in node.names:
                imported_types[alias.asname or alias.name] = source
    for class_node in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
        attributes = {}
        for node in ast.walk(class_node):
            if (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Attribute)
                    and isinstance(node.target.value, ast.Name) and node.target.value.id == "self"):
                attributes[node.target.attr] = annotation_names(node.annotation)
        for node in ast.walk(class_node):
            if not isinstance(node, ast.Compare) or len(node.ops) != 1 or not isinstance(node.ops[0], (ast.Is, ast.IsNot)):
                continue
            operands = (node.left, node.comparators[0])
            # Enum members use identity equality unless their class explicitly
            # overrides __eq__.
            if any(isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name)
                   and value.value.id in enum_names for value in operands):
                safe.add(node.lineno)
                continue
            # `self.attr is None` is equivalent to equality when the annotated
            # concrete class inherits object equality.
            none_side = any(isinstance(value, ast.Constant) and value.value is None for value in operands)
            attr = next((value for value in operands if isinstance(value, ast.Attribute)
                         and isinstance(value.value, ast.Name) and value.value.id == "self"), None)
            types = attributes.get(attr.attr, set()) if attr else set()
            safe_types = {
                name for name in types
                if (imported_types.get(name, relative), name) in plain_definitions
            }
            if none_side and types and types <= safe_types:
                safe.add(node.lineno)
    if safe:
        output["safe_identity"][relative] = sorted(safe)

# Collection-time imports have no test context. Build a transitive reverse
# import map so import-time edits can be attributed through package modules.
known_ids = set()
for lm in output["lines"].values():
    for values in lm.values(): known_ids.update(values)
module_files = {}
for module_path in ROOT.glob("sqlglot/**/*.py"):
    relative_module = module_path.relative_to(ROOT).as_posix()
    module_name = relative_module[:-3].replace("/", ".")
    if module_name.endswith(".__init__"):
        module_name = module_name[:-9]
    module_files[module_name] = relative_module
dependencies = {path: set() for path in module_files.values()}
for module, relative in module_files.items():
    tree = trees.get(relative)
    if tree is None:
        continue
    for node in ast.walk(tree):
        names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
        for name in names:
            candidate = name
            while candidate and candidate not in module_files:
                candidate = candidate.rpartition(".")[0]
            if candidate:
                dependencies[relative].add(module_files[candidate])

import_tests = {path: set() for path in module_files.values()}
for test_file in ROOT.glob("tests/**/*.py"):
    relative_test = test_file.relative_to(ROOT).as_posix() + "::"
    file_ids = {i for i in known_ids if i.startswith(relative_test)}
    if not file_ids: continue
    try: tree = ast.parse(test_file.read_text())
    except (OSError, SyntaxError): continue
    for node in ast.walk(tree):
        modules = []
        if isinstance(node, ast.Import): modules = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module: modules = [node.module]
        for module in modules:
            if module == "sqlglot" or module.startswith("sqlglot."):
                candidate = module
                while candidate and candidate not in module_files:
                    candidate = candidate.rpartition(".")[0]
                if candidate:
                    import_tests[module_files[candidate]].update(file_ids)
for _ in range(len(dependencies)):
    changed = False
    for importer, imported_files in dependencies.items():
        for imported in imported_files:
            before = len(import_tests[imported])
            import_tests[imported].update(import_tests[importer])
            changed |= len(import_tests[imported]) != before
    if not changed:
        break
output["imports"] = {path: values for path, values in import_tests.items() if values}

# Map fixture path literals to their enclosing test method.
class FixtureVisitor(ast.NodeVisitor):
    def __init__(self, test_path): self.test_path, self.stack = test_path, []
    def visit_FunctionDef(self, node):
        self.stack.append(node.name); self.generic_visit(node); self.stack.pop()
    visit_AsyncFunctionDef = visit_FunctionDef
    def visit_ClassDef(self, node):
        self.stack.append(node.name); self.generic_visit(node); self.stack.pop()
    def _record(self, value, test_id):
        fixture = f"tests/fixtures/optimizer/{value}.sql"
        if (ROOT / fixture).exists():
            output["fixtures"].setdefault(fixture, set()).add(test_id)
    def visit_Call(self, node):
        name = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id if isinstance(node.func, ast.Name) else ""
        if (name == "check_file" and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str) and self.stack and self.stack[-1].startswith("test_")):
            self._record(node.args[0].value, self.test_path + "::" + "::".join(self.stack))
        self.generic_visit(node)
    def visit_Constant(self, node):
        if isinstance(node.value, str) and self.stack and self.stack[-1].startswith("test_"):
            value = node.value
            test_id = self.test_path + "::" + "::".join(self.stack)
            if re.fullmatch(r"[A-Za-z0-9_./-]+\.(?:sql|json)", value):
                direct = ROOT / "tests/fixtures" / value
                matches = [direct] if direct.exists() else list((ROOT / "tests/fixtures").rglob(Path(value).name))
                for path in matches:
                    fixture = path.relative_to(ROOT).as_posix()
                    output["fixtures"].setdefault(fixture, set()).add(test_id)
for test_file in ROOT.glob("tests/**/*.py"):
    try: FixtureVisitor(test_file.relative_to(ROOT).as_posix()).visit(ast.parse(test_file.read_text()))
    except (OSError, SyntaxError): pass
output["imports"] = {k: sorted(v) for k, v in output["imports"].items() if v}
output["fixtures"] = {k: sorted(v) for k, v in output["fixtures"].items() if v}
# Intern repeated context sets. Large parser functions share many test IDs, so
# this reduces the serialized map by orders of magnitude.
pool, indexes = [], {}
def intern(values):
    key = tuple(values)
    if key not in indexes: indexes[key] = len(pool); pool.append(values)
    return indexes[key]
for line_map in output["lines"].values():
    for line, values in list(line_map.items()): line_map[line] = intern(values)
for section in ("regions", "conditions", "functions", "symbols"):
    for filename, entries in output[section].items():
        output[section][filename] = [[start, end, intern(values)] for start, end, values in entries]
for section in ("imports", "fixtures"):
    for key, values in list(output[section].items()): output[section][key] = intern(values)
for key, values in list(output["modules"].items()): output["modules"][key] = intern(values)
output["sets"] = pool
OUTPUT_FILE.write_text(json.dumps(output, separators=(",", ":")))
