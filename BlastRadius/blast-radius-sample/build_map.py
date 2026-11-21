"""Build a compact line-to-test map from coverage data."""
import ast, json, os, re
from pathlib import Path
from coverage import CoverageData

ROOT = Path(os.environ.get("SQLGLOT_ROOT", "/opt/sqlglot"))
DATA_FILE = os.environ.get("COVERAGE_FILE", "/tmp/.coverage")
OUTPUT_FILE = Path(os.environ.get("MAP_OUTPUT", "/app/coverage-map.json"))
data, output = CoverageData(basename=DATA_FILE), {"lines": {}, "functions": {}, "modules": {}, "imports": {}, "fixtures": {}}
data.read()
records = []
for measured in data.measured_files():
    path = Path(measured)
    try: relative = path.relative_to(ROOT).as_posix()
    except ValueError: continue
    if not relative.startswith("sqlglot/"): continue
    contexts = data.contexts_by_lineno(measured)
    line_map = {line: {c for c in values if c.startswith("tests/")} for line, values in contexts.items()}
    try:
        tree = ast.parse(path.read_text())
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

# Collection-time imports have no test context. Attribute direct imports to all
# known tests in the importing file as a conservative safety net.
known_ids = set()
for lm in output["lines"].values():
    for values in lm.values(): known_ids.update(values)
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
                candidate = module.replace(".", "/") + ".py"
                if (ROOT / candidate).exists(): output["imports"].setdefault(candidate, set()).update(file_ids)

# Map fixture path literals to their enclosing test method.
class FixtureVisitor(ast.NodeVisitor):
    def __init__(self, test_path): self.test_path, self.stack = test_path, []
    def visit_FunctionDef(self, node):
        self.stack.append(node.name); self.generic_visit(node); self.stack.pop()
    visit_AsyncFunctionDef = visit_FunctionDef
    def visit_ClassDef(self, node):
        self.stack.append(node.name); self.generic_visit(node); self.stack.pop()
    def visit_Constant(self, node):
        if isinstance(node.value, str) and self.stack and self.stack[-1].startswith("test_"):
            value = node.value
            if value.startswith("optimizer/") and value.endswith(".sql"):
                fixture = "tests/fixtures/" + value
                output["fixtures"].setdefault(fixture, set()).add(self.test_path + "::" + "::".join(self.stack))
            elif re.fullmatch(r"[A-Za-z0-9_]+", value):
                fixture = f"tests/fixtures/optimizer/{value}.sql"
                if (ROOT / fixture).exists():
                    output["fixtures"].setdefault(fixture, set()).add(self.test_path + "::" + "::".join(self.stack))
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
for filename, functions in output["functions"].items():
    output["functions"][filename] = [[start, end, intern(values)] for start, end, values in functions]
for section in ("imports", "fixtures"):
    for key, values in list(output[section].items()): output[section][key] = intern(values)
for key, values in list(output["modules"].items()): output["modules"][key] = intern(values)
output["sets"] = pool
OUTPUT_FILE.write_text(json.dumps(output, separators=(",", ":")))
