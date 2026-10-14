"""Postgres persistence — anomalies in full, everything else as counters.

`ARCHITECTURE.md` §8.3 specified one year of raw logs partitioned by month. At
200 lines/s that is ~6.3 billion rows on hardware chosen for being cheap; the
policy was never costed against the volume the same document describes. This
module implements the replacement from `design.md` D7:

  - `anomalies`      — flagged records in full, raw line included, kept forever
  - `template_counts` — (template_id, hour, host) totals, kept one year

Normal lines are discarded after scoring. That loses normal-line forensics,
which is a real cost and is documented as one rather than discovered later.
"""

from __future__ import annotations

from dataclasses import dataclass

# Model key, revision and threshold travel with every row, because a bare score
# from an unknown model is not evidence.
#
# Rows are grouped by template rather than written one per flagged line. A single
# mis-scored template produced 4,091 rows from one 20k-line run, and that number
# is a problem twice over: nobody reads 4,091 near-identical findings, and at a
# 21% flag rate the retention design in `design.md` D7 stops holding. So repeats
# of one template on one host in one hour collapse into one row with a count.
#
# What that costs: the individual parameters of the repeats. One exemplar line is
# kept in full, the rest become a number. That is the same shape of trade-off as
# discarding normal lines, and it is why the exception below exists.
ANOMALIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS anomalies (
    id             BIGSERIAL PRIMARY KEY,
    detected_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    host           TEXT,
    agent_id       TEXT,
    source         TEXT,
    log_timestamp  TEXT,
    template_id    TEXT,
    template       TEXT,
    message        TEXT        NOT NULL,
    score          REAL        NOT NULL,
    threshold      REAL        NOT NULL,
    model_key      TEXT        NOT NULL,
    model_revision TEXT        NOT NULL,
    from_cache     BOOLEAN     NOT NULL DEFAULT FALSE
)
"""

# Added separately so an existing table gains them rather than being silently
# left on the old shape by CREATE TABLE IF NOT EXISTS.
ANOMALIES_COLUMNS = [
    "ALTER TABLE anomalies ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ",
    "ALTER TABLE anomalies ADD COLUMN IF NOT EXISTS occurrences BIGINT NOT NULL DEFAULT 1",
    # FALSE means "this row is one specific line and must stay that way".
    "ALTER TABLE anomalies ADD COLUMN IF NOT EXISTS collapsed BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE anomalies ADD COLUMN IF NOT EXISTS hour TIMESTAMPTZ",
    # Null on rows written before these existed; the bridge falls back for those.
    "ALTER TABLE anomalies ADD COLUMN IF NOT EXISTS anomaly_id UUID",
    "ALTER TABLE anomalies ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ",
    "ALTER TABLE anomalies ADD COLUMN IF NOT EXISTS analyzed_at TIMESTAMPTZ",
]

# What makes a replayed spool batch harmless: the id is derived from the batch
# and the position in it, so the same line maps to the same row.
ANOMALIES_ID_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS anomalies_anomaly_id_idx ON anomalies (anomaly_id)
"""

# Partial: only collapsed rows are unique per template/host/hour/model. Rows that
# opted out of grouping are free to repeat, which is the entire point of them.
ANOMALIES_GROUP_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS anomalies_group_idx
    ON anomalies (template_id, host, hour, model_key, model_revision)
    WHERE collapsed
"""

UPSERT_ANOMALY_GROUP = """
INSERT INTO anomalies
    (host, agent_id, source, log_timestamp, template_id, template, message, score, threshold,
     model_key, model_revision, from_cache, occurrences, collapsed, hour, last_seen,
     anomaly_id, ingested_at, analyzed_at)
SELECT %(host)s, %(agent_id)s, %(source)s, %(log_timestamp)s, %(template_id)s, %(template)s,
       %(message)s, %(score)s, %(threshold)s, %(model_key)s, %(model_revision)s, %(from_cache)s,
       %(occurrences)s, TRUE, date_trunc('hour', now()), now(),
       %(anomaly_id)s, %(ingested_at)s, %(analyzed_at)s
