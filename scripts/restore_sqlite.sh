#!/usr/bin/env bash
set -euo pipefail

BACKUP_PATH="${1:?Usage: restore_sqlite.sh BACKUP_PATH [DB_PATH]}"
DB_PATH="${2:-${DATABASE_PATH:-/data/learning.db}}"

if [[ ! -f "$BACKUP_PATH" ]]; then
  echo "Backup not found: $BACKUP_PATH" >&2
  exit 1
fi

mkdir -p "$(dirname "$DB_PATH")"
if [[ -f "$DB_PATH" ]]; then
  cp "$DB_PATH" "$DB_PATH.before-restore-$(date -u +%Y%m%d-%H%M%S)"
fi
cp "$BACKUP_PATH" "$DB_PATH"
echo "Restored $BACKUP_PATH to $DB_PATH"
