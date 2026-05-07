#!/usr/bin/env python3
"""
Batch runner for the SourceKit Analyzer MCP tools.

The MCP server returns JSON per call. This runner calls the same tool
implementations in-process and writes the raw results to disk so later agents
can parse them into the three migration documents.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import server  # noqa: E402


EXCLUDED_DIRS = {".git", ".build", ".swiftpm", "DerivedData", "build", "Pods"}


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def safe_name(path: Path) -> str:
    return "__".join(path.parts).replace(" ", "_").replace(":", "_")


def relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def selection_start(symbol: Dict[str, Any]) -> Dict[str, int]:
    selection = symbol.get("selection_range") or symbol.get("range") or {}
    start = selection.get("start") or {}
    return {
        "line": int(start.get("line", 0)),
        "character": int(start.get("character", 0)),
    }


def symbol_id(symbol: Dict[str, Any]) -> Tuple[str, str, int, int]:
    pos = selection_start(symbol)
    return (
        str(symbol.get("file_path", "")),
        str(symbol.get("name", "")),
        pos["line"],
        pos["character"],
    )


def walk_files(root: Path, suffix: str, exclude_path_regex: List["re.Pattern"] = None) -> List[Path]:
    excludes = exclude_path_regex or []
    results: List[Path] = []
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        for name in names:
            if not name.endswith(suffix):
                continue
            full = Path(base, name).resolve()
            rel = str(full.relative_to(root)) if full.is_relative_to(root) else str(full)
            if any(rx.search(rel) for rx in excludes):
                continue
            results.append(full)
    return sorted(results)


def package_files(root: Path, excludes=None) -> List[Path]:
    return walk_files(root, "Package.swift", excludes)


def swift_files(root: Path, excludes=None) -> List[Path]:
    return walk_files(root, ".swift", excludes)


def parse_package_dependencies(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    package_matches = []
    for match in re.finditer(r"\.package\s*\((.*?)\)", text, flags=re.DOTALL):
        expr = " ".join(match.group(0).split())
        package_matches.append(expr)
    product_matches = []
    for match in re.finditer(r"\.product\s*\((.*?)\)", text, flags=re.DOTALL):
        expr = " ".join(match.group(0).split())
        product_matches.append(expr)
    return {
        "file_path": str(path),
        "relative_path": relative(path, server.ROOT),
        "package_dependencies": package_matches,
        "product_dependencies": product_matches,
    }


def find_core_symbol_names(root: Path, excludes=None) -> List[str]:
    candidates = set()
    patterns = [
        r"^\s*@main\s*\n\s*struct\s+([A-Za-z_][A-Za-z0-9_]*)",
        r"^\s*(?:public\s+|final\s+|open\s+|internal\s+|private\s+|fileprivate\s+)*class\s+([A-Za-z_][A-Za-z0-9_]*(?:ViewModel|Interactor|Controller|Router|Coordinator|Assembly))\b",
        r"^\s*(?:public\s+|final\s+|open\s+|internal\s+|private\s+|fileprivate\s+)*struct\s+([A-Za-z_][A-Za-z0-9_]*(?:View|ViewModel|Interactor|Router|Coordinator|Assembly))\b",
        r"^\s*(?:public\s+|open\s+|internal\s+|private\s+|fileprivate\s+)*protocol\s+([A-Za-z_][A-Za-z0-9_]*)\b",
    ]
    combined = [re.compile(pattern, re.MULTILINE) for pattern in patterns]
    for path in swift_files(root, excludes):
        text = path.read_text(encoding="utf-8", errors="replace")
        for regex in combined:
            for match in regex.finditer(text):
                candidates.add(match.group(1))
    return sorted(candidates)


def run(args: argparse.Namespace) -> Dict[str, Any]:
    root = Path(args.project_root).resolve()
    output = Path(args.output).resolve()

    server.ROOT = root
    server._lsp_clients.clear()
    if args.no_lsp:
        server.DISABLE_LSP = True

    excludes = [re.compile(p) for p in (args.exclude_path_regex or [])]

    started = time.time()
    output.mkdir(parents=True, exist_ok=True)

    metadata = {
        "project_root": str(root),
        "output_root": str(output),
        "mode": "fallback-no-lsp" if args.no_lsp else "sourcekit-lsp-with-fallback",
        "exclude_path_regex": args.exclude_path_regex or [],
        "source_revision": args.source_revision or "",
        "started_at_epoch": started,
    }
    write_json(output / "metadata.json", metadata)

    tree = server.tool_get_directory_tree({"root_path": str(root), "include_hidden": False})
    write_json(output / "directory_tree.json", tree)

    packages = [parse_package_dependencies(path) for path in package_files(root, excludes)]
    write_json(output / "package_dependencies.json", {"packages": packages})

    all_swift = swift_files(root, excludes)
    if args.limit and args.limit > 0:
        selected_swift = all_swift[: args.limit]
    else:
        selected_swift = all_swift
    write_json(
        output / "swift_files.json",
        {
            "total_swift_files": len(all_swift),
            "analyzed_swift_files": len(selected_swift),
            "files": [relative(path, root) for path in selected_swift],
        },
    )

    structures_index = []
    collected_symbols: List[Dict[str, Any]] = []
    for index, path in enumerate(selected_swift, start=1):
        rel = relative(path, root)
        try:
            data = server.tool_get_file_structure({"file_path": str(path)})
            status = "ok"
        except Exception as exc:
            data = {"file_path": str(path), "relative_path": rel, "error": str(exc)}
            status = "error"
        data["relative_path"] = rel
        if isinstance(data.get("symbols"), list):
            collected_symbols.extend(
                [
                    {
                        "name": symbol.get("name", ""),
                        "file_path": str(path),
                        "relative_path": rel,
                        "kind": symbol.get("kind", ""),
                        "range": symbol.get("range"),
                        "selection_range": symbol.get("selection_range"),
                    }
                    for symbol in data["symbols"]
                    if symbol.get("name")
                ]
            )
        target = output / "file_structures" / f"{safe_name(Path(rel))}.json"
        write_json(target, data)
        structures_index.append({"file": rel, "status": status, "json": relative(target, output)})
        if args.progress:
            print(f"[{index}/{len(selected_swift)}] {status}: {rel}", file=sys.stderr)
    write_json(output / "file_structures_index.json", {"files": structures_index})
    write_json(output / "symbol_catalog.json", {"symbols": collected_symbols})

    symbols = args.symbol or []
    if args.auto_core_symbols:
        symbols = sorted(set(symbols) | set(find_core_symbol_names(root, excludes)))
    if args.all_symbol_definitions:
        symbols = sorted(set(symbols) | {str(item["name"]).split(".")[-1] for item in collected_symbols if item.get("name")})
    if args.exclude_symbol_regex:
        excludes = [re.compile(pattern) for pattern in args.exclude_symbol_regex]
        symbols = [symbol for symbol in symbols if not any(regex.search(symbol) for regex in excludes)]
    if args.symbol_limit and args.symbol_limit > 0:
        symbols = symbols[: args.symbol_limit]
    write_json(output / "core_symbols.json", {"symbols": symbols})

    definitions = {}
    references = {}
    for index, symbol in enumerate(symbols, start=1):
        try:
            definitions[symbol] = server.tool_definition({"symbol_name": symbol})
        except Exception as exc:
            definitions[symbol] = {"symbol_name": symbol, "error": str(exc)}
        if args.references:
            try:
                references[symbol] = server.tool_references({"symbol_name": symbol})
            except Exception as exc:
                references[symbol] = {"symbol_name": symbol, "error": str(exc)}
        if args.progress:
            print(f"[symbol {index}/{len(symbols)}] {symbol}", file=sys.stderr)
    write_json(output / "definitions.json", definitions)
    if args.references:
        write_json(output / "references.json", references)

    if args.diagnostics:
        diagnostics_index = []
        for index, path in enumerate(selected_swift, start=1):
            rel = relative(path, root)
            try:
                data = server.tool_diagnostics({"file_path": str(path)})
                status = "ok"
            except Exception as exc:
                data = {"file_path": str(path), "relative_path": rel, "error": str(exc)}
                status = "error"
            data["relative_path"] = rel
            target = output / "diagnostics" / f"{safe_name(Path(rel))}.json"
            write_json(target, data)
            diagnostics_index.append({"file": rel, "status": status, "json": relative(target, output)})
            if args.progress:
                print(f"[diagnostics {index}/{len(selected_swift)}] {status}: {rel}", file=sys.stderr)
        write_json(output / "diagnostics_index.json", {"files": diagnostics_index})

    if args.hovers or args.hover_all_discovered_symbols:
        seen: Set[Tuple[str, str, int, int]] = set()
        hover_index = []
        hover_groups: Dict[str, List[Dict[str, Any]]] = {}
        selected_symbol_names = {name.split(".")[-1] for name in symbols}
        hover_candidates = collected_symbols
        if args.hovers and not args.hover_all_discovered_symbols:
            hover_candidates = [
                symbol for symbol in collected_symbols
                if str(symbol.get("name", "")).split(".")[-1] in selected_symbol_names
            ]
        for symbol in hover_candidates:
            sid = symbol_id(symbol)
            if sid in seen:
                continue
            seen.add(sid)
            file_path = Path(symbol["file_path"])
            rel = symbol["relative_path"]
            position = selection_start(symbol)
            try:
                data = server.tool_hover({"file_path": str(file_path), "position": position})
                status = "ok"
            except Exception as exc:
                data = {
                    "file_path": str(file_path),
                    "relative_path": rel,
                    "symbol_name": symbol["name"],
                    "position": position,
                    "error": str(exc),
                }
                status = "error"
            data["relative_path"] = rel
            data["symbol_name"] = symbol["name"]
            hover_groups.setdefault(rel, []).append(data)
            hover_index.append(
                {
                    "file": rel,
                    "symbol_name": symbol["name"],
                    "line": position["line"],
                    "character": position["character"],
                    "status": status,
                }
            )
            if args.progress and len(hover_index) % 50 == 0:
                print(f"[hover {len(hover_index)}] latest: {rel}::{symbol['name']}", file=sys.stderr)

        for rel, items in hover_groups.items():
            target = output / "hovers" / f"{safe_name(Path(rel))}.json"
            write_json(target, {"file": rel, "hovers": items})
        write_json(output / "hovers_index.json", {"entries": hover_index})

    write_json(
        output / "tool_inventory.json",
        {
            "read_only_tools_exported": [
                item
                for item in [
                    "get_directory_tree",
                    "get_file_structure",
                    "definition",
                    "references" if args.references else None,
                    "diagnostics" if args.diagnostics else None,
                    "hover" if args.hovers else None,
                ]
                if item is not None
            ],
            "mutating_tools_not_run": ["edit_file", "rename_symbol"],
        },
    )

    completed = {
        **metadata,
        "completed_at_epoch": time.time(),
        "duration_seconds": round(time.time() - started, 3),
        "total_swift_files": len(all_swift),
        "analyzed_swift_files": len(selected_swift),
        "core_symbol_count": len(symbols),
        "collected_symbol_count": len(collected_symbols),
    }
    write_json(output / "summary.json", completed)
    return completed


def main() -> None:
    parser = argparse.ArgumentParser(description="Write raw analyzer JSON results to disk.")
    parser.add_argument("--project-root", default=str(Path.cwd()), help="Swift project root.")
    parser.add_argument("--output", default="AnalysisOutput/raw", help="Output directory for raw JSON.")
    parser.add_argument("--no-lsp", action="store_true", help="Disable SourceKit-LSP and use structural fallback only.")
    parser.add_argument("--limit", type=int, default=0, help="Analyze only the first N Swift files.")
    parser.add_argument("--symbol", action="append", default=[], help="Symbol to collect definition/references for. Repeatable.")
    parser.add_argument("--auto-core-symbols", action="store_true", help="Auto-detect app/viewmodel/interactor/router/controller/protocol symbols.")
    parser.add_argument("--symbol-limit", type=int, default=0, help="Limit auto/core symbols.")
    parser.add_argument(
        "--exclude-symbol-regex",
        action="append",
        default=[],
        help="Exclude matching symbols from definition/reference collection. Repeatable.",
    )
    parser.add_argument("--references", action="store_true", help="Also collect references for selected symbols.")
    parser.add_argument("--diagnostics", action="store_true", help="Collect diagnostics for every selected Swift file.")
    parser.add_argument(
        "--hovers",
        action="store_true",
        help="Collect hover results for selected symbol declaration points (pairs naturally with --auto-core-symbols or --symbol).",
    )
    parser.add_argument(
        "--hover-all-discovered-symbols",
        action="store_true",
        help="Collect hover results for every discovered symbol declaration point in the analyzed files. This can be very slow on large repositories.",
    )
    parser.add_argument(
        "--all-symbol-definitions",
        action="store_true",
        help="Collect definitions/references for every discovered symbol name from file structures, not just core symbols.",
    )
    parser.add_argument("--progress", action="store_true", help="Print progress to stderr.")
    parser.add_argument(
        "--exclude-path-regex",
        action="append",
        default=[],
        help="Skip Swift files whose project-relative path matches this regex. Repeatable. Example: '(^|/)Tests?(/|$)'.",
    )
    parser.add_argument(
        "--source-revision",
        default="",
        help="Optional git revision/tag of the source tree being analyzed. Recorded in metadata.json.",
    )
    args = parser.parse_args()

    summary = run(args)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
