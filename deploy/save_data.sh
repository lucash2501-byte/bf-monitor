#!/usr/bin/env bash
# Salva o histórico de preços na branch "data".
# Usa SEMPRE um único commit (force-push): o banco é binário e, com um commit a cada rodada, o repositório
# cresceria sem parar. O histórico em si fica dentro do prices.db, não nos commits.
# Uso: save_data.sh <url-do-repositório> [pasta=data]
set -euo pipefail

REMOTE="${1:?informe a URL do repositório}"
DIR="${2:-data}"

if [ ! -f "$DIR/prices.db" ]; then
  echo "Sem prices.db para salvar."
  exit 0
fi

cd "$DIR"
rm -rf .git
git init -q -b data
git config user.name "bf-monitor"
git config user.email "bf-monitor@users.noreply.github.com"
git add prices.db
git commit -q -m "Histórico de preços $(date -u '+%Y-%m-%d %H:%M UTC')"
git push -q --force "$REMOTE" data:data
echo "Histórico salvo."
