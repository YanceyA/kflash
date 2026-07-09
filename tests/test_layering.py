"""Enforce the engine -> events/decisions layering rule (Phase 2 §B.5) and the
Textual UI's boundary (UI_BRAINSTORM §5), post Stage 3 (legacy UI deleted).

Engine modules must talk to the world only through ``Emitter``/``EventSink``
(output) and ``DecisionProvider`` (input). Mechanical AST checks guard this
(no import side effects):

1. The legacy UI modules (``tui``/``screen``/``panels``/``theme``/``ansi``/
   ``output``/``ui_input``) were DELETED at Stage 3 -- they must not exist and
   NO module anywhere in the package may import those names again.
2. Engine modules must NOT call ``print()`` or ``input()`` directly -- all
   terminal I/O goes through the ``Emitter``/``DecisionProvider`` seams.

The engine-module set is derived dynamically from the source tree so a newly
added engine module is automatically covered without touching this file.

The Textual UI (``kflash/ui/``) sits on the *other* side of the same seam, so a
second block of AST checks pins its boundary (UI_BRAINSTORM §5 "the UI must not
reach around the contract into engine internals"):

3. No engine module may import ``kflash.ui`` -- the engine must never depend on
   the UI. ``flash.py``'s dispatch is the single sanctioned exception, and it
   must stay a *function-local* lazy import (verified via AST) so importing the
   engine never drags in Textual.
4. No module under ``kflash/ui/`` may call ``print()``/``input()`` -- a stdout
   write corrupts a running Textual app.
5. The derived engine-module set must never silently absorb ``kflash/ui/`` (a
   recursive-glob regression would otherwise start policing the Textual UI as
   engine, or -- worse -- stop policing it at all).

These UI checks recurse into ``kflash/ui/screens/`` too.
"""

from __future__ import annotations

import ast
import glob
import os
from typing import Optional

_KFLASH_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kflash"
)
_COMMANDS_DIR = os.path.join(_KFLASH_DIR, "commands")
_UI_DIR = os.path.join(_KFLASH_DIR, "ui")

# The legacy UI modules deleted at Stage 3 (UI_BRAINSTORM §8). These names are
# tombstones: the files must not reappear under kflash/ and no module may
# import them (rule 1).
LEGACY_UI_MODULES = {
    "tui",
    "screen",
    "panels",
    "theme",
    "ansi",
    "output",
    "ui_input",
}

# Modules that are NOT engine: flash.py is the composition root (the only
# module allowed to know about the UI, via a function-local import).
# Everything else under kflash/ and kflash/commands/ is engine.
NON_ENGINE_MODULES = {"flash"}

# Engine modules the layering rules apply to, derived from the source tree.
# Keys are qualified names: top-level modules by stem ("events"), command
# modules as "commands.<stem>" ("commands.flash_single").


def _derive_engine_modules() -> dict[str, str]:
    modules: dict[str, str] = {}
    for path in sorted(glob.glob(os.path.join(_KFLASH_DIR, "*.py"))):
        stem = os.path.splitext(os.path.basename(path))[0]
        if stem == "__init__" or stem in NON_ENGINE_MODULES:
            continue
        modules[stem] = path
    for path in sorted(glob.glob(os.path.join(_COMMANDS_DIR, "*.py"))):
        stem = os.path.splitext(os.path.basename(path))[0]
        if stem == "__init__":
            continue
        modules[f"commands.{stem}"] = path
    return modules


ENGINE_MODULES = _derive_engine_modules()

# Module names an engine module must never import: every deleted legacy UI
# module (tombstones -- rule 1 additionally bans them package-wide).
BANNED = set(LEGACY_UI_MODULES)

# Direct print()/input() sites tolerated in engine modules, keyed by
# engine-module name -> the set of builtin calls allowed there.
#
# Phase 3 drove this to EMPTY: every engine site now routes through the
# Emitter (output) / DecisionProvider (input) seams. The dict is kept (empty)
# so the enforcement below reads uniformly and any regression -- a new engine
# print()/input() -- fails ``test_engine_modules_do_not_call_print_or_input``.
PRINT_INPUT_ALLOWLIST: dict[str, set[str]] = {}


def _imported_kflash_modules(path: str) -> set[str]:
    """Return the set of sibling kflash modules imported by *path* (AST-level)."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            # Relative import (from .X import ...) or absolute (from kflash.X ...)
            if node.level >= 1 and mod:
                found.add(mod.split(".")[0])
            elif mod.startswith("kflash."):
                found.add(mod.split(".")[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("kflash."):
                    found.add(alias.name.split(".")[1])
    return found


def _print_input_calls(path: str) -> set[str]:
    """Return which of ``print``/``input`` are CALLED as builtins in *path*."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    hits: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("print", "input")
        ):
            hits.add(node.func.id)
    return hits


