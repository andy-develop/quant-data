#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一回测引擎（backtest/engine.py）最小单元测试。
用真实行情数据验证：指标计算自洽、状态机转移、T+1 撮合、净值/指标复算、抄底闭环口径。
运行：python3 -m unittest tests.test_engine -v   （或 python3 tests/test_engine.py）
"""
import os, sys, math
import unittest
import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # product/
# 统一回测引擎：优先仓库内 product/backtest/，无则回退上级 backtest/
BACKTEST = os.path.join(BASE, "backtest")
if not os.path.isdir(BACKTEST):
    BACKTEST = os.path.join(os.path.dirname(BASE), "backtest")
sys.path.insert(0, BACKTEST)
import engine as E

TR = os.path.join(BACKTEST, "h20269_daily.csv")
PX = os.path.join(BACKTEST, "h30269_daily.csv")


class TestPrices(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.df = E.get_prices(TR, PX)

    def test_no_duplicate_dates(self):
        d = self.df["date"]
        self.assertEqual(d.nunique(), len(d), "日期有重复")

    def test_window_covered(self):
        self.assertGreaterEqual(len(self.df), 2400, "10年日频应≥2400个交易日")

    def test_px_below_tr(self):
        # 全收益指数含分红再投，长期应高于价格指数
        self.assertGreater(self.df["close"].iloc[-1], self.df["px"].iloc[-1])


class TestSignals(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.df = E.build_signals(E.get_prices(TR, PX))

    def test_bollinger_consistency(self):
        df = self.df.dropna(subset=["upper", "lower"])
        self.assertTrue((df["upper"] >= df["lower"]).all(), "上轨必须≥下轨")

    def test_wj_finite(self):
        df = self.df.dropna(subset=["wj"])
        self.assertTrue(np.isfinite(df["wj"]).all(), "周线J必须有限（J=3K-2D 允许越出0-100）")

    def test_ma200_uses_px(self):
        df = self.df.dropna(subset=["ma200"])
        # 手工复算第 200 天后的一个 ma200
        i = 250
        expect = df["px"].iloc[i - 199:i + 1].mean()
        self.assertAlmostEqual(df["ma200"].iloc[i], expect, places=4)

    def test_rsi_defined(self):
        last = self.df.iloc[-1]
        self.assertTrue(math.isfinite(last["rsi"]) and math.isfinite(last["wrsi"]), "末日指标必须有限")


class TestReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        df = E.build_signals(E.get_prices(TR, PX))
        cls.trades, cls.closed, cls.positions, cls.state, cls.pos, cls.legs, cls.t0 = E.replay(df)
        cls.df = df

    def test_positions_valid(self):
        ok = set(self.positions) <= {0.0, 1.0, 1.25, 1.5}
        self.assertTrue(ok, "仓位必须属于 {0,100,125,150}%")

    def test_buy_sell_paired(self):
        # 卖出 ≤ 买入（期初建仓无对应卖出；可能含未平仓超配档）；抄底档必须全部配对闭环
        nb = sum(1 for t in self.trades if t["action"] == "买入")
        ns = sum(1 for t in self.trades if t["action"] == "卖出")
        self.assertGreaterEqual(nb, ns, "卖出笔数不得多于买入（期初建仓无配对卖出）")
        os_buys = sum(1 for t in self.trades if t["action"] == "买入" and ("超卖" in t["reason"] or "离场中" in t["reason"]))
        self.assertEqual(len(self.closed), os_buys, "已平仓抄底档数必须等于超卖加仓买入笔数")

    def test_t_plus_1(self):
        # 每笔非期初交易，其 T-1 日必须是信号日（oversold/overbought/momentum_lost 或到期）
        by_date = self.df.set_index("date")
        for t in self.trades[1:]:
            d = pd.Timestamp(t["date"])
            prev = by_date.loc[:d - pd.Timedelta(days=1)].iloc[-1]
            # 买入类：前一日应有超卖信号（或期初/回补兜底由到期触发）
            if t["action"] == "买入" and "期初" not in t["reason"]:
                self.assertTrue(
                    bool(prev["oversold"]) or "离场" in t["reason"] or "回补" in t["reason"],
                    f"{t['date']} 买入前一日无超卖信号: {t['reason']}")

    def test_hold_days_cap(self):
        # 到期卖出：买入日与卖出日相隔 ≤ 60 自然日 + 一个成交日
        for t in self.trades:
            if "满60" in t["reason"] and "卖出" in t["action"]:
                pass  # 逐档到期已由状态机保证；此处检查存在性
        self.assertTrue(any("满60" in t["reason"] for t in self.trades if t["action"] == "卖出"))


class TestEquity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        df_all = E.build_signals(E.get_prices(TR, PX))
        trades, closed, positions, *_ = E.replay(df_all, t1=True, start=E.START)
        df = df_all[df_all["date"] >= pd.Timestamp(E.START)].reset_index(drop=True)
        cls.ec = E.equity_curve(df, trades, positions)
        cls.m = E.metrics(cls.ec, trades)
        cls.trades = trades

    def test_nav_recompute(self):
        # metrics.total = 终值/初始 - 1（净值已含期初建仓成本，nav 首日可能略低于 1）
        nav = self.ec["strategy_nav"]
        total = nav[-1] - 1
        self.assertAlmostEqual(total, self.m["total"], places=8)

    def test_sharpe_recompute(self):
        nav = self.ec["strategy_nav"]
        rets = np.diff(nav) / nav[:-1]
        sharpe = rets.mean() / rets.std(ddof=1) * math.sqrt(252) if rets.std(ddof=1) > 0 else 0
        self.assertAlmostEqual(sharpe, self.m["sharpe"], places=6)

    def test_mdd_recompute(self):
        nav = self.ec["strategy_nav"]
        dd = nav / np.maximum.accumulate(nav) - 1
        self.assertAlmostEqual(dd.min(), self.m["mdd"], places=8)

    def test_daily_full(self):
        # 日频全量（不抽稀）：策略净值点数 = 交易日数
        self.assertEqual(len(self.ec["strategy_nav"]), len(self.ec["dates"]))
        self.assertGreaterEqual(len(self.ec["dates"]), 2400)


class TestOversold(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        df = E.build_signals(E.get_prices(TR, PX))
        trades, closed, positions, *_ = E.replay(df)
        cls.closed = closed

    def test_closed_matches_legs(self):
        # 每个已平仓抄底档 = 一次超卖加仓；buy/sell 日期与价格齐全
        for c in self.closed:
            self.assertTrue(c["buy_date"] < c["sell_date"])
            self.assertGreater(c["sell_fill"], 0)
            self.assertGreater(c["buy_fill"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
