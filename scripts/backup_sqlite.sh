#!/usr/bin/env bash
set -euo pipefail

DB_PATH="${1:-${DATABASE_PATH:-/data/learning.db}}"
BACKUP_DIR="${2:-/data/backups}"
KEEP_LAST="${KEEP_LAST_BACKUPS:-14}"

mkdir -p "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
TARGET="$BACKUP_DIR/learning-$STAMP.db"

sqlite3 "$DB_PATH" ".backup '$TARGET'"
find "$BACKUP_DIR" -name 'learning-*.db' -type f -printf '%T@ %p\n' \
  | sort -rn \
  | awk -v keep="$KEEP_LAST" 'NR > keep {print $2}' \
  | xargs -r rm -f

echo "$TARGET"
