#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

: "${SOURCEKIT_LSP:=sourcekit-lsp}"
export SOURCEKIT_LSP

SAFE_TMP="${SOURCEKIT_ANALYZER_TMPDIR:-/private/tmp}"
UNIQUE_ID="$$"

if [ "${SOURCEKIT_ANALYZER_LSP_HOME:-}" != "system" ]; then
  : "${SOURCEKIT_ANALYZER_LSP_HOME:=${SAFE_TMP%/}/sourcekit-lsp-home-${UNIQUE_ID}}"
  export SOURCEKIT_ANALYZER_LSP_HOME
fi

if [ -z "${SOURCEKIT_LSP_ARGS:-}" ]; then
  export SOURCEKIT_LSP_ARGS="--scratch-path ${SAFE_TMP%/}/sourcekit-lsp-scratch-${UNIQUE_ID}"
fi

if [ -n "${SOURCEKIT_ANALYZER_LSP_HOME:-}" ] && [ "${SOURCEKIT_ANALYZER_LSP_HOME}" != "system" ]; then
  mkdir -p "${SOURCEKIT_ANALYZER_LSP_HOME}"
fi

: "${SOURCEKIT_ANALYZER_ROOT:=$PWD}"
export SOURCEKIT_ANALYZER_ROOT

exec python3 "$HERE/server.py" "$@"
