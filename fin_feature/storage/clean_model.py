"""v5 schema for immutable, version-backed cleaning results."""

DDL_CLEAN_STATEMENT = """
CREATE TABLE IF NOT EXISTS clean_statement (
    clean_id TEXT NOT NULL PRIMARY KEY CHECK(length(clean_id)=64 AND clean_id NOT GLOB '*[^0-9a-f]*'),
    record_ref TEXT NOT NULL REFERENCES statement_version(version_id),
    config_hash TEXT NOT NULL CHECK(length(config_hash)=64 AND config_hash NOT GLOB '*[^0-9a-f]*'),
    config_snapshot TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('kept', 'dropped')),
    standardized_fields TEXT NOT NULL,
    compatibility_fields TEXT NOT NULL,
    diagnostics TEXT NOT NULL,
    UNIQUE(record_ref, config_hash)
)
"""

DDL_CLEAN_SELECTION = """
CREATE TABLE IF NOT EXISTS clean_selection (
    secucode TEXT NOT NULL,
    report_date TEXT NOT NULL,
    table_type TEXT NOT NULL CHECK(table_type IN ('financial_indicator','income_statement','balance_sheet')),
    clean_ref TEXT NOT NULL REFERENCES clean_statement(clean_id),
    PRIMARY KEY(secucode, report_date, table_type)
)
"""

CLEAN_TABLES = ('clean_financial_indicator', 'clean_income_statement', 'clean_balance_sheet')
CLEAN_RESULT_COLUMNS = ('clean_id', 'record_ref', 'config_hash', 'config_snapshot', 'status',
                        'standardized_fields', 'compatibility_fields', 'diagnostics')
REQUIRED_COLUMNS = {
    'clean_selection': {'secucode', 'report_date', 'table_type', 'clean_ref'},
    'clean_statement': {'clean_id', 'record_ref', 'config_hash', 'config_snapshot',
                        'status', 'standardized_fields', 'compatibility_fields', 'diagnostics'},
    **{table: {'record_ref', 'clean_ref'} for table in CLEAN_TABLES},
}


def migrate_v5_clean_lineage(conn):
    from fin_feature.storage.migrations import _ensure_column_exists
    from fin_feature.storage.schema import (
        DDL_CLEAN_FINANCIAL_INDICATOR, DDL_CLEAN_INCOME_STATEMENT, DDL_CLEAN_BALANCE_SHEET,
    )
    # Some accepted v2/v3 drafts contain only stage-2 tables. Complete missing projections here.
    for ddl in (DDL_CLEAN_FINANCIAL_INDICATOR, DDL_CLEAN_INCOME_STATEMENT, DDL_CLEAN_BALANCE_SHEET):
        conn.execute(ddl)
    conn.execute(DDL_CLEAN_STATEMENT)
    conn.execute(DDL_CLEAN_SELECTION)
    for table in CLEAN_TABLES:
        _ensure_column_exists(conn, table, 'record_ref', 'TEXT REFERENCES statement_version(version_id)')
        _ensure_column_exists(conn, table, 'clean_ref', 'TEXT REFERENCES clean_statement(clean_id)')


def verify_v5_structure(conn):
    import re
    from fin_feature.cleaning.pipeline import CLEAN_FIELDS
    gaps = []
    for table, required in REQUIRED_COLUMNS.items():
        found = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
        gaps.extend(f'{table}.{col}' for col in sorted(required - found))
    for kind, required in CLEAN_FIELDS.items():
        found = {row[1] for row in conn.execute(f'PRAGMA table_info(clean_{kind})')}
        gaps.extend(f'clean_{kind}.{col}' for col in sorted(set(required) - found))
    expected_keys = {'clean_statement': ('clean_id',), 'clean_selection': ('secucode','report_date','table_type')}
    for table, expected in expected_keys.items():
        info = list(conn.execute(f'PRAGMA table_info({table})'))
        actual = tuple(r[1] for r in sorted(info, key=lambda r:r[5]) if r[5])
        if actual != expected:
            gaps.append(f'{table}: primary key')
    unique = set()
    for idx in conn.execute('PRAGMA index_list(clean_statement)'):
        if idx[2] and not idx[4]:
            safe_name = idx[1].replace('"', '""')
            unique.add(tuple(r[2] for r in conn.execute(f'PRAGMA index_info("{safe_name}")')))
    if ('record_ref','config_hash') not in unique:
        gaps.append('clean_statement: unique(record_ref,config_hash)')
    expected_fks = {
        'clean_statement': {('record_ref','statement_version','version_id')},
        'clean_selection': {('clean_ref','clean_statement','clean_id')},
        **{table: {('record_ref','statement_version','version_id'), ('clean_ref','clean_statement','clean_id')} for table in CLEAN_TABLES},
    }
    for table, required in expected_fks.items():
        found = {(r[3],r[2],r[4]) for r in conn.execute(f'PRAGMA foreign_key_list({table})')}
        if not required <= found:
            gaps.append(f'{table}: lineage foreign keys')
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='clean_statement'").fetchone()
    compact = re.sub(r'\s+', '', sql[0]).lower() if sql else ''
    if "check(statusin('kept','dropped'))" not in compact:
        gaps.append('clean_statement: status constraint')
    for field in ('clean_id', 'config_hash'):
        if f"check(length({field})=64and{field}notglob'*[^0-9a-f]*')" not in compact:
            gaps.append(f'clean_statement: {field} constraint')
    if gaps:
        raise RuntimeError('v5 cleaning structure incomplete: ' + ', '.join(gaps))