# --------------------------------------------------------------------------- #
# UI-boundary helpers (UI_BRAINSTORM §5)
# --------------------------------------------------------------------------- #
def _rel(path: str) -> str:
    """Path relative to the repo's ``kflash`` dir, for readable failures."""
    return os.path.relpath(path, os.path.dirname(_KFLASH_DIR))


def _iter_py(root: str) -> list[str]:
    """Every ``*.py`` under *root* (recursive), excluding ``__init__.py``."""
    return [
        path
        for path in sorted(glob.glob(os.path.join(root, "**", "*.py"), recursive=True))
        if os.path.basename(path) != "__init__.py"
    ]


def _is_under_ui(path: str) -> bool:
    return os.path.abspath(path).startswith(_UI_DIR + os.sep)


def _ui_modules() -> list[str]:
    """All modules under ``kflash/ui/`` (including ``screens/``)."""
    return _iter_py(_UI_DIR)


def _non_ui_kflash_modules() -> list[str]:
    """Every ``kflash/**.py`` that is NOT under ``kflash/ui/``.

    This is the engine + frozen-legacy-UI + composition-root surface -- the set
    that must never depend on ``kflash.ui``.
    """
    return [path for path in _iter_py(_KFLASH_DIR) if not _is_under_ui(path)]


def _ui_import_nodes(tree: ast.AST) -> list[ast.stmt]:
    """Import statements in *tree* that pull in the ``kflash.ui`` package."""
    nodes: list[ast.stmt] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            seg: Optional[str] = None
            if node.level >= 1 and mod:
                seg = mod.split(".")[0]
            elif mod.startswith("kflash."):
                seg = mod.split(".")[1]
            if seg == "ui":
                nodes.append(node)
        elif isinstance(node, ast.Import):
            if any(a.name == "kflash.ui" or a.name.startswith("kflash.ui.") for a in node.names):
                nodes.append(node)
    return nodes


def _classify_ui_imports(path: str) -> tuple[list[int], list[int]]:
    """Split ``kflash.ui`` imports in *path* into (module-level, function-local).

    Returns two lists of line numbers so a violation names the exact site.
    """
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    ui_nodes = _ui_import_nodes(tree)
    inside_functions: set[int] = set()
    for func in ast.walk(tree):
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(func):
                inside_functions.add(id(child))
    module_level = [n.lineno for n in ui_nodes if id(n) not in inside_functions]
    func_local = [n.lineno for n in ui_nodes if id(n) in inside_functions]
    return module_level, func_local


def test_engine_module_set_is_sane():
    # Derived dynamically -- guard against an empty/broken glob and confirm the
    # known engine members are present (a UI module leaking in would fail the
    # import/print checks below).
    assert ENGINE_MODULES, "engine-module derivation produced an empty set"
    for expected in ("events", "flash_steps", "runner", "commands.flash_single"):
        assert expected in ENGINE_MODULES, f"engine module not derived: {expected}"
    # The composition root must be excluded from the engine set.
    for excluded in NON_ENGINE_MODULES:
        assert excluded not in ENGINE_MODULES


def test_legacy_ui_modules_are_deleted():
    # Rule 1 (Stage 3): the legacy UI modules are gone and must stay gone. A
    # file reappearing under any of these names would silently re-enter the
    # engine-module set (or worse, shadow the UI) -- fail loudly instead.
    revenants = [
        name
        for name in sorted(LEGACY_UI_MODULES)
        if os.path.exists(os.path.join(_KFLASH_DIR, f"{name}.py"))
    ]
    assert not revenants, (
        f"deleted legacy UI modules have reappeared under kflash/: {revenants}"
    )


def test_no_module_anywhere_imports_legacy_ui_names():
    # Rule 1, import side: NO module in the package (engine, composition root,
    # or Textual UI) may import the deleted legacy module names.
    offenders: dict[str, set[str]] = {}
    for path in _iter_py(_KFLASH_DIR):
        hits = _imported_kflash_modules(path) & LEGACY_UI_MODULES
        if hits:
            offenders[_rel(path)] = hits
    assert not offenders, (
        f"modules importing deleted legacy UI modules: {offenders}"
    )


def test_command_modules_exist():
    # The six commands plus _common must be present (they are engine modules).
    for expected in (
        "commands._common",
        "commands.flash_single",
        "commands.flash_batch",
        "commands.device_add",
        "commands.device_manage",
        "commands.build_cmd",
    ):
        assert expected in ENGINE_MODULES, f"command module missing: {expected}"


def test_engine_modules_do_not_import_ui():
    offenders: dict[str, set[str]] = {}
    for name, path in ENGINE_MODULES.items():
        banned_hits = _imported_kflash_modules(path) & BANNED
        if banned_hits:
            offenders[name] = banned_hits
    assert not offenders, f"engine modules importing UI layers: {offenders}"


