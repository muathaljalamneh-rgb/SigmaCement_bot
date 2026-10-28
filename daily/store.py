"""Postgres storage for the daily files and the built reports.

Slots: cumulative workbooks (QC, truck scale, lab, plan, fan) keep only the latest upload ('*');
the mill production workbook is kept per month ('YYYY-MM'); the DCS and sales files per day."""
import json
import datetime as dt

import psycopg2
from psycopg2.extras import RealDictCursor

PER_DAY = {'process', 'sales'}
PER_MONTH = {'mill_production'}


def slot_for(kind, day):
    if kind in PER_DAY:
        return day.isoformat()
    if kind in PER_MONTH:
        return f'{day.year}-{day.month:02d}'
    return '*'


class Store:
    def __init__(self, dsn, sslmode='require'):
        self.dsn, self.sslmode = dsn, sslmode

    def conn(self):
        return psycopg2.connect(self.dsn, sslmode=self.sslmode)

    def init(self):
        with self.conn() as c, c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_files (
                    kind TEXT, slot TEXT, filename TEXT, data BYTEA,
                    uploaded_at TIMESTAMP DEFAULT NOW(), uploaded_by BIGINT,
                    PRIMARY KEY (kind, slot));
                CREATE TABLE IF NOT EXISTS daily_reports (
                    day DATE PRIMARY KEY, report TEXT, generated_at TIMESTAMP DEFAULT NOW(),
                    notified_at TIMESTAMP);
            """)
            c.commit()

    def save_file(self, kind, day, filename, data, uid=None, now=None):
        slot = slot_for(kind, day)
        with self.conn() as c, c.cursor() as cur:
            cur.execute("""INSERT INTO daily_files (kind, slot, filename, data, uploaded_at, uploaded_by)
                           VALUES (%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (kind, slot) DO UPDATE SET filename=EXCLUDED.filename, data=EXCLUDED.data,
                           uploaded_at=EXCLUDED.uploaded_at, uploaded_by=EXCLUDED.uploaded_by""",
                        (kind, slot, filename, psycopg2.Binary(data), now or dt.datetime.now(), uid))
            c.commit()
        return slot

    def load_file(self, kind, day):
        with self.conn() as c, c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT filename, data, uploaded_at FROM daily_files WHERE kind=%s AND slot=%s",
                        (kind, slot_for(kind, day)))
            r = cur.fetchone()
        if not r:
            return None
        return {'filename': r['filename'], 'data': bytes(r['data']), 'uploaded_at': r['uploaded_at']}

    def file_index(self):
        with self.conn() as c, c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT kind, slot, filename, uploaded_at FROM daily_files ORDER BY uploaded_at DESC")
            return cur.fetchall()

    def save_report(self, day, report):
        with self.conn() as c, c.cursor() as cur:
            cur.execute("""INSERT INTO daily_reports (day, report, generated_at) VALUES (%s,%s,NOW())
                           ON CONFLICT (day) DO UPDATE SET report=EXCLUDED.report, generated_at=NOW()""",
                        (day, json.dumps(report, ensure_ascii=False, default=str)))
            c.commit()

    def load_report(self, day):
        with self.conn() as c, c.cursor() as cur:
            cur.execute("SELECT report FROM daily_reports WHERE day=%s", (day,))
            r = cur.fetchone()
        return json.loads(r[0]) if r else None

    def report_days(self):
        with self.conn() as c, c.cursor() as cur:
            cur.execute("SELECT day FROM daily_reports ORDER BY day DESC")
            return [r[0] for r in cur.fetchall()]

    def mark_notified(self, day):
        with self.conn() as c, c.cursor() as cur:
            cur.execute("UPDATE daily_reports SET notified_at=NOW() WHERE day=%s", (day,))
            c.commit()

    def notified(self, day):
        with self.conn() as c, c.cursor() as cur:
            cur.execute("SELECT notified_at FROM daily_reports WHERE day=%s", (day,))
            r = cur.fetchone()
        return bool(r and r[0])
