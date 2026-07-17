#!/bin/sh
set -eu

source_root=/opt/gmr/quant-strategy
runtime_root=/app/quant-strategy
data_root=/data/quant-strategy

mkdir -p "$runtime_root" "$data_root"
cp -R "$source_root"/. "$runtime_root"/

# SQLite must live in a writable directory so WAL/SHM creation is reliable.
# Keeping this canonical in-tree name as a symlink preserves the production
# path guard while all database bytes remain under the host /data mount.
rm -f "$runtime_root/quant_system.db" \
  "$runtime_root/ticker_names.json" \
  "$runtime_root/scripts/core/a_share_map_cache.json"
ln -s "$data_root/quant_system.db" "$runtime_root/quant_system.db"
ln -s "$data_root/ticker_names.json" "$runtime_root/ticker_names.json"
ln -s "$data_root/a_share_map_cache.json" \
  "$runtime_root/scripts/core/a_share_map_cache.json"

exec python /app/scheduler.py "$@"
