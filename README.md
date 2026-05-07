# MCP SourceKit Analyzer

A standalone stdio MCP server that exposes Swift project-analysis and SourceKit-LSP semantic tools.

## Public v1

This repository packages a standalone MCP server for Swift code understanding and editing.

Public v1 goal:
- Provide the same 6 LSP-style function tools described in the paper
- Provide the 2 project-analysis tools used to bootstrap agent understanding
- Prefer stable `module-root` semantic routing for SwiftPM modules
- Preserve fallback behavior and expose fallback status in responses

Public v1 non-goal:
- Guarantee that Xcode app-root semantic results are identical to ideal full-workspace indexing results
- Precompute repository-wide `hover` / `definition` / `references` for every symbol

Read-only tools:
- `get_directory_tree`
- `get_file_structure`
- `definition`
- `hover`
- `references`
- `diagnostics`

Mutating tools:
- `edit_file`
- `rename_symbol`

## Capability Matrix

Stable in public v1:
- `get_directory_tree`
- `get_file_structure`
- `diagnostics`
- `definition` with LSP-first and structural fallback

Stable for `Modules/*` package roots:
- `hover`
- `references` when SourceKit-LSP returns semantic results
- `rename_symbol` and `edit_file` as execution tools

Best-effort for app-root / Xcode target files such as `Wallet/*.swift`:
- `hover`
- `references`
- `definition` when cross-module semantic context depends on full app-target indexing

Always inspect these response fields:
- `source`
- `routing`
- `lsp_root`
- `limitations`

Swift routing behavior:
- Files under `Modules/*` route to the nearest `Package.swift` root.
- Each module root reuses its own SourceKit-LSP client.
- App files such as `Wallet/*.swift` stay on the app/build-server root.
- Responses expose `source`, `routing`, `lsp_root`, and `limitations`.
- `definition` now prefers real LSP definition results and falls back to structural/text lookup when semantic resolution is unavailable.

## Requirements

- macOS with Xcode / Swift toolchain installed
- `sourcekit-lsp` available on `PATH` or passed via `SOURCEKIT_LSP`
- For Xcode app targets, a working `buildServer.json` in the analyzed repository root

## Repository Layout Expectations

Best results today come from repositories that look like one of these:
- SwiftPM package roots with `Package.swift`
- Xcode app roots with `buildServer.json`
- Mixed app + `Modules/*` repositories where package code can be routed module-by-module

## Install

Clone this repository anywhere on your machine:

```sh
git clone <your-repo-url> /path/to/mcp-sourcekit-analyzer
```

No Python package install step is required. The server uses only the Python standard library.

## Start The Server

Analyze the current working directory:

```sh
SOURCEKIT_ANALYZER_ROOT=/path/to/swift/repo \
  /path/to/mcp-sourcekit-analyzer/launch.sh
```

Use the real user HOME instead of a temporary sandbox-safe HOME:

```sh
SOURCEKIT_ANALYZER_LSP_HOME=system \
SOURCEKIT_ANALYZER_ROOT=/path/to/swift/repo \
  /path/to/mcp-sourcekit-analyzer/launch.sh
```

The process will stay running and wait for MCP stdio requests. That is expected.

## Connect This MCP To A Client

Add an MCP server entry to your MCP-capable client configuration.

Example:

```json
{
  "mcpServers": {
    "sourcekit-analyzer": {
      "command": "/Users/gloria/huawei/mcp-sourcekit-analyzer/launch.sh",
      "env": {
        "SOURCEKIT_ANALYZER_ROOT": "/path/to/swift/repo",
        "SOURCEKIT_ANALYZER_LSP_HOME": "system"
      }
    }
  }
}
```

After the client reloads, the server will expose:
- `get_directory_tree`
- `get_file_structure`
- `definition`
- `hover`
- `references`
- `diagnostics`
- `edit_file`
- `rename_symbol`

## Recommended Agent Usage

Use the tools in this order:

1. `get_directory_tree`
2. `get_file_structure`
3. `hover` / `definition` / `references` only when needed
4. `diagnostics` after local edits or translation output
5. `edit_file` / `rename_symbol` only for concrete changes

Recommended pattern:
- Do lightweight project analysis first
- Use semantic tools lazily, driven by the current symbol under work
- Do not precompute `hover` / `definition` / `references` for every symbol in a medium or large repository

## App Root vs Module Root

Prefer `module-root` whenever the file belongs to `Modules/*`:
- More stable SourceKit-LSP results
- Cleaner SwiftPM build context
- Better fit for on-demand symbol resolution

Use `app-root` only when you need app-target context:
- `Wallet/*.swift`
- Xcode-target-only files
- app wiring and composition code

If `app-root` semantic results are unstable:
- keep the request
- inspect `source` and `limitations`
- retry from a package file when the same symbol also exists in `Modules/*`

## Response Semantics

The server is explicit about quality and routing:

- `source = swift-lsp`
  - semantic result came from SourceKit-LSP
- `source = swift-lsp-empty`
  - semantic request succeeded but returned no semantic payload
- `source = swift-lsp-empty+text-fallback`
  - semantic request returned nothing, then fallback results were used
- `source = text-structure-fallback` or `regex-fallback: ...`
  - semantic path failed and the server returned a structural fallback

## Publishing Boundary

Public v1 release statement:
- This MCP server is suitable for agent-driven Swift repository exploration and targeted semantic queries.
- `Modules/*` package code is the primary supported semantic path.
- Xcode app-root semantic behavior is available on a best-effort basis and must not be advertised as equivalent to full IDE-quality workspace semantics.
- Fallback states are part of the public contract and should be surfaced to downstream agents.

## Troubleshooting

If the server starts but no semantic results appear:
- verify `sourcekit-lsp` is on `PATH`
- verify `SOURCEKIT_ANALYZER_ROOT` points to the intended repository
- for Xcode apps, verify `buildServer.json` exists and matches your active build environment
- try `SOURCEKIT_ANALYZER_LSP_HOME=system` outside sandboxed environments
- prefer `Modules/*` package files to confirm SourceKit-LSP behavior first

If app-root behavior is weaker than module-root behavior:
- this is a known public v1 limitation
- keep using `module-root` for semantic-heavy workflows

## Notes

- `get_file_structure` now includes compatibility fields such as `imports`, `globals`, and `skeleton`
- `definition` prefers true LSP definition results and keeps the structural fallback path as backup
- `hover`, `definition`, and `references` are intended for on-demand agent queries, not full-repository precomputation
