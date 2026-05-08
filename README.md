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
- For repository translation workflows, treat `get_directory_tree` and `get_file_structure` as the planning/indexing tools, then call `hover`, `definition`, `references`, and `diagnostics` only for the files and symbols being translated or validated.
- This follows the ReCodeAgent-style tool pattern: project-analysis tools provide compact structure for planning, while LSP tools are used selectively for type information, navigation, and validation rather than as a mandatory full-repository scan.
- If a full structural inventory is needed, prefer `run_analysis.py --no-lsp` first, then run targeted LSP queries on high-value modules or symbols.

## App Root vs Module Root

The server supports two Swift semantic routing modes.

`module-root`
- Used for files under `Modules/*`
- Routes to the nearest `Package.swift` root
- Best choice for SwiftPM package code and semantic-heavy workflows
- Usually provides the most stable `hover`, `definition`, and `diagnostics` results

`app-root`
- Used for app-target files such as `Wallet/*.swift`
- Routes to the repository / build-server root
- Use when you need app composition, app wiring, or Xcode-target context
- Semantic quality depends on the app build-server and indexing state

Agent guidance:
- Choose `module-root` when the task is about package code and a module file is available
- Choose `app-root` when the task is specifically about app-target code or app-level composition
- Always inspect `source`, `routing`, and `limitations` in the response
- If app-root semantic results are weak, empty, or unstable, prefer a module-root path when the same symbol can be analyzed from `Modules/*`

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

## Known Runtime Behavior

Observed on the EUDI iOS wallet repository (`eudi-app-ios-wallet-ui`, revision `4fce3dd6`):

- The MCP stdio layer can initialize, list tools, and serve `tools/call` requests normally.
- `sourcekit-lsp` was available at `/usr/bin/sourcekit-lsp`.
- Single-point semantic calls on `Modules/*` files worked with `source = swift-lsp`; verified tools included `get_file_structure`, `hover`, `diagnostics`, and `definition`.
- A `module-root` query for `Modules/feature-common/Sources/Interactor/BiometryInteractor.swift` routed to `Modules/feature-common` and returned stable Swift LSP results.
- A limited batch run over 25 files with diagnostics and selected symbols completed successfully, but some batched `definition`/document-symbol requests timed out and fell back to structural/text results.
- A full structural run with LSP disabled completed quickly and produced a useful repository inventory, making it the better first step for translation planning.
- The repository root did not contain `buildServer.json`, so app-root/Xcode-target semantic quality should be treated as best-effort unless a valid build server configuration is provided.

Practical guidance:

- Prefer `Modules/*` paths when checking SwiftPM package code.
- Inspect `source`, `routing`, `lsp_root`, and `limitations` on every semantic response before trusting it as LSP-backed.
- Do not interpret fallback output as failure; it is useful for structure and snippets, but less authoritative than `source = swift-lsp` for type-driven translation decisions.
- Avoid full-repository `hover`, `definition`, or `references` precomputation. Use them as on-demand tools for the symbol currently being translated, refactored, or validated.
- Run `diagnostics` after edits or generated translation output, not necessarily before every analysis step.

## Notes

- `get_file_structure` now includes compatibility fields such as `imports`, `globals`, and `skeleton`
- `definition` prefers true LSP definition results and keeps the structural fallback path as backup
- `hover`, `definition`, and `references` are intended for on-demand agent queries, not full-repository precomputation
