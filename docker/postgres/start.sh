#!/bin/sh
# A separately owned, persistent PostgreSQL cluster; never touches SQLite data.
set -eu
umask 077
pg_data=/var/lib/postgresql/data
pg_socket=/var/run/marketing-agents-postgres
pg_options="-c listen_addresses='' -c unix_socket_directories=$pg_socket -c unix_socket_permissions=0770 -c unix_socket_group=marketing -c hba_file=/app/postgres/pg_hba.conf -c ident_file=/app/postgres/pg_ident.conf -c max_connections=60"
test "$(id -u)" = 999
test ! -L "$pg_data"
if [ ! -f "$pg_data/PG_VERSION" ]; then
    # initdb itself refuses nonempty storage, including unknown/incomplete state.
    initdb -D "$pg_data" --username=postgres --auth-local=peer --auth-host=reject --encoding=UTF8 --locale=C
    pg_ctl -D "$pg_data" -o "$pg_options" -w -t 30 start
    trap 'pg_ctl -D "$pg_data" -m fast -w -t 20 stop' EXIT HUP INT TERM
    psql -X -w -v ON_ERROR_STOP=1 -h "$pg_socket" -U postgres -d postgres -f /app/postgres/bootstrap.sql
    pg_ctl -D "$pg_data" -m fast -w -t 20 stop
    trap - EXIT HUP INT TERM
fi
test "$(head -n 1 "$pg_data/PG_VERSION")" = 17
exec postgres -D "$pg_data" \
    -c listen_addresses='' -c unix_socket_directories="$pg_socket" \
    -c unix_socket_permissions=0770 -c unix_socket_group=marketing \
    -c hba_file=/app/postgres/pg_hba.conf -c ident_file=/app/postgres/pg_ident.conf \
    -c max_connections=60
