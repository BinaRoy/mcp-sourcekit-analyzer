#!/usr/bin/env python3
"""
MCP wrapper that exposes project-analysis and semantic tools over stdio.

Backends are dispatched by file extension, so the same tool surface works
across multiple programming languages:

  - Swift (.swift)   -> SourceKit-LSP
  - Cangjie (.cj)    -> Cangjie official LSPServer (cangjie_tools)

Exposed MCP tools (matching ReCodeAgent §3.1):

  Project analysis:
    get_directory_tree, get_file_structure
  Semantic (LSP-backed):
    definition, hover, references, diagnostics, edit_file, rename_symbol

The server uses only the Python standard library.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, unquote, urlparse


JSON = Dict[str, Any]
ROOT = Path(os.environ.get("SOURCEKIT_ANALYZER_ROOT", os.environ.get("MCP_ANALYZER_ROOT", Path.cwd()))).resolve()
DISABLE_LSP = os.environ.get("SOURCEKIT_ANALYZER_DISABLE_LSP", "").lower() in {"1", "true", "yes"}
LSP_STDERR_LOG = os.environ.get("SOURCEKIT_ANALYZER_LSP_STDERR")

SOURCEKIT_LSP = os.environ.get("SOURCEKIT_LSP", "sourcekit-lsp")
CANGJIE_LSP = os.environ.get("CANGJIE_LSP", "LSPServer")
SOURCEKIT_TMP_ROOT = Path(os.environ.get("SOURCEKIT_ANALYZER_TMPDIR", "/private/tmp")).resolve()
CANGJIE_DYLD_LIBRARY_PATH = os.environ.get("CANGJIE_DYLD_LIBRARY_PATH")
CANGJIE_PATH_PREFIX = os.environ.get("CANGJIE_PATH_PREFIX")
_lsp_spawn_counter = 0


def default_sourcekit_lsp_home() -> Optional[str]:
    """Return a writable HOME for SourceKit-LSP when the real cache is blocked.

    Sandboxed MCP hosts may not grant SourceKit-LSP access to the user's SwiftPM
    cache under ~/Library. In that state SourceKit can abort with "Service is
    invalid". Running only the LSP child with a writable temporary HOME keeps
    semantic requests working without changing the Python server's own HOME.
    """
    configured = os.environ.get("SOURCEKIT_ANALYZER_LSP_HOME")
    if configured == "system":
        return None
    if configured:
        return configured
    return str(SOURCEKIT_TMP_ROOT / f"sourcekit-lsp-home-{os.getpid()}")


LSP_HOME = default_sourcekit_lsp_home()
DEFAULT_SOURCEKIT_LSP_SCRATCH = str(SOURCEKIT_TMP_ROOT / f"sourcekit-lsp-scratch-{os.getpid()}")
SOURCEKIT_LSP_ARGS_ENV = os.environ.get("SOURCEKIT_LSP_ARGS")
CANGJIE_LSP_ARGS = os.environ.get("CANGJIE_LSP_ARGS", "")

EXCLUDED_DIRS = {
    ".git",
    ".build",
    ".swiftpm",
    "DerivedData",
    "build",
    "Pods",
    ".idea",
    ".vscode",
    "target",
    "output",
}

DOCUMENT_SYMBOL_TIMEOUT = float(os.environ.get("SOURCEKIT_ANALYZER_DOCUMENT_SYMBOL_TIMEOUT", "8"))
SEMANTIC_REQUEST_TIMEOUT = float(os.environ.get("SOURCEKIT_ANALYZER_SEMANTIC_TIMEOUT", "20"))


# ---------------------------------------------------------------------------
# Language backends
# ---------------------------------------------------------------------------

SWIFT_DECL_RE = re.compile(
    r"^[ \t]*(?:@\w+(?:\([^)]*\))?[ \t]*(?:\n[ \t]*)?)*"
    r"(?:(?:public|private|fileprivate|internal|open|static|final|actor|indirect|mutating|nonmutating|override|convenience|required)\s+)*"
    r"(class|struct|enum|protocol|actor|extension|func|var|let|typealias)\s+"
    r"([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)",
    re.MULTILINE,
)

# Cangjie: func / class / struct / enum / interface / extend / var / let / type
# Modifiers: public, private, protected, internal, open, static, mut, override,
# redef, operator, abstract, sealed, final, foreign, unsafe.
CANGJIE_DECL_RE = re.compile(
    r"^[ \t]*(?:@\w+(?:\[[^\]]*\])?[ \t]*(?:\n[ \t]*)?)*"
    r"(?:(?:public|private|protected|internal|open|static|mut|override|redef|operator|abstract|sealed|final|foreign|unsafe)\s+)*"
    r"(class|struct|enum|interface|extend|func|var|let|type)\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)


@dataclass
class LanguageBackend:
    name: str                       # "swift" / "cangjie"
    extensions: Tuple[str, ...]     # (".swift",) / (".cj",)
    executable: str                 # path or name of the LSP server binary
    language_id: str                # LSP languageId
    decl_regex: re.Pattern          # fallback declaration regex
    args: Tuple[str, ...] = field(default_factory=tuple)


SWIFT_BACKEND = LanguageBackend(
    name="swift",
    extensions=(".swift",),
    executable=SOURCEKIT_LSP,
    language_id="swift",
    decl_regex=SWIFT_DECL_RE,
    args=tuple(shlex.split(SOURCEKIT_LSP_ARGS_ENV or "")),
)

CANGJIE_BACKEND = LanguageBackend(
    name="cangjie",
    extensions=(".cj",),
    executable=CANGJIE_LSP,
    language_id="cangjie",
    decl_regex=CANGJIE_DECL_RE,
    args=tuple(shlex.split(CANGJIE_LSP_ARGS)),
)

BACKENDS: Tuple[LanguageBackend, ...] = (SWIFT_BACKEND, CANGJIE_BACKEND)
EXT_TO_BACKEND: Dict[str, LanguageBackend] = {
    ext: backend for backend in BACKENDS for ext in backend.extensions
}
ALL_EXTENSIONS: Tuple[str, ...] = tuple(EXT_TO_BACKEND.keys())


def backend_for_path(path: Path) -> Optional[LanguageBackend]:
    return EXT_TO_BACKEND.get(path.suffix)


def lsp_environment(backend: LanguageBackend) -> Dict[str, str]:
    env = os.environ.copy()
    if LSP_HOME:
        Path(LSP_HOME).mkdir(parents=True, exist_ok=True)
        env["HOME"] = LSP_HOME
    if backend.name == "swift":
        for key in ("CANGJIE_HOME", "CANGJIE_DYLD_LIBRARY_PATH", "CANGJIE_PATH_PREFIX"):
            env.pop(key, None)
    elif backend.name == "cangjie":
        if CANGJIE_DYLD_LIBRARY_PATH:
            env["DYLD_LIBRARY_PATH"] = CANGJIE_DYLD_LIBRARY_PATH
        if CANGJIE_PATH_PREFIX:
            env["PATH"] = CANGJIE_PATH_PREFIX + os.pathsep + env.get("PATH", "")
    return env


def lsp_command(backend: LanguageBackend) -> List[str]:
    global _lsp_spawn_counter
    args = list(backend.args)
    if backend.name == "swift" and SOURCEKIT_LSP_ARGS_ENV is None and LSP_HOME:
        _lsp_spawn_counter += 1
        scratch = Path(DEFAULT_SOURCEKIT_LSP_SCRATCH) / str(_lsp_spawn_counter)
        scratch.mkdir(parents=True, exist_ok=True)
        args.extend(["--scratch-path", str(scratch)])
    return [backend.executable, *args]


# ---------------------------------------------------------------------------
# Path / text helpers
# ---------------------------------------------------------------------------

def path_to_uri(path: Path) -> str:
    return "file://" + quote(str(path.resolve()))


def uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    return Path(unquote(parsed.path)).resolve()


def resolve_path(file_path: str) -> Path:
    path = Path(file_path)
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def line_offsets(text: str) -> List[int]:
    offsets = [0]
    for match in re.finditer(r"\n", text):
        offsets.append(match.end())
    return offsets


def offset_to_position(text: str, offset: int) -> JSON:
    offsets = line_offsets(text)
    line = 0
    for idx, start in enumerate(offsets):
        if start > offset:
            break
        line = idx
    return {"line": line, "character": offset - offsets[line]}


def position_to_offset(text: str, line: int, character: int) -> int:
    offsets = line_offsets(text)
    if line >= len(offsets):
        return len(text)
    return min(offsets[line] + character, len(text))


def range_text(text: str, range_obj: JSON) -> str:
    start = range_obj.get("start", {})
    end = range_obj.get("end", {})
    start_offset = position_to_offset(text, start.get("line", 0), start.get("character", 0))
    end_offset = position_to_offset(text, end.get("line", 0), end.get("character", 0))
    return text[start_offset:end_offset].rstrip()


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def source_files(extensions: Iterable[str] = ALL_EXTENSIONS) -> List[Path]:
    exts = tuple(extensions)
    files: List[Path] = []
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        for name in names:
            if name.endswith(exts):
                files.append(Path(base, name).resolve())
    return sorted(files)


def source_files_containing(pattern: str, extensions: Iterable[str] = ALL_EXTENSIONS) -> List[Path]:
    exts = list(extensions)
    if shutil.which("rg"):
        try:
            cmd = ["rg", "-l"]
            for ext in exts:
                cmd += ["--glob", f"*{ext}"]
            cmd += [pattern, str(ROOT)]
            completed = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=10,
            )
            if completed.returncode in (0, 1):
                return [Path(line).resolve() for line in completed.stdout.splitlines() if line.strip()]
        except Exception:
            pass
    return source_files(exts)


# ---------------------------------------------------------------------------
# LSP client
# ---------------------------------------------------------------------------

@dataclass
class PendingRequest:
    event: threading.Event
    response: Optional[JSON] = None


@dataclass(frozen=True)
class LSPRoute:
    backend: LanguageBackend
    root: Path
    route: str
    limitations: Tuple[str, ...] = ()


class LSPClient:
    def __init__(self, backend: LanguageBackend, root: Path) -> None:
        if not shutil.which(backend.executable) and not Path(backend.executable).exists():
            raise RuntimeError(
                f"{backend.name} LSP executable not found: {backend.executable}"
            )

        self.backend = backend
        self.root = root
        self.proc = subprocess.Popen(
            lsp_command(backend),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(root),
            env=lsp_environment(backend),
        )
        self._next_id = 1
        self._lock = threading.Lock()
        self._pending: Dict[int, PendingRequest] = {}
        self._diagnostics: Dict[str, List[JSON]] = {}
        self._opened: set[str] = set()
        self._failed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._stderr = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr.start()
        try:
            self._initialize()
        except Exception:
            # Don't leak the child process on init failure (e.g., timeout).
            self.close()
            raise

    def is_usable(self) -> bool:
        return not self._failed and self.proc.poll() is None

    def close(self) -> None:
        self._failed = True
        if self.proc.poll() is not None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=2)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass

    def _mark_failed(self) -> None:
        self.close()

    def _drain_stderr(self) -> None:
        assert self.proc.stderr is not None
        log = open(LSP_STDERR_LOG, "ab") if LSP_STDERR_LOG else None
        try:
            for line in iter(self.proc.stderr.readline, b""):
                if log:
                    log.write(line)
                    log.flush()
        finally:
            if log:
                log.close()

    def _read_loop(self) -> None:
        assert self.proc.stdout is not None
        while True:
            headers: Dict[str, str] = {}
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    self._failed = True
                    return
                if line in (b"\r\n", b"\n"):
                    break
                raw = line.decode("ascii", errors="replace").strip()
                if ":" in raw:
                    key, value = raw.split(":", 1)
                    headers[key.lower()] = value.strip()
            length = int(headers.get("content-length", "0"))
            if length <= 0:
                continue
            body = self.proc.stdout.read(length)
            try:
                message = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            if "id" in message and message.get("method") is None:
                pending = self._pending.get(int(message["id"]))
                if pending:
                    pending.response = message
                    pending.event.set()
            elif message.get("method") == "textDocument/publishDiagnostics":
                params = message.get("params", {})
                uri = params.get("uri")
                if uri:
                    self._diagnostics[uri] = params.get("diagnostics", [])

    def _send(self, message: JSON) -> None:
        payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
        wire = b"Content-Length: " + str(len(payload)).encode("ascii") + b"\r\n\r\n" + payload
        assert self.proc.stdin is not None
        try:
            with self._lock:
                self.proc.stdin.write(wire)
                self.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            # Pipe died mid-flight — invalidate so the next call respawns.
            self._mark_failed()
            raise

    def request(self, method: str, params: Optional[JSON] = None, timeout: float = 20.0) -> Any:
        request_id = self._next_id
        self._next_id += 1
        pending = PendingRequest(threading.Event())
        self._pending[request_id] = pending
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        except Exception:
            self._pending.pop(request_id, None)
            self._mark_failed()
            raise
        if not pending.event.wait(timeout):
            self._pending.pop(request_id, None)
            self._mark_failed()
            raise TimeoutError(f"{self.backend.name} LSP request timed out: {method}")
        self._pending.pop(request_id, None)
        response = pending.response or {}
        if "error" in response:
            raise RuntimeError(f"{method}: {response['error']}")
        return response.get("result")

    def notify(self, method: str, params: Optional[JSON] = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _initialize(self) -> None:
        self.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootPath": str(self.root),
                "rootUri": path_to_uri(self.root),
                "workspaceFolders": [
                    {
                        "uri": path_to_uri(self.root),
                        "name": self.root.name,
                    }
                ],
                "capabilities": {
                    "textDocument": {
                        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                        "definition": {},
                        "references": {},
                        "hover": {"contentFormat": ["markdown", "plaintext"]},
                        "rename": {"prepareSupport": True},
                        "publishDiagnostics": {"relatedInformation": True},
                    },
                    "workspace": {
                        "symbol": {},
                        "applyEdit": True,
                        "workspaceEdit": {"documentChanges": True},
                    },
                },
            },
        )
        self.notify("initialized", {})

    def open_document(self, path: Path) -> None:
        uri = path_to_uri(path)
        if uri in self._opened:
            return
        self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": self.backend.language_id,
                    "version": 1,
                    "text": read_text(path),
                }
            },
        )
        self._opened.add(uri)

    def did_change(self, path: Path, text: str) -> None:
        uri = path_to_uri(path)
        if uri not in self._opened:
            self.open_document(path)
            return
        self.notify(
            "textDocument/didChange",
            {
                "textDocument": {"uri": uri, "version": 2},
                "contentChanges": [{"text": text}],
            },
        )

    def document_symbols(self, path: Path) -> List[JSON]:
        self.open_document(path)
        return self.request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": path_to_uri(path)}},
            timeout=DOCUMENT_SYMBOL_TIMEOUT,
        ) or []

    def hover(self, path: Path, position: JSON) -> Any:
        self.open_document(path)
        return self.request(
            "textDocument/hover",
            {"textDocument": {"uri": path_to_uri(path)}, "position": position},
            timeout=SEMANTIC_REQUEST_TIMEOUT,
        )

    def definition(self, path: Path, position: JSON) -> Any:
        self.open_document(path)
        return self.request(
            "textDocument/definition",
            {"textDocument": {"uri": path_to_uri(path)}, "position": position},
            timeout=SEMANTIC_REQUEST_TIMEOUT,
        )

    def references(self, path: Path, position: JSON) -> Any:
        self.open_document(path)
        return self.request(
            "textDocument/references",
            {
                "textDocument": {"uri": path_to_uri(path)},
                "position": position,
                "context": {"includeDeclaration": True},
            },
            timeout=SEMANTIC_REQUEST_TIMEOUT,
        )

    def rename(self, path: Path, position: JSON, new_name: str) -> Any:
        self.open_document(path)
        return self.request(
            "textDocument/rename",
            {
                "textDocument": {"uri": path_to_uri(path)},
                "position": position,
                "newName": new_name,
            },
            timeout=SEMANTIC_REQUEST_TIMEOUT,
        )

    def diagnostics(self, path: Path) -> List[JSON]:
        uri = path_to_uri(path)
        self.open_document(path)
        deadline = time.time() + 3.0
        while time.time() < deadline and uri not in self._diagnostics:
            time.sleep(0.05)
        return self._diagnostics.get(uri, [])


_lsp_clients: Dict[str, LSPClient] = {}
_lsp_lock = threading.Lock()


def path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def nearest_ancestor_with_file(path: Path, filename: str) -> Optional[Path]:
    current = path if path.is_dir() else path.parent
    while True:
        if (current / filename).exists():
            return current
        if current == current.parent:
            return None
        current = current.parent


def route_for_path(path: Path) -> LSPRoute:
    backend = backend_for_path(path)
    if backend is None:
        raise ValueError(f"No LSP backend registered for {path.suffix}")

    if backend.name != "swift":
        return LSPRoute(backend=backend, root=ROOT, route="global-root")

    package_root = nearest_ancestor_with_file(path, "Package.swift")
    modules_root = ROOT / "Modules"
    if package_root and (package_root == ROOT or path_is_within(package_root, modules_root)):
        return LSPRoute(backend=backend, root=package_root, route="module-root")

    build_server_root = nearest_ancestor_with_file(path, "buildServer.json")
    limitations: List[str] = []
    if path_is_within(path, ROOT / "Wallet"):
        limitations.append(
            "Wallet app files stay on app-root SourceKit-LSP. In sandboxed and non-indexed environments, cross-module semantics may be null, time out, or degrade."
        )
    if build_server_root is not None:
        return LSPRoute(
            backend=backend,
            root=build_server_root,
            route="app-root-build-server",
            limitations=tuple(limitations),
        )
    return LSPRoute(
        backend=backend,
        root=ROOT,
        route="global-root",
        limitations=tuple(limitations),
    )


def lsp_for(backend: LanguageBackend, root: Optional[Path] = None) -> LSPClient:
    if DISABLE_LSP:
        raise RuntimeError(f"LSP disabled by SOURCEKIT_ANALYZER_DISABLE_LSP")
    lsp_root = (root or ROOT).resolve()
    cache_key = f"{backend.name}:{lsp_root}"
    with _lsp_lock:
        client = _lsp_clients.get(cache_key)
        if client is not None and not client.is_usable():
            client.close()
            _lsp_clients.pop(cache_key, None)
            client = None
        if client is None:
            try:
                client = LSPClient(backend, lsp_root)
            except Exception:
                # Make sure a failed respawn does not poison the slot for the
                # next caller — the next request must be free to try again.
                _lsp_clients.pop(cache_key, None)
                raise
            _lsp_clients[cache_key] = client
        return client


def lsp_for_path(path: Path) -> LSPClient:
    route = route_for_path(path)
    return lsp_for(route.backend, route.root)


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------

def symbol_kind_name(kind: int) -> str:
    names = {
        2: "module", 3: "namespace", 4: "package", 5: "class", 6: "method",
        7: "property", 8: "field", 9: "constructor", 10: "enum",
        11: "interface", 12: "function", 13: "variable", 14: "constant",
        15: "string", 16: "number", 17: "boolean", 18: "array", 19: "object",
        20: "key", 21: "null", 22: "enumMember", 23: "struct", 24: "event",
        25: "operator", 26: "typeParameter",
    }
    return names.get(kind, f"kind_{kind}")


def flatten_document_symbols(symbols: Iterable[JSON], file_path: Path) -> List[JSON]:
    result: List[JSON] = []
    for symbol in symbols:
        if "location" in symbol:
            loc = symbol["location"]
            result.append(
                {
                    "name": symbol.get("name"),
                    "kind": symbol_kind_name(symbol.get("kind", 0)),
                    "file_path": str(uri_to_path(loc["uri"])),
                    "range": loc.get("range"),
                    "selection_range": loc.get("range"),
                }
            )
            continue
        item = {
            "name": symbol.get("name"),
            "kind": symbol_kind_name(symbol.get("kind", 0)),
            "file_path": str(file_path),
            "range": symbol.get("range"),
            "selection_range": symbol.get("selectionRange"),
        }
        result.append(item)
        result.extend(flatten_document_symbols(symbol.get("children", []), file_path))
    return result


def regex_symbols(path: Path) -> List[JSON]:
    backend = backend_for_path(path)
    if backend is None:
        return []
    text = read_text(path)
    items: List[JSON] = []
    for match in backend.decl_regex.finditer(text):
        kind, name = match.group(1), match.group(2)
        pos = offset_to_position(text, match.start(2))
        line_end = text.find("\n", match.start())
        if line_end == -1:
            line_end = len(text)
        items.append(
            {
                "name": name,
                "kind": kind,
                "file_path": str(path),
                "range": {
                    "start": offset_to_position(text, match.start()),
                    "end": offset_to_position(text, line_end),
                },
                "selection_range": {
                    "start": pos,
                    "end": {"line": pos["line"], "character": pos["character"] + len(name)},
                },
            }
        )
    return items


def extract_imports(path: Path) -> List[str]:
    backend = backend_for_path(path)
    if backend is None:
        return []
    text = read_text(path)
    imports: List[str] = []
    if backend.name == "swift":
        pattern = re.compile(r"^[ \t]*import[ \t]+([A-Za-z_][A-Za-z0-9_\.]*)", re.MULTILINE)
    else:
        pattern = re.compile(r"^[ \t]*import[ \t]+([A-Za-z_][A-Za-z0-9_\.]*)", re.MULTILINE)
    for match in pattern.finditer(text):
        imports.append(match.group(1))
    seen = set()
    ordered: List[str] = []
    for item in imports:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def extract_top_level_globals(path: Path) -> List[JSON]:
    backend = backend_for_path(path)
    if backend is None:
        return []
    text = read_text(path)
    globals_: List[JSON] = []
    if backend.name == "swift":
        pattern = re.compile(
            r"^(?:public|private|fileprivate|internal|open|static|final)?[ \t]*(var|let)\s+([A-Za-z_][A-Za-z0-9_]*)",
            re.MULTILINE,
        )
    else:
        pattern = re.compile(r"^(?:public|private|protected|internal|open|static|mut)?[ \t]*(var|let)\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)
    for match in pattern.finditer(text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        if match.start() != line_start:
            continue
        kind, name = match.group(1), match.group(2)
        pos = offset_to_position(text, match.start(2))
        line_end = text.find("\n", match.start())
        if line_end == -1:
            line_end = len(text)
        globals_.append(
            {
                "name": name,
                "kind": kind,
                "file_path": str(path),
                "range": {
                    "start": offset_to_position(text, match.start()),
                    "end": offset_to_position(text, line_end),
                },
                "selection_range": {
                    "start": pos,
                    "end": {"line": pos["line"], "character": pos["character"] + len(name)},
                },
            }
        )
    return globals_


def find_symbol_declarations(symbol_name: str) -> List[JSON]:
    matches: List[JSON] = []
    pattern = (
        r"\b(class|struct|enum|protocol|actor|extension|interface|extend|func|var|let|typealias|type)\s+"
        + re.escape(symbol_name)
        + r"\b"
    )
    for path in source_files_containing(pattern):
        regex_matches = [
            symbol for symbol in regex_symbols(path)
            if symbol.get("name") == symbol_name
            or str(symbol.get("name", "")).endswith("." + symbol_name)
        ]
        matches.extend(regex_matches)
    return matches


def normalize_lsp_locations(result: Any) -> List[JSON]:
    if result is None:
        return []
    if isinstance(result, dict):
        if "uri" in result:
            return [result]
        if "targetUri" in result:
            return [
                {
                    "uri": result.get("targetUri"),
                    "range": result.get("targetRange"),
                    "selectionRange": result.get("targetSelectionRange"),
                }
            ]
        return []
    if isinstance(result, list):
        locations: List[JSON] = []
        for item in result:
            locations.extend(normalize_lsp_locations(item))
        return locations
    return []


def lsp_definition_results(path: Path, position: JSON) -> Tuple[List[JSON], str]:
    route = route_for_path(path)
    backend_name = route.backend.name
    raw = lsp_for(route.backend, route.root).definition(path, position)
    locations = normalize_lsp_locations(raw)
    results: List[JSON] = []
    for loc in locations:
        target_path = uri_to_path(loc["uri"])
        text = read_text(target_path) if target_path.exists() else ""
        selection_range = loc.get("selectionRange") or loc.get("range")
        code = range_text(text, loc.get("range")) if text and loc.get("range") else ""
        if not code and text and selection_range:
            start_line = selection_range.get("start", {}).get("line")
            name_hint = ""
            if start_line is not None:
                try:
                    line_text = text.splitlines()[int(start_line)]
                    name_match = re.search(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", line_text)
                    if name_match:
                        name_hint = name_match.group(1)
                except Exception:
                    pass
            if name_hint:
                code = enclosing_decl_text(target_path, name_hint, start_line)
        results.append(
            {
                "file_path": str(target_path),
                "range": loc.get("range"),
                "selection_range": selection_range,
                "code": code,
                "source": f"{backend_name}-lsp",
                "routing": route.route,
                "lsp_root": str(route.root),
                "limitations": list(route.limitations),
            }
        )
    return results, f"{backend_name}-lsp"


def enclosing_decl_text(path: Path, name: str, start_line: Optional[int] = None) -> str:
    text = read_text(path)
    backend = backend_for_path(path)
    if backend is None:
        return ""
    # Build a name-anchored pattern from the backend's decl regex prefix.
    if backend.name == "swift":
        keyword = r"(class|struct|enum|protocol|actor|extension|func|var|let|typealias)"
        modifiers = r"(?:(?:public|private|fileprivate|internal|open|static|final|actor|indirect|mutating|nonmutating|override|convenience|required)\s+)*"
        attr = r"(?:@\w+(?:\([^)]*\))?[ \t]*(?:\n[ \t]*)?)*"
    else:  # cangjie
        keyword = r"(class|struct|enum|interface|extend|func|var|let|type)"
        modifiers = r"(?:(?:public|private|protected|internal|open|static|mut|override|redef|operator|abstract|sealed|final|foreign|unsafe)\s+)*"
        attr = r"(?:@\w+(?:\[[^\]]*\])?[ \t]*(?:\n[ \t]*)?)*"
    pattern = re.compile(
        r"^[ \t]*" + attr + modifiers + keyword + r"\s+" + re.escape(name) + r"\b",
        re.MULTILINE,
    )
    for match in pattern.finditer(text):
        pos = offset_to_position(text, match.start())
        if start_line is not None and abs(pos["line"] - start_line) > 2:
            continue
        brace = text.find("{", match.end())
        newline = text.find("\n", match.end())
        if brace == -1 or (newline != -1 and newline < brace):
            end = newline if newline != -1 else len(text)
            return text[match.start():end].rstrip()
        depth = 0
        idx = brace
        while idx < len(text):
            char = text[idx]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[match.start(): idx + 1].rstrip()
            idx += 1
    return ""


# ---------------------------------------------------------------------------
# Edit application
# ---------------------------------------------------------------------------

def apply_edits_to_text(text: str, edits: List[JSON]) -> str:
    """Apply a list of LSP TextEdits to text. Edits may overlap by zero width;
    we sort by descending start offset so earlier edits keep stable indices."""
    indexed: List[Tuple[int, int, str]] = []
    for edit in edits:
        rng = edit.get("range") or {}
        start = position_to_offset(text, rng.get("start", {}).get("line", 0), rng.get("start", {}).get("character", 0))
        end = position_to_offset(text, rng.get("end", {}).get("line", 0), rng.get("end", {}).get("character", 0))
        indexed.append((start, end, edit.get("newText", "")))
    indexed.sort(key=lambda triple: (triple[0], triple[1]), reverse=True)
    for start, end, new_text in indexed:
        text = text[:start] + new_text + text[end:]
    return text


def apply_workspace_edit(workspace_edit: JSON) -> List[JSON]:
    """Apply an LSP WorkspaceEdit. Returns a per-file summary."""
    summary: List[JSON] = []
    changes = workspace_edit.get("changes") or {}
    document_changes = workspace_edit.get("documentChanges") or []

    file_edits: Dict[str, List[JSON]] = {}
    for uri, edits in changes.items():
        file_edits.setdefault(uri, []).extend(edits)
    for change in document_changes:
        if "textDocument" in change:
            uri = change["textDocument"].get("uri")
            if uri:
                file_edits.setdefault(uri, []).extend(change.get("edits", []))

    for uri, edits in file_edits.items():
        path = uri_to_path(uri)
        if not path.exists():
            summary.append({"file_path": str(path), "applied": 0, "error": "file not found"})
            continue
        original = read_text(path)
        updated = apply_edits_to_text(original, edits)
        if updated != original:
            write_text(path, updated)
        summary.append({"file_path": str(path), "applied": len(edits), "changed": updated != original})
        # Notify the relevant LSP backend so subsequent semantic queries see the new text.
        try:
            client = lsp_for_path(path)
            client.did_change(path, updated)
        except Exception:
            pass
    return summary


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def tool_get_directory_tree(args: JSON) -> JSON:
    root = resolve_path(args.get("root_path", "."))
    max_depth = args.get("max_depth")
    include_hidden = bool(args.get("include_hidden", False))
    if not root.exists():
        raise FileNotFoundError(str(root))

    def walk(path: Path, depth: int) -> JSON:
        node: JSON = {
            "name": path.name or str(path),
            "path": str(path),
            "type": "directory" if path.is_dir() else "file",
        }
        if path.is_dir() and (max_depth is None or depth < int(max_depth)):
            children = []
            for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                if not include_hidden and child.name.startswith("."):
                    continue
                if child.is_dir() and child.name in EXCLUDED_DIRS:
                    continue
                children.append(walk(child, depth + 1))
            node["children"] = children
        return node

    return {"root": str(root), "tree": walk(root, 0)}


def tool_get_file_structure(args: JSON) -> JSON:
    path = resolve_path(args["file_path"])
    if not path.exists():
        raise FileNotFoundError(str(path))
    backend = backend_for_path(path)
    if backend is None:
        return {"file_path": str(path), "language": "unknown", "symbols": []}
    route = route_for_path(path)
    source = f"{backend.name}-lsp"
    try:
        symbols = flatten_document_symbols(lsp_for(route.backend, route.root).document_symbols(path), path)
    except Exception as exc:
        source = f"regex-fallback: {exc}"
        symbols = regex_symbols(path)
    if not symbols:
        regex_fallback = regex_symbols(path)
        if regex_fallback:
            source = f"{source}+regex-fallback" if source.startswith(backend.name) else source
            symbols = regex_fallback
    grouped: JSON = {
        "classes": [], "structs": [], "enums": [], "protocols": [],
        "interfaces": [], "extensions": [], "functions": [], "variables": [],
        "other": [],
    }
    for symbol in symbols:
        kind = symbol.get("kind")
        target = {
            "class": "classes",
            "struct": "structs",
            "enum": "enums",
            "interface": "interfaces",
            "protocol": "protocols",
            "function": "functions",
            "method": "functions",
            "func": "functions",
            "variable": "variables",
            "constant": "variables",
            "property": "variables",
            "var": "variables",
            "let": "variables",
            "extension": "extensions",
            "extend": "extensions",
        }.get(kind, "other")
        grouped[target].append(symbol)
    imports = extract_imports(path)
    globals_ = extract_top_level_globals(path)
    return {
        "file_path": str(path),
        "language": backend.name,
        "source": source,
        "routing": route.route,
        "lsp_root": str(route.root),
        "limitations": list(route.limitations),
        "imports": imports,
        "globals": globals_,
        "skeleton": {
            "imports": imports,
            "classes": grouped["classes"],
            "functions": grouped["functions"],
            "globals": globals_,
            "structs": grouped["structs"],
        },
        **grouped,
        "symbols": symbols,
    }


def tool_definition(args: JSON) -> JSON:
    symbol_name = args["symbol_name"]
    declarations = find_symbol_declarations(symbol_name)
    lsp_results: List[JSON] = []
    lsp_error = ""
    if declarations:
        decl = declarations[0]
        path = Path(decl["file_path"])
        selection = decl.get("selection_range") or decl.get("range") or {}
        position = selection.get("start", {"line": 0, "character": 0})
        try:
            lsp_results, lsp_source = lsp_definition_results(path, position)
            if lsp_results:
                return {
                    "symbol_name": symbol_name,
                    "definitions": lsp_results,
                    "status": "ok",
                    "source": lsp_source,
                }
        except Exception as exc:
            lsp_error = str(exc)
    results = []
    structural = {"class", "struct", "enum", "protocol", "interface", "actor",
                  "extension", "extend", "func", "function", "method"}
    for decl in declarations:
        path = Path(decl["file_path"])
        text = read_text(path)
        code = ""
        if decl.get("range"):
            code = range_text(text, decl["range"])
        if decl.get("kind") in structural:
            expanded = enclosing_decl_text(
                path,
                str(decl["name"]).split(".")[-1],
                decl.get("selection_range", {}).get("start", {}).get("line"),
            )
            if expanded:
                code = expanded
        if not code:
            code = enclosing_decl_text(
                path,
                str(decl["name"]).split(".")[-1],
                decl.get("selection_range", {}).get("start", {}).get("line"),
            )
        results.append({**decl, "code": code})
    response = {
        "symbol_name": symbol_name,
        "definitions": results,
        "status": "ok" if results else "not_found",
        "source": "text-structure-fallback",
    }
    if lsp_error:
        response["lsp_error"] = lsp_error
    return response


def tool_hover(args: JSON) -> JSON:
    path = resolve_path(args["file_path"])
    position = args["position"]
    route = route_for_path(path)
    source = f"{route.backend.name}-lsp"
    error = ""
    try:
        result = lsp_for(route.backend, route.root).hover(path, position)
        if result is None:
            source = f"{route.backend.name}-lsp-empty"
    except Exception as exc:
        result = None
        source = f"{route.backend.name}-lsp-error"
        error = str(exc)
    response = {
        "file_path": str(path),
        "position": position,
        "hover": result,
        "source": source,
        "routing": route.route,
        "lsp_root": str(route.root),
        "limitations": list(route.limitations),
    }
    if error:
        response["error"] = error
    return response


def text_references(symbol_name: str) -> List[JSON]:
    pattern = re.compile(r"\b" + re.escape(symbol_name) + r"\b")
    refs: List[JSON] = []
    for path in source_files():
        text = read_text(path)
        for line_no, line in enumerate(text.splitlines()):
            for match in pattern.finditer(line):
                refs.append(
                    {
                        "file_path": str(path),
                        "range": {
                            "start": {"line": line_no, "character": match.start()},
                            "end": {"line": line_no, "character": match.end()},
                        },
                        "line_text": line.strip(),
                        "source": "text-fallback",
                    }
                )
    return refs


def tool_references(args: JSON) -> JSON:
    symbol_name = args["symbol_name"]
    declarations = find_symbol_declarations(symbol_name)
    if not declarations:
        return {"symbol_name": symbol_name, "references": text_references(symbol_name), "source": "text-fallback"}
    decl = declarations[0]
    path = Path(decl["file_path"])
    selection = decl.get("selection_range") or decl.get("range") or {}
    position = selection.get("start", {"line": 0, "character": 0})
    route = route_for_path(path)
    backend_name = route.backend.name
    try:
        locations = lsp_for(route.backend, route.root).references(path, position) or []
        refs = []
        for loc in locations:
            refs.append({
                "file_path": str(uri_to_path(loc["uri"])),
                "range": loc.get("range"),
                "source": f"{backend_name}-lsp",
            })
        if not refs:
            return {
                "symbol_name": symbol_name,
                "references": text_references(symbol_name),
                "source": f"{backend_name}-lsp-empty+text-fallback",
                "routing": route.route,
                "lsp_root": str(route.root),
                "limitations": list(route.limitations),
            }
        return {
            "symbol_name": symbol_name,
            "references": refs,
            "source": f"{backend_name}-lsp",
            "routing": route.route,
            "lsp_root": str(route.root),
            "limitations": list(route.limitations),
        }
    except Exception as exc:
        return {
            "symbol_name": symbol_name,
            "references": text_references(symbol_name),
            "source": f"text-fallback: {exc}",
            "routing": route.route,
            "lsp_root": str(route.root),
            "limitations": list(route.limitations),
        }


def tool_diagnostics(args: JSON) -> JSON:
    path = resolve_path(args["file_path"])
    route = route_for_path(path)
    source = f"{route.backend.name}-lsp"
    error = ""
    try:
        diagnostics = lsp_for(route.backend, route.root).diagnostics(path)
    except Exception as exc:
        diagnostics = []
        source = f"{route.backend.name}-lsp-error"
        error = str(exc)
    response = {
        "file_path": str(path),
        "diagnostics": diagnostics,
        "source": source,
        "routing": route.route,
        "lsp_root": str(route.root),
        "limitations": list(route.limitations),
    }
    if error:
        response["error"] = error
    return response


def tool_edit_file(args: JSON) -> JSON:
    """Apply atomic text edits to a file. Edits is a list of LSP TextEdits:
       [{ "range": {start: {line,character}, end: {line,character}}, "newText": "..." }]
    Edits are applied in reverse-order so positions remain valid."""
    path = resolve_path(args["file_path"])
    edits = args.get("edits") or []
    if not isinstance(edits, list) or not edits:
        raise ValueError("edits must be a non-empty list of TextEdit objects")
    if not path.exists():
        raise FileNotFoundError(str(path))
    original = read_text(path)
    updated = apply_edits_to_text(original, edits)
    if updated != original:
        write_text(path, updated)
    # Inform the LSP backend, if any, so subsequent semantic queries are consistent.
    try:
        client = lsp_for_path(path)
        client.did_change(path, updated)
    except Exception:
        pass
    return {
        "file_path": str(path),
        "applied": len(edits),
        "changed": updated != original,
        "bytes_before": len(original),
        "bytes_after": len(updated),
    }


def tool_rename_symbol(args: JSON) -> JSON:
    """Rename a symbol via LSP textDocument/rename and apply the workspace edit.

    Inputs:
      - file_path + position + new_name           (preferred, exact)
      - symbol_name + new_name                    (locates declaration first)
    """
    new_name = args["new_name"]
    if "file_path" in args and "position" in args:
        path = resolve_path(args["file_path"])
        position = args["position"]
    else:
        symbol_name = args["symbol_name"]
        decls = find_symbol_declarations(symbol_name)
        if not decls:
            return {"status": "not_found", "symbol_name": symbol_name}
        decl = decls[0]
        path = Path(decl["file_path"])
        selection = decl.get("selection_range") or decl.get("range") or {}
        position = selection.get("start", {"line": 0, "character": 0})

    client = lsp_for_path(path)
    workspace_edit = client.rename(path, position, new_name) or {}
    summary = apply_workspace_edit(workspace_edit)
    total = sum(item.get("applied", 0) for item in summary)
    return {
        "file_path": str(path),
        "position": position,
        "new_name": new_name,
        "files_changed": summary,
        "total_edits": total,
        "status": "ok" if total else "no_changes",
    }


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOLS: Dict[str, JSON] = {
    "get_directory_tree": {
        "description": "Return the project directory tree. Excludes build artifacts and .git by default.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "root_path": {"type": "string", "description": "Optional path relative to the project root."},
                "max_depth": {"type": "integer", "description": "Optional maximum recursion depth."},
                "include_hidden": {"type": "boolean", "description": "Include hidden files/directories."},
            },
        },
        "handler": tool_get_directory_tree,
    },
    "get_file_structure": {
        "description": "Return declarations in a source file (Swift or Cangjie) grouped by kind.",
        "inputSchema": {"type": "object", "required": ["file_path"], "properties": {"file_path": {"type": "string"}}},
        "handler": tool_get_file_structure,
    },
    "definition": {
        "description": "Return full implementation snippets and locations for declarations matching a symbol name.",
        "inputSchema": {"type": "object", "required": ["symbol_name"], "properties": {"symbol_name": {"type": "string"}}},
        "handler": tool_definition,
    },
    "hover": {
        "description": "Return LSP hover info at a file position. Position is zero-based LSP line/character.",
        "inputSchema": {
            "type": "object",
            "required": ["file_path", "position"],
            "properties": {
                "file_path": {"type": "string"},
                "position": {
                    "type": "object",
                    "required": ["line", "character"],
                    "properties": {"line": {"type": "integer"}, "character": {"type": "integer"}},
                },
            },
        },
        "handler": tool_hover,
    },
    "references": {
        "description": "Return project-wide references for a symbol name via the matching LSP backend, with text fallback.",
        "inputSchema": {"type": "object", "required": ["symbol_name"], "properties": {"symbol_name": {"type": "string"}}},
        "handler": tool_references,
    },
    "diagnostics": {
        "description": "Return LSP diagnostics for a source file (Swift or Cangjie).",
        "inputSchema": {"type": "object", "required": ["file_path"], "properties": {"file_path": {"type": "string"}}},
        "handler": tool_diagnostics,
    },
    "edit_file": {
        "description": "Apply a list of LSP TextEdits to a file atomically. Edits: [{range:{start,end}, newText}].",
        "inputSchema": {
            "type": "object",
            "required": ["file_path", "edits"],
            "properties": {
                "file_path": {"type": "string"},
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["range", "newText"],
                        "properties": {
                            "range": {
                                "type": "object",
                                "required": ["start", "end"],
                                "properties": {
                                    "start": {
                                        "type": "object",
                                        "required": ["line", "character"],
                                        "properties": {"line": {"type": "integer"}, "character": {"type": "integer"}},
                                    },
                                    "end": {
                                        "type": "object",
                                        "required": ["line", "character"],
                                        "properties": {"line": {"type": "integer"}, "character": {"type": "integer"}},
                                    },
                                },
                            },
                            "newText": {"type": "string"},
                        },
                    },
                },
            },
        },
        "handler": tool_edit_file,
    },
    "rename_symbol": {
        "description": "Rename a symbol project-wide via LSP textDocument/rename. Provide either (file_path+position) or symbol_name, plus new_name.",
        "inputSchema": {
            "type": "object",
            "required": ["new_name"],
            "properties": {
                "file_path": {"type": "string"},
                "position": {
                    "type": "object",
                    "properties": {"line": {"type": "integer"}, "character": {"type": "integer"}},
                },
                "symbol_name": {"type": "string"},
                "new_name": {"type": "string"},
            },
        },
        "handler": tool_rename_symbol,
    },
}


# ---------------------------------------------------------------------------
# MCP protocol
# ---------------------------------------------------------------------------

def make_error(message: str, code: int = -32000) -> JSON:
    return {"code": code, "message": message}


def mcp_result(request_id: Any, result: Any) -> JSON:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def mcp_error(request_id: Any, message: str, code: int = -32000) -> JSON:
    return {"jsonrpc": "2.0", "id": request_id, "error": make_error(message, code)}


def content_result(data: Any) -> JSON:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def handle(message: JSON) -> Optional[JSON]:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        return mcp_result(
            request_id,
            {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "sourcekit-analyzer", "version": "0.2.0"},
            },
        )
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        tools = []
        for name, spec in TOOLS.items():
            tools.append({"name": name, "description": spec["description"], "inputSchema": spec["inputSchema"]})
        return mcp_result(request_id, {"tools": tools})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in TOOLS:
            return mcp_error(request_id, f"Unknown tool: {name}", -32602)
        try:
            data = TOOLS[name]["handler"](args)
            return mcp_result(request_id, content_result(data))
        except Exception as exc:
            return mcp_result(
                request_id,
                {"content": [{"type": "text", "text": json.dumps({"error": str(exc)}, ensure_ascii=False)}], "isError": True},
            )
    if method == "ping":
        return mcp_result(request_id, {})
    if request_id is not None:
        return mcp_error(request_id, f"Unsupported method: {method}", -32601)
    return None


def read_mcp_message(stream) -> Optional[JSON]:
    headers: Dict[str, str] = {}
    while True:
        line = stream.readline()
        if line == b"":
            return None
        if line in (b"\r\n", b"\n"):
            break
        raw = line.decode("ascii", errors="replace").strip()
        if ":" in raw:
            key, value = raw.split(":", 1)
            headers[key.lower()] = value.strip()

    length = int(headers.get("content-length", "0"))
    if length <= 0:
        raise ValueError("Missing or invalid Content-Length header")
    body = stream.read(length)
    if len(body) != length:
        raise EOFError("Unexpected EOF while reading MCP message body")
    return json.loads(body.decode("utf-8"))


def write_mcp_message(stream, message: JSON) -> None:
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    stream.write(b"Content-Length: " + str(len(payload)).encode("ascii") + b"\r\n\r\n" + payload)
    stream.flush()


def main() -> None:
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        try:
            message = read_mcp_message(stdin)
            if message is None:
                break
            response = handle(message)
        except Exception as exc:
            response = mcp_error(None, str(exc))
        if response is not None:
            write_mcp_message(stdout, response)


if __name__ == "__main__":
    main()