def test_engine_modules_do_not_call_print_or_input():
    offenders: dict[str, set[str]] = {}
    for name, path in ENGINE_MODULES.items():
        illegal = _print_input_calls(path) - PRINT_INPUT_ALLOWLIST.get(name, set())
        if illegal:
            offenders[name] = illegal
    assert not offenders, (
        "engine modules calling print()/input() outside the allowlist "
        f"(route them through Emitter/DecisionProvider): {offenders}"
    )


def test_print_input_allowlist_has_no_stale_entries():
    # Enforce the shrink-toward-empty goal: an allowlisted site that no longer
    # calls print()/input() must be removed from PRINT_INPUT_ALLOWLIST so the
    # rule tightens automatically.
    stale: dict[str, set[str]] = {}
    for name, kinds in PRINT_INPUT_ALLOWLIST.items():
        assert name in ENGINE_MODULES, f"allowlisted module not in engine set: {name}"
        extra = kinds - _print_input_calls(ENGINE_MODULES[name])
        if extra:
            stale[name] = extra
    assert not stale, f"stale print/input allowlist entries (tighten!): {stale}"


# --------------------------------------------------------------------------- #
# UI-boundary rules (UI_BRAINSTORM §5) -- rules 3-6 in the module docstring.
# --------------------------------------------------------------------------- #
def test_ui_module_set_is_sane():
    # Guard against a broken glob so the checks below can never vacuously pass.
    ui_modules = _ui_modules()
    assert ui_modules, "kflash/ui module derivation produced an empty set"
    stems = {os.path.splitext(os.path.basename(p))[0] for p in ui_modules}
    for expected in ("app", "skin", "engine_bridge", "dialogs", "style_guide"):
        assert expected in stems, f"ui module not derived: {expected}"
    # The screens/ subpackage must be swept too (regression guard for rule 6).
    assert any(os.sep + "screens" + os.sep in p for p in ui_modules), (
        "kflash/ui/screens/ modules are not being scanned"
    )


def test_no_engine_module_imports_ui():
    # Rule 3: the engine must never depend on the Textual UI. flash.py is the
    # single sanctioned exception and is verified separately (its import must
    # stay function-local).
    offenders: dict[str, set[str]] = {}
    for path in _non_ui_kflash_modules():
        stem = os.path.splitext(os.path.basename(path))[0]
        if stem == "flash":
            continue
        if "ui" in _imported_kflash_modules(path):
            offenders[_rel(path)] = {"ui"}
    assert not offenders, (
        "non-UI modules importing kflash.ui (the engine must not depend on the "
        f"Textual UI): {offenders}"
    )


def test_flash_ui_import_stays_function_local():
    # Rule 3 (the one exception): flash.py's main() may import kflash.ui, but
    # ONLY as a function-local lazy import, so `import kflash.flash` (the
    # engine/commands surface) works in environments without textual installed.
    flash_path = os.path.join(_KFLASH_DIR, "flash.py")
    module_level, func_local = _classify_ui_imports(flash_path)
    assert not module_level, (
        "flash.py imports kflash.ui at module level (line(s) "
        f"{module_level}); the UI import must be function-local so the "
        "engine never imports Textual eagerly"
    )
    assert func_local, (
        "flash.py's sanctioned function-local kflash.ui import is gone -- if the "
        "UI dispatch moved, keep it function-local and update this test"
    )


def test_ui_modules_do_not_call_print_or_input():
    # Rule 4: a stdout write (print) or a cooked-mode read (input) corrupts a
    # running Textual app. Neither may appear anywhere under kflash/ui/.
    offenders: dict[str, set[str]] = {}
    for path in _ui_modules():
        hits = _print_input_calls(path)
        if hits:
            offenders[_rel(path)] = hits
    assert not offenders, (
        "kflash/ui modules calling print()/input() (corrupts the Textual app -- "
        f"use a widget / RichLog / modal instead): {offenders}"
    )


def test_engine_module_derivation_excludes_ui():
    # Rule 5: ENGINE_MODULES is derived from kflash/*.py + kflash/commands/*.py.
    # kflash/ui/ is a subpackage and must never leak into that set -- otherwise
    # the print/input + import-UI engine checks would wrongly police (or a glob
    # change could silently stop policing) the Textual UI.
    ui_paths = {os.path.abspath(p) for p in _ui_modules()}
    leaked = {
        name: path
        for name, path in ENGINE_MODULES.items()
        if os.path.abspath(path) in ui_paths or _is_under_ui(path)
    }
    assert not leaked, f"kflash/ui modules leaked into the engine set: {leaked}"
    assert not any(name.startswith("ui") for name in ENGINE_MODULES), (
        "an engine-module name starts with 'ui' -- the ui package must not be "
        "derived as engine"
    )
