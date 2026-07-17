"""Read-time exclusion for legacy rows copied into quarantine evidence.

The helper intentionally parses primary-key JSON in Python. It does not rely
on SQLite JSON1 and never interpolates untrusted table names or JSON values.
"""

import json
import re


SUPPORTED_SOURCES = {
    "portfolio": ("id",),
    "trade_history": ("id",),
    "portfolio_snapshots": ("id",),
    "strategy_daily_results": ("id",),
    "strategy_accounts": ("strategy_id",),
    "strategy_nav_history": ("date", "strategy_id"),
}

INTEGER_ID_SOURCES = {
    "portfolio",
    "trade_history",
    "portfolio_snapshots",
    "strategy_daily_results",
}
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class QuarantineMetadataError(RuntimeError):
    """Raised when quarantine metadata cannot identify an exact legacy row."""


def _table_exists(connection, table):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _validate_primary_key(source_table, payload):
    columns = SUPPORTED_SOURCES[source_table]
    if not isinstance(payload, dict) or set(payload) != set(columns):
        raise QuarantineMetadataError(
            f"source_pk_json for {source_table} must contain exactly {columns!r}"
        )
    values = tuple(payload[column] for column in columns)
    if source_table in INTEGER_ID_SOURCES:
        identifier = values[0]
        if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier <= 0:
            raise QuarantineMetadataError(
                f"Invalid integer primary key for quarantined {source_table} row"
            )
    elif any(value is None or isinstance(value, (dict, list, bool)) for value in values):
        raise QuarantineMetadataError(
            f"Invalid primary key for quarantined {source_table} row"
        )
    return values


def quarantined_primary_keys(connection, source_table):
    """Return exact primary-key tuples quarantined for a supported table.

    A missing ``quarantine_rows`` table means the release has not introduced
    quarantine yet, so callers retain the legacy behavior unchanged.
    """
    if source_table not in SUPPORTED_SOURCES:
        raise ValueError(f"Unsupported quarantine source table: {source_table!r}")

    if _table_exists(connection, "quarantine_key_index"):
        columns = SUPPORTED_SOURCES[source_table]
        try:
            rows = connection.execute(
                "SELECT key_arity, key_1, key_2, source_pk_json "
                "FROM quarantine_key_index WHERE source_table=?",
                (source_table,),
            ).fetchall()
        except Exception as error:
            raise QuarantineMetadataError(
                "quarantine_key_index exists but its required schema is unavailable"
            ) from error
        identifiers = set()
        for key_arity, key_1, key_2, raw_payload in rows:
            try:
                payload = json.loads(raw_payload)
            except (TypeError, ValueError) as error:
                raise QuarantineMetadataError(
                    f"Invalid source_pk_json for {source_table}"
                ) from error
            values = _validate_primary_key(source_table, payload)
            expected_text = tuple(str(value) for value in values)
            indexed_text = (key_1,) if key_arity == 1 else (key_1, key_2)
            if key_arity != len(columns) or indexed_text != expected_text:
                raise QuarantineMetadataError(
                    f"Normalized quarantine key mismatch for {source_table}"
                )
            identifiers.add(values)
        return frozenset(identifiers)

    if not _table_exists(connection, "quarantine_rows"):
        return frozenset()

    try:
        payloads = connection.execute(
            "SELECT source_pk_json FROM quarantine_rows WHERE source_table=?",
            (source_table,),
        ).fetchall()
    except Exception as error:
        raise QuarantineMetadataError(
            "quarantine_rows exists but its required schema is unavailable"
        ) from error

    identifiers = set()
    for (raw_payload,) in payloads:
        try:
            payload = json.loads(raw_payload)
        except (TypeError, ValueError) as error:
            raise QuarantineMetadataError(
                f"Invalid source_pk_json for {source_table}"
            ) from error
        identifiers.add(_validate_primary_key(source_table, payload))
    return frozenset(identifiers)


def quarantined_row_ids(connection, source_table):
    """Return integer row IDs for supported single-integer-key legacy tables."""
    if source_table not in INTEGER_ID_SOURCES:
        raise ValueError(f"{source_table!r} does not use a single integer row ID")
    return frozenset(key[0] for key in quarantined_primary_keys(connection, source_table))


def quarantine_filter(connection, source_table, table_alias=None):
    """Return ``(sql_suffix, parameters, keys)`` for active-row queries.

    Integer IDs are rendered only after strict type validation, avoiding
    SQLite's parameter-count limit for large duplicate quarantines. Text and
    composite keys remain bound parameters. The suffix always starts with
    `` AND `` so callers can append it to an existing WHERE clause.
    """

    if source_table not in SUPPORTED_SOURCES:
        raise ValueError(f"Unsupported quarantine source table: {source_table!r}")
    if table_alias is not None and not SAFE_IDENTIFIER.fullmatch(table_alias):
        raise ValueError(f"Unsafe quarantine table alias: {table_alias!r}")

    keys = quarantined_primary_keys(connection, source_table)
    if not keys:
        return "", (), keys
    columns = SUPPORTED_SOURCES[source_table]
    prefix = f"{table_alias}." if table_alias else ""

    if source_table in INTEGER_ID_SOURCES:
        integer_literals = ",".join(str(key[0]) for key in sorted(keys))
        return (
            f" AND {prefix}{columns[0]} NOT IN ({integer_literals})",
            (),
            keys,
        )

    if len(columns) == 1:
        ordered = sorted(keys, key=lambda key: str(key[0]))
        placeholders = ",".join("?" for _ in ordered)
        return (
            f" AND {prefix}{columns[0]} NOT IN ({placeholders})",
            tuple(key[0] for key in ordered),
            keys,
        )

    ordered = sorted(keys, key=lambda key: tuple(str(value) for value in key))
    clauses = []
    parameters = []
    for key in ordered:
        clauses.append(
            "(" + " AND ".join(f"{prefix}{column}=?" for column in columns) + ")"
        )
        parameters.extend(key)
    return f" AND NOT ({' OR '.join(clauses)})", tuple(parameters), keys


def quarantine_exclusion(connection, source_table):
    """Return a safe SQL suffix and the exact excluded ID set.

    Primary keys are strictly validated Python integers and the identifier
    column comes from ``SUPPORTED_SOURCES``. Rendering validated integers
    avoids SQLite's parameter-count limit while remaining injection-safe.
    """
    suffix, parameters, keys = quarantine_filter(connection, source_table)
    if parameters:
        raise ValueError(
            f"quarantine_exclusion only supports integer row IDs: {source_table!r}"
        )
    return suffix, frozenset(key[0] for key in keys)
