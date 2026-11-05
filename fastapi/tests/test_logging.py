import ast
import logging
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_pipeline_code_uses_logging_not_print():
    """print() is only allowed in __main__ blocks, the CLI output and explicit print_* report helpers."""
    allowed_funcs = {"main", "print_graph_summary"}
    offenders = []
    for f in (ROOT / "lo_pipeline").rglob("*.py"):
        if f.name == "files.py":  # helpers for standalone scripts
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        main_blocks = [(n.lineno, n.end_lineno) for n in tree.body
                       if isinstance(n, ast.If) and "__main__" in ast.unparse(n.test)]
        owner = {}
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef):
                for c in ast.walk(fn):
                    owner[id(c)] = fn.name
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print":
                if any(a <= n.lineno <= b for a, b in main_blocks) or owner.get(id(n)) in allowed_funcs:
                    continue
                offenders.append(f"{f.relative_to(ROOT)}:{n.lineno}")
    assert not offenders, f"use logger instead of print: {offenders}"


def test_old_ui_log_stream_still_receives_pipeline_logs():
    original_stdout = sys.stdout
    try:
        import main  # swaps sys.stdout for its log capture and attaches the lo_pipeline handler
        while not main._log_queue.empty():
            main._log_queue.get_nowait()
        logging.getLogger("lo_pipeline.units.extractor").info("[Extraction] hello")
        logging.getLogger("lo_pipeline.units.extractor").debug("[Dedup] document text")  # below INFO
        lines = []
        while not main._log_queue.empty():
            lines.append(main._log_queue.get_nowait())
        assert any(line.endswith("::[Extraction] hello") for line in lines), lines
        assert not any("document text" in line for line in lines), "debug messages must not reach the UI"
    finally:
        sys.stdout = original_stdout
