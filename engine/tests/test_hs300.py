#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""沪深300 择时变体（v8.0）单元测试：
1. make_params 默认值 = 红利低波模块常量（保证同构，不污染默认路径）；
2. 3 参数覆盖（X_UP=15 / Y_DOWN=20 / HOLD_DAYS=120）生效且其余结构件原样；
3. p=None 与显式默认参数结果逐列一致（参数化对红利低波零回归）；
4. 变体信号语义：X_UP 更低 → 超买更易触发；Y_DOWN 更深 → 超卖更难触发；
5. replay 到期文案携带 HOLD_DAYS=120；build_snapshot 的 param 反映传入参数；
6. hs300_update 参数定义正确。
运行：python3 -m unittest tests.test_hs300 -v
"""
import os, sys, unittest, datetime
import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
BACKTEST = os.path.join(BASE, "backtest")
if not os.path.isdir(BACKTEST):
    BACKTEST = os.path.join(os.path.dirname(BASE), "backtest")
sys.path.insert(0, BACKTEST)   # 必须后插（优先）：仓库根存在旧 engine.py，避免误加载
import engine as E
import update as U

TR = os.path.join(BACKTEST, "h20269_daily.csv")
PX = os.path.join(BACKTEST, "h30269_daily.csv")
PARAM_NAMES = ("HOLD_DAYS", "REBUY_DAYS", "J_LOW", "J_HIGH", "J_CROSS_FROM", "J_CROSS_TO",
               "RSI_OS", "RSI_CROSS_FROM", "RSI_CROSS_TO", "X_UP", "Y_DOWN", "Y_ACC",
               "DIV_2OF3", "DELAY_SELL", "VAL_GATE", "MA250_GATE", "WEEK_J0", "VAL_WIN",
               "MAX_POS", "START", "SLIPPAGE_BPS", "FEE_RATE", "FEE_MIN", "FIN_RATE", "TRADING_DAYS")


class TestMakeParams(unittest.TestCase):
    def test_default_matches_module_constants(self):
        P = E.make_params()
        for k in PARAM_NAMES:
            self.assertEqual(getattr(P, k), getattr(E, k), f"{k} 默认值应与模块常量一致")

    def test_hs300_overrides_only_three(self):
        P = E.make_params(X_UP=15.0, Y_DOWN=20.0, HOLD_DAYS=120)
        self.assertEqual(P.X_UP, 15.0)
        self.assertEqual(P.Y_DOWN, 20.0)
        self.assertEqual(P.HOLD_DAYS, 120)
        # 其余结构件原样保留
        self.assertEqual(P.REBUY_DAYS, 90)
        self.assertEqual(P.J_LOW, 1.0)
        self.assertEqual(P.J_HIGH, 95.0)
        self.assertEqual(P.RSI_OS, 35.0)
        self.assertEqual(P.MAX_POS, 1.5)
        self.assertEqual(P.SLIPPAGE_BPS, 5)
        self.assertEqual(P.FIN_RATE, 0.07)
        self.assertTrue(P.VAL_GATE and P.MA250_GATE)


class TestParametrizedSignals(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.df = E.get_prices(TR, PX)
        cls.df_dft = E.build_signals(cls.df.copy())
        cls.df_exp = E.build_signals(cls.df.copy(), p=E.make_params())
        cls.df_hs = E.build_signals(cls.df.copy(), p=E.make_params(X_UP=15.0, Y_DOWN=20.0, HOLD_DAYS=120))

    def test_h1_week_completed(self):
        # 策略层审计 H-1：未完成 ISO 周剔除判定（交易日历二分）
        self.assertTrue(E._week_completed(datetime.date(2022, 5, 27)))    # 周五，下周交易日 5-30 → 本周完整
        self.assertFalse(E._week_completed(datetime.date(2022, 5, 25)))   # 周三，5-26/27 仍在同一 ISO 周 → 未完成

    def test_h1_build_signals_drops_incomplete_week(self):
        # 实盘每日运行（末日=今天，未完成周）应剔除末日所在周，wj 回填上一完整周。
        # 合成 12 交易日（2 周完整 + 周一/周二），末日=周二属未完成周（日历/降级均判未完成）。
        n = 12
        px = 3000.0 * (1.002 ** np.arange(n))
        df = pd.DataFrame({"date": pd.bdate_range("2022-05-02", periods=n),
                           "px": px, "close": px, "vol": np.full(n, 1e9), "tr": px})
        sig = E.build_signals(df.copy())
        sig_b = E.build_signals(df.iloc[:10].copy())   # 完整 2 周（末日 05-13 周五）
        self.assertEqual(sig["wj"].iloc[-1], sig_b["wj"].iloc[-1])   # 末日 wj = 上周 J（剔除生效）

    def test_h6_max_pos_caps_leverage(self):
        # 策略层审计 H-6：MAX_POS 派生仓位档位——1.0 关闭杠杆，1.5 允许加仓
        df = E.build_signals(E.get_prices(TR, PX))
        P0 = E.make_params(X_UP=15.0, Y_DOWN=14.0, HOLD_DAYS=120, MAX_POS=1.0)
        P1 = E.make_params(X_UP=15.0, Y_DOWN=14.0, HOLD_DAYS=120, MAX_POS=1.5)
        _, _, pos0, *_ = E.replay(df, p=P0)
        _, _, pos1, *_ = E.replay(df, p=P1)
        self.assertLessEqual(pos0.max(), 1.0 + 1e-9)          # 杠杆关闭：任何时点 ≤100%
        self.assertGreaterEqual(pos1.max(), pos0.max())       # 1.5 档不劣于 1.0 档（且可达杠杆位）

    def test_explicit_default_identical_to_none(self):
        # p=None 与 p=make_params() 逐列一致（参数化对默认路径零回归）
        cols = [c for c in self.df_dft.columns if c not in ("date", "close", "px", "vol")]
        for c in cols:
            a = self.df_dft[c].fillna(-1e9).values
            b = self.df_exp[c].fillna(-1e9).values
            np.testing.assert_allclose(a, b, err_msg=f"列 {c} 不应因显式默认参数而变化")

    def test_x_up_lowers_overbought_threshold(self):
        # X_UP 15 < 20：15%-20% 涨幅区间内变体应多出超买信号
        a = int(self.df_dft["overbought"].sum())
        b = int(self.df_hs["overbought"].sum())
        self.assertGreaterEqual(b, a, "X_UP=15 触发超买次数应≥默认 X_UP=20")

    def test_y_down_deepens_oversold_threshold(self):
        # Y_DOWN 20 > 14：14%-20% 跌幅区间内默认触发、变体不触发 → 变体超卖次数≤默认
        a = int(self.df_dft["oversold"].sum())
        b = int(self.df_hs["oversold"].sum())
        self.assertLessEqual(b, a, "Y_DOWN=20 触发超卖次数应≤默认 Y_DOWN=14")

    def test_hold_days_in_replay_reason(self):
        # HOLD_DAYS=5 强制到期：验证参数透传到 replay 到期卖出 reason（沪深300 120 天较长、
        # 历史多为动能消失提前卖，用极端短持有日确保产生到期单）
        trades, *_ = E.replay(self.df_hs, start=E.START,
                              p=E.make_params(X_UP=15.0, Y_DOWN=20.0, HOLD_DAYS=5))
        due = [t for t in trades if "自然日" in t["reason"]]
        self.assertTrue(due, "HOLD_DAYS=5 应产生到期卖出")
        self.assertTrue(any("5自然日" in t["reason"] for t in due), "到期 reason 应携带 HOLD_DAYS 值")
        # 沪深300 定稿参数（120 天）下回测不报错、且到期文案为 120（若产生到期单）
        trades2, *_ = E.replay(self.df_hs, start=E.START,
                               p=E.make_params(X_UP=15.0, Y_DOWN=20.0, HOLD_DAYS=120))
        due2 = [t for t in trades2 if "卖出一档临时仓" in t["reason"]]
        for t in due2:
            self.assertIn("120", t["reason"])

    def test_snapshot_param_reflects_p(self):
        df_all = self.df_hs
        trades, closed, positions, state, pos, legs, t0 = E.replay(
            df_all, t1=True, start=E.START, p=E.make_params(X_UP=15.0, Y_DOWN=20.0, HOLD_DAYS=120))
        df = df_all[df_all["date"] >= pd.Timestamp(E.START)].reset_index(drop=True)
        ec = E.equity_curve(df, trades, positions, p=E.make_params(X_UP=15.0, Y_DOWN=20.0, HOLD_DAYS=120))
        m = E.metrics(ec, trades)
        os_stat = E.oversold_stats(closed)
        snap = U.build_snapshot(df_all, state, pos, legs, t0, trades, os_stat,
                                cal={"is_today_trade": None, "next_trade": None, "cal_missing": True},
                                p=E.make_params(X_UP=15.0, Y_DOWN=20.0, HOLD_DAYS=120))
        self.assertEqual(snap["param"]["x_up"], 15.0)
        self.assertEqual(snap["param"]["y_down"], 20.0)
        self.assertEqual(snap["param"]["hold_days"], 120)
        # 默认调用（p=None）保持红利低波参数
        snap_dft = U.build_snapshot(df_all, state, pos, legs, t0, trades, os_stat,
                                    cal={"is_today_trade": None, "next_trade": None, "cal_missing": True})
        self.assertEqual(snap_dft["param"]["x_up"], 20.0)
        self.assertEqual(snap_dft["param"]["hold_days"], 60)


class TestHs300Update(unittest.TestCase):
    def test_hs300_params_definition(self):
        import hs300_update
        self.assertEqual(hs300_update.P.X_UP, 15.0)
        # v8.1（策略层审计 H-4）：Y_DOWN 20→14——20% 阈值在沪深300 全样本仅触发 3 天（2018 年 0 天），被数据禁用
        self.assertEqual(hs300_update.P.Y_DOWN, 14.0)
        self.assertEqual(hs300_update.P.HOLD_DAYS, 120)
        self.assertEqual(hs300_update.START, E.START)   # 与红利低波同窗口


if __name__ == "__main__":
    unittest.main()