-- A replayed batch finds the row it already wrote and adds nothing. ON CONFLICT
-- takes one arbiter index, so it cannot also cover anomaly_id; without this a
-- replay that crosses the hour would violate that index and fail forever.
WHERE NOT EXISTS (SELECT 1 FROM anomalies WHERE anomaly_id = %(anomaly_id)s)
ON CONFLICT (template_id, host, hour, model_key, model_revision) WHERE collapsed
DO UPDATE SET
    -- anomaly_id and both instants stay those of the first occurrence.
    occurrences = anomalies.occurrences + EXCLUDED.occurrences,
    last_seen   = now(),
    -- Keep the worst score seen, not the most recent. A group summarised by
    -- whichever line happened to arrive last would hide its own peak.
    score       = GREATEST(anomalies.score, EXCLUDED.score)
"""

# Partitioned by month so retention is a DROP rather than a DELETE across a
# table the database is also trying to serve reads from.
TEMPLATE_COUNTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS template_counts (
    template_id   TEXT        NOT NULL,
    hour          TIMESTAMPTZ NOT NULL,
    host          TEXT        NOT NULL,
    template      TEXT,
    count         BIGINT      NOT NULL DEFAULT 0,
    anomaly_count BIGINT      NOT NULL DEFAULT 0,
    PRIMARY KEY (template_id, hour, host)
) PARTITION BY RANGE (hour)
"""

# Every record increments a counter, including the ones shed before scoring —
# otherwise a quiet dashboard cannot be told apart from a discarded hour.
UPSERT_COUNT = """
INSERT INTO template_counts (template_id, hour, host, template, count, anomaly_count)
VALUES (%(template_id)s, date_trunc('hour', %(hour)s::timestamptz), %(host)s, %(template)s,
        %(count)s, %(anomaly_count)s)
ON CONFLICT (template_id, hour, host) DO UPDATE
SET count         = template_counts.count + EXCLUDED.count,
    anomaly_count = template_counts.anomaly_count + EXCLUDED.anomaly_count,
    template      = COALESCE(template_counts.template, EXCLUDED.template)
"""

# The ungrouped path: one row, one line, kept whole.
INSERT_ANOMALY = """
INSERT INTO anomalies
    (host, agent_id, source, log_timestamp, template_id, template, message, score, threshold,
     model_key, model_revision, from_cache, occurrences, collapsed, hour, last_seen,
     anomaly_id, ingested_at, analyzed_at)
VALUES (%(host)s, %(agent_id)s, %(source)s, %(log_timestamp)s, %(template_id)s, %(template)s,
        %(message)s, %(score)s, %(threshold)s, %(model_key)s, %(model_revision)s, %(from_cache)s,
        1, FALSE, date_trunc('hour', now()), now(),
        %(anomaly_id)s, %(ingested_at)s, %(analyzed_at)s)
ON CONFLICT (anomaly_id) DO NOTHING
"""

# Analysis latency of every scored record, not only the flagged ones. One row
# per (minute, spool batch) and insert-only, so replaying a batch cannot count
# its samples twice. The bridge sums a minute's rows; `id` is its cursor.
LATENCY_SCHEMA = """
CREATE TABLE IF NOT EXISTS latency_samples (
    id            BIGSERIAL        UNIQUE,
    minute        TIMESTAMPTZ      NOT NULL,
    batch         TEXT             NOT NULL,
    count         BIGINT           NOT NULL,
    sum_ms        DOUBLE PRECISION NOT NULL,
    max_ms        DOUBLE PRECISION NOT NULL,
    bucket_counts BIGINT[]         NOT NULL,
    PRIMARY KEY (minute, batch)
)
"""

INSERT_LATENCY = """
INSERT INTO latency_samples (minute, batch, count, sum_ms, max_ms, bucket_counts)
VALUES (%(minute)s, %(batch)s, %(count)s, %(sum_ms)s, %(max_ms)s, %(bucket_counts)s)
ON CONFLICT (minute, batch) DO NOTHING
"""


