#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""沪深300 五维打分单元测试。"""
from __future__ import annotations

import os
import sys
import unittest

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # engine/
REPO = os.path.dirname(ROOT)  # quant-data/
sys.path.insert(0, os.path.join(REPO, "scripts"))
import build_hs300_score as S  # noqa: E402


class TestHs300Score(unittest.TestCase):
    def test_percentile_score_range_and_flip(self):
        rng = np.random.default_rng(0)
        raw = pd.Series(rng.normal(0, 1, 800))
        s = S._percentile_score(raw, window=200, flip=False)
        self.assertTrue(s.dropna().between(-1, 1).all())
        # 窗口末端最大值 → 接近 +1
        raw.iloc[-1] = raw.iloc[-200:].max() + 1
        s2 = S._percentile_score(raw, window=200, flip=False)
        self.assertGreater(s2.iloc[-1], 0.9)
        s3 = S._percentile_score(raw, window=200, flip=True)
        self.assertLess(s3.iloc[-1], -0.9)

    def test_percentile_cold_start_forces_zero(self):
        """有效历史 < MIN_HISTORY 时明确记 0，禁止 2 点打出 ±1。"""
        raw = pd.Series([np.nan] * 10 + [0.5, 0.9])
        s = S._percentile_score(raw, window=200, flip=False)
        self.assertEqual(float(s.iloc[-1]), 0.0)
        self.assertEqual(float(s.iloc[-2]), 0.0)
        self.assertTrue(bool(S._cold_start_mask(raw).iloc[-1]))
        # 刚好满门槛后不再强制 0
        raw60 = pd.Series(np.linspace(0, 1, S.MIN_HISTORY))
        s60 = S._percentile_score(raw60, window=200, flip=False)
        self.assertGreater(abs(float(s60.iloc[-1])), 0.0)
        self.assertFalse(bool(S._cold_start_mask(raw60).iloc[-1]))

    def test_adx_signed_runs(self):
        n = 120
        close = pd.Series(np.linspace(100, 130, n) + np.sin(np.linspace(0, 8, n)))
        df = pd.DataFrame({
            "high": close + 1,
            "low": close - 1,
            "close": close,
        })
        adx = S._adx_signed(df, 20)
        self.assertEqual(len(adx), n)
        self.assertTrue(np.isfinite(adx.iloc[-1]))

    def test_nh_ratio_bounds(self):
        high = pd.Series(list(range(40)))
        r = S._nh_ratio(high, 20)
        self.assertAlmostEqual(r.iloc[-1], 1.0)
        high2 = pd.Series(list(range(40, 0, -1)))
        r2 = S._nh_ratio(high2, 20)
        self.assertAlmostEqual(r2.iloc[-1], 0.0)

    def test_payload_schema_if_present(self):
        path = os.path.join(REPO, "data", "payload", "hs300_score.json")
        if not os.path.exists(path):
            self.skipTest("hs300_score.json 尚未生成")
        import json
        with open(path, encoding="utf-8") as f:
            p = json.load(f)
        self.assertIn("snapshot", p)
        snap = p["snapshot"]
        self.assertIn("score", snap)
        self.assertEqual(len(snap["dims"]), 5)
        self.assertEqual(len(snap["indicators"]), 10)
        if snap["score"] is not None:
            self.assertGreaterEqual(snap["score"], -1)
            self.assertLessEqual(snap["score"], 1)


if __name__ == "__main__":
    unittest.main()
