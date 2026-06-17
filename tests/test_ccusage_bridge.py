"""Tests for ccusage_bridge: pure transforms, upserts, and ingest orchestration.

These run without Node/ccusage installed — the subprocess layer is stubbed and
the JSON fixtures mirror real ccusage 20.0.x output (camelCase, nested
burnRate/projection on the active block only)."""

import tempfile
import unittest
from pathlib import Path

import ccusage_bridge as cb
from scanner import get_db, init_db

BLOCKS_FIXTURE = {
    "blocks": [
        {"id": "gap-1", "isGap": True},
        {
            "id": "2026-05-12T07:00:00.000Z", "isGap": False, "isActive": False,
            "startTime": "2026-05-12T07:00:00.000Z", "endTime": "2026-05-12T12:00:00.000Z",
            "actualEndTime": "2026-05-12T11:49:52.222Z",
            "tokenCounts": {"inputTokens": 6677, "outputTokens": 146676,
                            "cacheReadInputTokens": 6991052, "cacheCreationInputTokens": 424881},
            "totalTokens": 7569286, "costUSD": 11.44, "models": ["claude-opus-4-8"],
            "burnRate": None, "projection": None,
        },
        {
            "id": "2026-06-17T05:00:00.000Z", "isGap": False, "isActive": True,
            "startTime": "2026-06-17T05:00:00.000Z", "endTime": "2026-06-17T10:00:00.000Z",
            "actualEndTime": "2026-06-17T07:30:00.000Z",
            "tokenCounts": {"inputTokens": 91004, "outputTokens": 887241,
                            "cacheReadInputTokens": 113841619, "cacheCreationInputTokens": 4455635},
            "totalTokens": 119275499, "costUSD": 100.0, "models": ["claude-opus-4-8"],
            "burnRate": {"costPerHour": 35.86, "tokensPerMinute": 763676.4,
                         "tokensPerMinuteForIndicator": 6263.3},
            "projection": {"remainingMinutes": 143, "totalCost": 178.83, "totalTokens": 228481225},
        },
    ]
}

DAILY_FIXTURE = {
    "daily": [{
        "period": "2026-05-12", "inputTokens": 6883, "outputTokens": 304540,
        "cacheReadTokens": 27871959, "cacheCreationTokens": 1382762,
        "totalTokens": 29566144, "totalCost": 35.41, "modelsUsed": ["claude-opus-4-8"],
    }],
    "totals": {},
}


class TestTransforms(unittest.TestCase):
    def test_blocks_skip_gaps(self):
        rows = cb.blocks_to_rows(BLOCKS_FIXTURE, "now")
        self.assertEqual(len(rows), 2)  # gap dropped
        self.assertTrue(all(r["block_id"] != "gap-1" for r in rows))

    def test_blocks_map_fields(self):
        rows = cb.blocks_to_rows(BLOCKS_FIXTURE, "now")
        completed = next(r for r in rows if r["block_id"].startswith("2026-05-12"))
        self.assertEqual(completed["input_tokens"], 6677)
        self.assertEqual(completed["cache_read_tokens"], 6991052)
        self.assertEqual(completed["total_tokens"], 7569286)
        self.assertEqual(completed["cost_usd"], 11.44)
        self.assertEqual(completed["is_active"], 0)
        self.assertIsNone(completed["burn_rate_tpm"])

    def test_active_block_has_burn_rate_and_projection(self):
        rows = cb.blocks_to_rows(BLOCKS_FIXTURE, "now")
        active = next(r for r in rows if r["is_active"] == 1)
        self.assertAlmostEqual(active["burn_rate_tpm"], 763676.4)
        self.assertAlmostEqual(active["burn_rate_cost_per_hour"], 35.86)
        self.assertEqual(active["projected_total_tokens"], 228481225)
        self.assertEqual(active["remaining_minutes"], 143)

    def test_daily_map_period_and_cost(self):
        rows = cb.daily_to_rows(DAILY_FIXTURE, "ccusage-all", "now")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["day"], "2026-05-12")
        self.assertEqual(rows[0]["cost_usd"], 35.41)
        self.assertEqual(rows[0]["cache_read_tokens"], 27871959)
        self.assertEqual(rows[0]["source"], "ccusage-all")

    def test_daily_accepts_codex_style_fields(self):
        codex = {"daily": [{"date": "2026-05-12", "costUSD": 9.99, "inputTokens": 10}]}
        rows = cb.daily_to_rows(codex, "ccusage-codex", "now")
        self.assertEqual(rows[0]["day"], "2026-05-12")
        self.assertEqual(rows[0]["cost_usd"], 9.99)


