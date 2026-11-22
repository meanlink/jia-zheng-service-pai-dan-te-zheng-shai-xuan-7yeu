from pathlib import Path

from .db import transaction

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / 'migrations'

# dispatch_plans columns for pinned analyses. MySQL has no ADD COLUMN IF NOT
# EXISTS, so they are ensured via information_schema instead of the SQL file.
PLAN_COLUMNS = {
    'batch_id': 'INT NULL',
    'scope_snapshot': 'JSON NULL',
    'invalidation_reason': 'VARCHAR(255) NULL',
}


def apply_sql(cur, filename):
    migration = MIGRATIONS_DIR / filename
    for statement in migration.read_text(encoding='utf-8').split(';'):
        if statement.strip():
            cur.execute(statement)


def initialize_fieldwork():
    """Apply additive DDL to both fresh databases and existing dispatch volumes."""
    with transaction() as cur:
        apply_sql(cur, '002_fieldwork.sql')


def initialize_analysis():
    """Create the sample-batch table and pin columns on dispatch_plans."""
    with transaction() as cur:
        apply_sql(cur, '003_analysis.sql')
        for column, definition in PLAN_COLUMNS.items():
            cur.execute('''SELECT COUNT(*) AS n FROM information_schema.COLUMNS
                           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='dispatch_plans' AND COLUMN_NAME=%s''',
                        (column,))
            if cur.fetchone()['n'] == 0:
                cur.execute(f'ALTER TABLE dispatch_plans ADD COLUMN {column} {definition}')
        cur.execute('''SELECT COUNT(*) AS n FROM information_schema.TABLE_CONSTRAINTS
                       WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='dispatch_plans'
                         AND CONSTRAINT_TYPE='FOREIGN KEY' AND CONSTRAINT_NAME='fk_plan_batch' ''')
        if cur.fetchone()['n'] == 0:
            cur.execute('''ALTER TABLE dispatch_plans ADD CONSTRAINT fk_plan_batch
                           FOREIGN KEY (batch_id) REFERENCES analysis_batches(id)''')