@dataclass
class Store:
    dsn: str

    def _connect(self):
        import psycopg

        return psycopg.connect(self.dsn)

    def ensure_schema(self) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(ANOMALIES_SCHEMA)
                for statement in ANOMALIES_COLUMNS:
                    cursor.execute(statement)
                cursor.execute(ANOMALIES_GROUP_INDEX)
                cursor.execute(ANOMALIES_ID_INDEX)
                cursor.execute(LATENCY_SCHEMA)
                cursor.execute(TEMPLATE_COUNTS_SCHEMA)
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS anomalies_detected_at_idx ON anomalies (detected_at DESC)"
                )
            connection.commit()
        self.ensure_partitions()

    def ensure_partitions(self, months_ahead: int = 2) -> list[str]:
        """Create this month's partition and the next few.

        Called at startup and on the retention job. A missing partition is not a
        slow query, it is an insert that fails outright — so they are created
        ahead of time rather than on demand at the moment of the month rollover.
        """
        from psycopg import sql

        created = []
        with self._connect() as connection:
            with connection.cursor() as cursor:
                for offset in range(months_ahead + 1):
                    cursor.execute(
                        """
                        SELECT to_char(date_trunc('month', now()) + (%s || ' month')::interval, 'YYYY_MM'),
                               date_trunc('month', now()) + (%s || ' month')::interval,
                               date_trunc('month', now()) + ((%s + 1) || ' month')::interval
                        """,
                        (offset, offset, offset),
                    )
                    suffix, start, end = cursor.fetchone()
                    name = f"template_counts_{suffix}"
                    # Postgres does not accept bind parameters in DDL, so the
                    # bounds have to be composed as literals. `sql.Literal` does
                    # the quoting rather than an f-string, even though these
                    # values came back from the database a line ago — a DDL path
                    # that concatenates strings is one refactor away from taking
                    # them from somewhere else.
                    cursor.execute(
                        sql.SQL(
                            "CREATE TABLE IF NOT EXISTS {name} PARTITION OF template_counts "
                            "FOR VALUES FROM ({start}) TO ({end})"
                        ).format(
                            name=sql.Identifier(name),
                            start=sql.Literal(start),
                            end=sql.Literal(end),
                        )
                    )
                    created.append(name)
            connection.commit()
        return created

    def drop_old_partitions(self, keep_months: int = 12) -> list[str]:
        """Retention. A DROP, not a DELETE — the point of partitioning."""
        dropped = []
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT c.relname
                    FROM pg_class c
                    JOIN pg_inherits i ON i.inhrelid = c.oid
                    JOIN pg_class p ON p.oid = i.inhparent
                    WHERE p.relname = 'template_counts'
                      AND c.relname < 'template_counts_' ||
                          to_char(date_trunc('month', now()) - (%s || ' month')::interval, 'YYYY_MM')
                    """,
                    (keep_months,),
                )
                for (name,) in cursor.fetchall():
                    cursor.execute(f"DROP TABLE IF EXISTS {name}")
                    dropped.append(name)
            connection.commit()
        return dropped

    def write(self, anomalies: list[dict], counters: list[dict], latency: list[dict] = ()) -> None:
        """One transaction for all of it. Counters and anomalies must not diverge.

        If the anomaly rows land and the counters do not, the dashboard shows
        anomalies against an hour that claims to have seen no traffic.
        """
        if not anomalies and not counters and not latency:
            return

        grouped = [row for row in anomalies if row.get("collapsed", True)]
        individual = [row for row in anomalies if not row.get("collapsed", True)]

        with self._connect() as connection:
            with connection.cursor() as cursor:
                for row in grouped:
                    # One statement at a time rather than executemany: two rows of
                    # the same template in one batch must see each other's insert,
                    # and executemany would have them conflict inside the batch.
                    cursor.execute(UPSERT_ANOMALY_GROUP, row)
                if individual:
                    cursor.executemany(INSERT_ANOMALY, individual)
                if counters:
                    cursor.executemany(UPSERT_COUNT, counters)
                if latency:
                    cursor.executemany(INSERT_LATENCY, latency)
            connection.commit()
