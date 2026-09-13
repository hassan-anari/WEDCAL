#!/usr/bin/env bash
set -euo pipefail
: "${DATABASE_URL:?DATABASE_URL must be injected}"
: "${BACKUP_OUTPUT:?BACKUP_OUTPUT must name a protected destination file}"
umask 077
pg_dump --dbname="$DATABASE_URL" --format=custom --no-owner --no-acl --file="$BACKUP_OUTPUT"
pg_restore --list "$BACKUP_OUTPUT" >/dev/null
