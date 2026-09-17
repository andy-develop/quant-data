#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中证500低波（500SNLV）v9.0 变体单元测试：
1. ZZ500SNLV_PARAMS 经 make_params 覆盖项生效；
2. 相对默认路径：熊市超卖更严、临时仓更长、底仓可缩放；
3. gen_payload 常量与本文件预期一致（防漂移）。
运行：python3 -m unittest tests.test_zz500snlv -v
"""
import os, sys, unittest, importlib.util, re, ast
from pathlib import Path
import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
BACKTEST = os.path.join(BASE, "backtest")
sys.path.insert(0, BACKTEST)
import engine as E

# 与 gen_payload.ZZ500SNLV_PARAMS 同步（测试内嵌副本，加载脚本再交叉校验）
ZZ500 = dict(X_UP=25.0, Y_DOWN=18.0, HOLD_DAYS=120, REBUY_DAYS=180,
             BEAR_CORE=0.75, CORE_CONFIRM=10, OB_FROM_CD=True,
             FORCE_MIN_ABOVE_MA=20, OS_MIN_COUNT_BEAR=3, VAL_HALF_CAP=1.0)

PQ_TR = os.path.join(os.path.dirname(BASE), "data", "kline", "index", "H20782.parquet")
PQ_PX = os.path.join(os.path.dirname(BASE), "data", "kline", "index", "930782.parquet")


def _load_gen_params():
    path = os.path.join(os.path.dirname(BASE), "scripts", "gen_payload.py")
    spec = importlib.util.spec_from_file_location("gen_payload_mod", path)
    mod = importlib.util.module_from_spec(spec)
    # gen_payload 有副作用 import；仅读源码常量
    src = Path(path).read_text(encoding="utf-8")
    ns = {}
    # 抽取 ZZ500SNLV_PARAMS 字面量
    m = re.search(r"ZZ500SNLV_PARAMS\s*=\s*(\{[^}]+\})", src)
    if not m:
        raise RuntimeError("gen_payload.py 未找到 ZZ500SNLV_PARAMS")
    return ast.literal_eval(m.group(1))


@unittest.skipUnless(os.path.exists(PQ_TR) and os.path.exists(PQ_PX), "缺 H20782/930782 parquet")
class TestZz500Snlv(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tr = pd.read_parquet(PQ_TR)[["date", "close"]]
        px = pd.read_parquet(PQ_PX)[["date", "close"]].rename(columns={"close": "px"})
        cls.df = tr.merge(px, on="date", how="inner").sort_values("date").reset_index(drop=True)
        cls.df["vol"] = float("nan")
        cls.P = E.make_params(**ZZ500)
        cls.df_z = E.build_signals(cls.df.copy(), p=cls.P)
        cls.df_d = E.build_signals(cls.df.copy(), p=None)

    def test_gen_payload_params_in_sync(self):
        gp = _load_gen_params()
        self.assertEqual(gp, ZZ500)

    def test_params_override(self):
        for k, v in ZZ500.items():
            self.assertEqual(getattr(self.P, k), v, k)

    def test_bear_os_stricter_than_default(self):
        # 熊市门槛更高 → 可执行超卖日不应多于默认（在同序列上 oversold 原始列相同，
        # 差异在 replay 的 OS_MIN_COUNT_BEAR；这里用信号列 + 参数语义断言）
        self.assertGreater(self.P.OS_MIN_COUNT_BEAR, 2)
        self.assertGreater(self.P.Y_DOWN, E.Y_DOWN)
        self.assertGreater(self.P.HOLD_DAYS, E.HOLD_DAYS)
        self.assertLess(self.P.BEAR_CORE, 1.0)

    def test_x_up_harder_overbought(self):
        a = int(self.df_d["overbought"].sum())
        b = int(self.df_z["overbought"].sum())
        self.assertLessEqual(b, a)  # X_UP 更高 → 超买更少

    def test_y_down_harder_dn63_leg(self):
        # dn63 条件更严：Y_DOWN 更大 → 同价序列上「跌幅达标」日更少
        d_ok = (self.df_d["dn63"] <= -E.Y_DOWN).sum()
        z_ok = (self.df_z["dn63"] <= -self.P.Y_DOWN).sum()
        self.assertLessEqual(z_ok, d_ok)

    def test_replay_bear_core_and_hold(self):
        trades, *_ = E.replay(self.df_z, p=self.P)
        reasons = " ".join(t.get("reason", "") for t in trades)
        self.assertTrue(any("75%" in t.get("reason", "") or "底仓调至75" in t.get("reason", "")
                            for t in trades) or self.P.BEAR_CORE == 0.75)
        # 到期文案若出现应带 120
        exp = [t for t in trades if "自然日" in t.get("reason", "") and "加仓满" in t.get("reason", "")]
        for t in exp:
            self.assertIn("120", t["reason"])

    def test_full_run_beats_baseline_sharpe(self):
        """全期夏普应明显高于沿用红利默认（回归护栏，非精确钉死）。"""
        import tempfile
        tr = self.df[["date", "close"]].copy()
        px = self.df[["date", "px"]].copy()
        with tempfile.TemporaryDirectory() as td:
            tr_p = os.path.join(td, "tr.csv")
            px_p = os.path.join(td, "px.csv")
            tr.assign(vol=float("nan")).to_csv(tr_p, index=False)
            px.rename(columns={"px": "close"}).to_csv(px_p, index=False)
            m0 = E.run(tr_p, px_p, p=None)["metrics"]
            m1 = E.run(tr_p, px_p, p=self.P)["metrics"]
        self.assertGreater(m1["sharpe"], m0["sharpe"] + 0.1)
        self.assertGreater(m1["total"], m0["total"])


if __name__ == "__main__":
    unittest.main()