class TestUpserts(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"
        self.conn = get_db(self.db_path)
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_billing_windows_upsert_idempotent(self):
        rows = cb.blocks_to_rows(BLOCKS_FIXTURE, "t1")
        cb.upsert_billing_windows(self.conn, rows)
        cb.upsert_billing_windows(self.conn, rows)  # second call must not duplicate
        n = self.conn.execute("SELECT COUNT(*) FROM billing_windows").fetchone()[0]
        self.assertEqual(n, 2)

    def test_billing_windows_update_on_conflict(self):
        cb.upsert_billing_windows(self.conn, cb.blocks_to_rows(BLOCKS_FIXTURE, "t1"))
        # Re-ingest the active block with a higher total (window grew)
        grown = {"blocks": [dict(BLOCKS_FIXTURE["blocks"][2], totalTokens=999)]}
        cb.upsert_billing_windows(self.conn, cb.blocks_to_rows(grown, "t2"))
        row = self.conn.execute(
            "SELECT total_tokens, ingested_at FROM billing_windows WHERE is_active=1").fetchone()
        self.assertEqual(row[0], 999)
        self.assertEqual(row[1], "t2")

    def test_daily_upsert_idempotent_per_source(self):
        rows = cb.daily_to_rows(DAILY_FIXTURE, "ccusage-all", "t1")
        cb.upsert_ccusage_daily(self.conn, rows)
        cb.upsert_ccusage_daily(self.conn, rows)
        n = self.conn.execute("SELECT COUNT(*) FROM ccusage_daily_cache").fetchone()[0]
        self.assertEqual(n, 1)


class TestIngestOrchestration(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"

    def test_ingest_unavailable_is_graceful(self):
        rt = {"available": False, "reason": "no node"}
        res = cb.ingest(db_path=self.db_path, verbose=False, rt=rt)
        self.assertEqual(res, {"available": False})

    def test_ingest_populates_tables(self):
        rt = {"available": True, "runner": "npx", "kind": "npx"}
        orig = cb.run_ccusage
        cb.run_ccusage = lambda sub_args, rt=None: (
            BLOCKS_FIXTURE if "blocks" in sub_args else DAILY_FIXTURE)
        try:
            res = cb.ingest(db_path=self.db_path, verbose=False, rt=rt)
        finally:
            cb.run_ccusage = orig
        self.assertEqual(res["available"], True)
        self.assertEqual(res["blocks"], 2)
        self.assertEqual(res["daily"], 1)
        conn = get_db(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM billing_windows").fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ccusage_daily_cache").fetchone()[0], 1)
        conn.close()


class TestP90AndSummary(unittest.TestCase):
    def test_p90_empty_returns_floor(self):
        self.assertEqual(cb.compute_p90_limit([], floor=19000), 19000)
        self.assertEqual(cb.compute_p90_limit([0, None]), 0)

    def test_p90_single_value(self):
        self.assertEqual(cb.compute_p90_limit([500], floor=100), 500)
        self.assertEqual(cb.compute_p90_limit([50], floor=100), 100)

    def test_p90_multiple_is_high_percentile(self):
        vals = list(range(10, 110, 10))  # 10..100
        p90 = cb.compute_p90_limit(vals)
        self.assertGreaterEqual(p90, 90)
        self.assertIsInstance(p90, int)

    def test_summarize_billing_empty(self):
        db = Path(tempfile.mkdtemp()) / "u.db"
        conn = get_db(db); init_db(conn)
        self.assertEqual(cb.summarize_billing(conn), {"available": False})
        conn.close()

    def test_summarize_billing_populated(self):
        db = Path(tempfile.mkdtemp()) / "u.db"
        conn = get_db(db); init_db(conn)
        cb.upsert_billing_windows(conn, cb.blocks_to_rows(BLOCKS_FIXTURE, "t1"))
        conn.commit()
        s = cb.summarize_billing(conn)
        self.assertTrue(s["available"])
        self.assertEqual(s["window_count"], 2)
        self.assertIsNotNone(s["active"])
        self.assertEqual(s["active"]["is_active"], 1)
        # one completed window (total 7,569,286) -> P90 == that value
        self.assertEqual(s["plan_limit_estimate"], 7569286)
        conn.close()


if __name__ == "__main__":
    unittest.main()
