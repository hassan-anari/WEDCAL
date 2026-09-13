#!/usr/bin/env bash
set -euo pipefail
: "${RESTORE_DATABASE_URL:?RESTORE_DATABASE_URL must target an isolated restore database}"
: "${BACKUP_INPUT:?BACKUP_INPUT must name a custom-format backup}"
: "${ALLOW_DESTRUCTIVE_ISOLATED_RESTORE:?Set exactly to YES_ISOLATED_TARGET}"
[[ "$ALLOW_DESTRUCTIVE_ISOLATED_RESTORE" == "YES_ISOLATED_TARGET" ]] || exit 64
[[ "$RESTORE_DATABASE_URL" != "${DATABASE_URL:-}" ]] || { echo "Refusing source database restore" >&2; exit 65; }
pg_restore --dbname="$RESTORE_DATABASE_URL" --clean --if-exists --no-owner --no-acl "$BACKUP_INPUT"
