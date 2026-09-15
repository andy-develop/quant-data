#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PAYLOAD 提取 / 注入（update.py 与 sector_update.py 共用）。

两个更新脚本各自生成自己的数据段：
  - update.py          → {"snapshot": …, "backtest": …}（红利低波）
  - sector_update.py   → {"sector": …}（行业轮动）
页面 index.html 是自包含单文件，两个脚本轮流重写它。为避免互相覆盖，
每次写之前都从"当前 index.html"提取对方的数据段并原样带回来（carry-forward），
从而无论执行顺序如何，最终页面都同时包含两套数据；单跑任一脚本也不会清空对方。

约定：模板中 PAYLOAD 脚本块初始内容可为任意占位（如 __PAYLOAD__），
inject() 会用正则整块替换，extract() 解析失败返回 None。
"""
import json
import re

_PAT = re.compile(r'<script id="PAYLOAD" type="application/json">(.*?)</script>', re.S)


def extract(html):
    """从 HTML 中解析 PAYLOAD JSON；不存在或解析失败返回 None。"""
    if not html:
        return None
    m = _PAT.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def inject(tpl, payload):
    """把 payload 整块写回模板中的 PAYLOAD 脚本块（保持其他部分原样）。"""
    s = json.dumps(payload, ensure_ascii=False)
    if _PAT.search(tpl):
        return _PAT.sub(
            lambda m: '<script id="PAYLOAD" type="application/json">' + s + '</script>',
            tpl, count=1)
    return tpl
