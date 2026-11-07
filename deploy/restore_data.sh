#!/usr/bin/env bash
# Baixa o histórico de preços (branch "data") para a pasta indicada.
# Uso: restore_data.sh <url-do-repositório> [pasta=data]
set -euo pipefail

REMOTE="${1:?informe a URL do repositório}"
DIR="${2:-data}"

rm -rf "$DIR"
if git ls-remote --exit-code --heads "$REMOTE" data >/dev/null 2>&1; then
  git clone --quiet --depth 1 --branch data "$REMOTE" "$DIR"
  echo "Histórico restaurado ($(du -h "$DIR/prices.db" 2>/dev/null | cut -f1))."
else
  mkdir -p "$DIR"
  echo "Branch 'data' ainda não existe: começando um histórico novo."
fi
