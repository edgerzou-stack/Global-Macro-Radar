"""Read-time exclusion for legacy rows copied into quarantine evidence.

The helper intentionally parses primary-key JSON in Python. It does not rely
on SQLite JSON1 and never interpolates untrusted table names or JSON values.
"""

import json


SUPPORTED_SOURCES = {
    "portfolio": "id",
    "trade_history": "id",
}


class QuarantineMetadataError(RuntimeError):
    """Raised when quarantine metadata cannot identify an exact legacy row."""


def quarantined_row_ids(connection, source_table):
    """Return exact integer primary keys quarantined for a supported table.

    A missing ``quarantine_rows`` table means the release has not introduced
    quarantine yet, so callers retain the legacy behavior unchanged.
    """
    if source_table not in SUPPORTED_SOURCES:
        raise ValueError(f"Unsupported quarantine source table: {source_table!r}")

    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        ("quarantine_rows",),
    ).fetchone()
    if not exists:
        return frozenset()

    primary_key = SUPPORTED_SOURCES[source_table]
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
        if not isinstance(payload, dict) or set(payload) != {primary_key}:
            raise QuarantineMetadataError(
                f"source_pk_json for {source_table} must contain only {primary_key!r}"
            )
        identifier = payload[primary_key]
        if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier <= 0:
            raise QuarantineMetadataError(
                f"Invalid integer primary key for quarantined {source_table} row"
            )
        identifiers.add(identifier)
    return frozenset(identifiers)


def quarantine_exclusion(connection, source_table):
    """Return a safe SQL suffix and the exact excluded ID set.

    Primary keys are strictly validated Python integers and the identifier
    column comes from ``SUPPORTED_SOURCES``. Rendering validated integers
    avoids SQLite's parameter-count limit while remaining injection-safe.
    """
    identifiers = quarantined_row_ids(connection, source_table)
    if not identifiers:
        return "", identifiers
    id_column = SUPPORTED_SOURCES[source_table]
    integer_literals = ",".join(str(value) for value in sorted(identifiers))
    return f" AND {id_column} NOT IN ({integer_literals})", identifiers
