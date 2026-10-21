#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股全景扫描 Web 版 (纯标准库, 零依赖)
启动: python3 app.py  →  http://localhost:8080
接口:
  GET /                前端页面
  GET /api/search?q=   全A股代码/名称搜索(自动补全)
  GET /api/scan?code=  逐股全景评分(JSON)
  GET /api/screen?strategy=qarp|dividend|lowpos  策略初筛(JSON)
"""
import json, ssl, time, re, sys, os, threading
import urllib.request
from urllib.parse import quote, urlparse, parse_qs
from http.server import HTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import stock_scanner as ss  # 复用数据管道与评分

_ctx = ss.ctx
_UA = ss.UA

# ---------------- 全A股列表缓存 ----------------
_LIST_LOCK = threading.Lock()
_LIST_CACHE = {"ts": 0, "stocks": []}


def _gj(url, retry=3, timeout=20):
    for i in range(retry + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=timeout, context=_ctx) as r:
                return json.loads(r.read().decode())
        except Exception:
            if i == retry:
                return None
            time.sleep(1.5)


def _fnum(x):
    try:
        v = float(x)
        return v if v == v else None
    except Exception:
        return None


def fetch_all_stocks(max_age=21600):
    """全A快照归一化: code/name/price/pe/pb/mktcap亿/dy/chg60/ytd, 缓存6小时
    主源: 东财 clist | 备源: 腾讯 rank(东财限流时自动降级, 无PB/股息率)"""
    with _LIST_LOCK:
        if time.time() - _LIST_CACHE["ts"] < max_age and _LIST_CACHE["stocks"]:
            return _LIST_CACHE["stocks"]
    stocks = _fetch_em_list()
    if not stocks:
        stocks = _fetch_qq_list()
    if stocks:
        with _LIST_LOCK:
            _LIST_CACHE.update({"ts": time.time(), "stocks": stocks})
    return stocks


def _fetch_em_list():
    stocks, pn, total = [], 1, None
    while True:
        url = ("https://push2.eastmoney.com/api/qt/clist/get?pn=%d&pz=500&po=1&np=1"
               "&fltt=2&invt=2&fid=f12&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
               "&fields=f12,f14,f2,f9,f23,f20,f24,f25,f133,f100" % pn)
        d = _gj(url, retry=1)
        data = (d or {}).get("data") or {}
        diff = data.get("diff") or []
        if total is None:
            total = data.get("total") or 0
        if not diff:
            break
        for r in diff:
            cap = _fnum(r.get("f20"))
            stocks.append({
                "code": str(r.get("f12", "")), "name": r.get("f14", ""),
                "price": r.get("f2"), "pe": r.get("f9"), "pb": r.get("f23"),
                "mktcap亿": round(cap / 1e8, 1) if cap else None, "dy": r.get("f133"),
                "chg60": r.get("f24"), "ytd": r.get("f25"), "ind": r.get("f100"),
            })
        if pn * 500 >= total or pn > 12:
            break
        pn += 1
        time.sleep(0.3)
    return stocks


def _fetch_qq_list():
    stocks, offset = [], 0
    while offset < 5600:
        url = ("https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList"
               f"?board_code=aStock&sort_type=price&direct=down&offset={offset}&count=200")
        d = _gj(url, retry=1)
        rows = ((d or {}).get("data") or {}).get("rank_list") or []
        if not rows:
            break
        for r in rows:
            code = re.sub(r"^(sh|sz|bj)", "", r.get("code", ""))
            stocks.append({
                "code": code, "name": r.get("name", ""), "price": _fnum(r.get("zxj")),
                "pe": _fnum(r.get("pe_ttm")), "pb": None,
                "mktcap亿": _fnum(r.get("ltsz")), "dy": None,
                "chg60": _fnum(r.get("zdf_d60")), "ytd": _fnum(r.get("zdf_y")), "ind": None,
            })
        offset += 200
        time.sleep(0.25)
    return stocks


def enrich_quotes(stocks):
    """腾讯批量行情补齐现价/总市值/PB(每次50只)"""
    for i in range(0, len(stocks), 50):
        chunk = stocks[i:i + 50]
        syms = ",".join(market_prefix(s["code"]) + s["code"] for s in chunk)
        try:
            req = urllib.request.Request(f"https://qt.gtimg.cn/q={syms}", headers=_UA)
            with urllib.request.urlopen(req, timeout=20, context=_ctx) as r:
                lines = r.read().decode("gbk").strip().split(";")
            for j, line in enumerate(lines):
                if j >= len(chunk) or "~" not in line:
                    continue
                p = line.split("~")
                s = chunk[j]
                if not s.get("price"):
                    s["price"] = _fnum(p[3])
                s["pb"] = s.get("pb") or _fnum(p[46]) if len(p) > 46 else s.get("pb")
                if not s.get("mktcap亿") and len(p) > 45:
                    s["mktcap亿"] = _fnum(p[45])
        except Exception:
            pass
        time.sleep(0.4)
    return stocks


def market_prefix(code):
    return "sh" if code.startswith(("6", "9", "5")) else ("bj" if code.startswith(("4", "8")) else "sz")


def screen(strategy):
    stocks = fetch_all_stocks()
    if not stocks:
        return {"error": "全A列表抓取失败(东财与腾讯均不可用)，请稍后重试"}
    has_dy = any(s.get("dy") is not None for s in stocks)
    if strategy == "dividend" and not has_dy:
        return {"error": "当前为腾讯备用源(东财限流)，无股息率字段，高股息策略暂不可用；请稍后重试或使用其他策略"}
    out = []
    for s in stocks:
        nm = s.get("name") or ""
        if "ST" in nm.upper() or "退" in nm:
            continue
        pe, dy = _fnum(s["pe"]), _fnum(s["dy"])
        cap, chg60, ytd = s.get("mktcap亿"), _fnum(s["chg60"]), _fnum(s["ytd"])
        if not s["code"] or not nm:
            continue
        if strategy == "qarp":
            if pe and 3 <= pe < 30 and cap and cap > 50 and (chg60 is None or chg60 > -20):
                sc = (5.0 - min(pe, 15) / 5) + (dy or 0) * 0.5 + (2 if (ytd or 0) < 0 else 0)
                out.append({**s, "分": round(sc, 2)})
        elif strategy == "dividend":
            if dy and dy >= 4 and pe and 3 <= pe < 60:
                out.append({**s, "分": round(dy * 2 - min(pe, 40) / 4, 2)})
        elif strategy == "lowpos":
            if ytd is not None and ytd < -15 and pe and 3 <= pe < 60:
                out.append({**s, "分": round(-ytd - min(pe, 50) / 5, 2)})
    out.sort(key=lambda x: -x["分"])
    top = out[:40]
    enrich_quotes(top)
    return {"count": len(out), "stocks": top}


# ---------------- 扣非核验 + 周期分位 ----------------

def fetch_f10(code, sfx, n=44):
    """F10主要指标: 近44期归母/扣非(约20年,年报≥10个用于周期分位)"""
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_F10_FINANCE_MAINFINADATA"
           f"&columns=ALL&pageSize={n}&pageNumber=1&sortColumns=REPORT_DATE&sortTypes=-1&filter="
           + quote(f'(SECUCODE="{code}{sfx}")'))
    d = _gj(url, retry=2)
    return ((d or {}).get("result") or {}).get("data") or []


def profit_analysis(f10rows):
    """扣非YoY(自算) + 年度扣非分位(周期位置)"""
    if not f10rows:
        return None

    def val(r, k):
        try:
            v = float(r.get(k))
            return v if v == v else None
        except Exception:
            return None

    latest = f10rows[0]
    lf_raw = val(latest, "KCFJCXSYJLR")                       # 元(用于同比比率)
    lf = round(lf_raw / 1e8, 2) if lf_raw is not None else None  # 亿元(仅展示)
    yoy = None
    ldate = str(latest.get("REPORT_DATE"))[:10]
    py = str(int(ldate[:4]) - 1) + ldate[4:]
    for r in f10rows:
        if str(r.get("REPORT_DATE"))[:10] == py:
            pf = val(r, "KCFJCXSYJLR")                        # 元
            if pf not in (None, 0) and lf_raw is not None:
                yoy = round((lf_raw / pf - 1) * 100, 1) if pf > 0 else (round((lf_raw / abs(pf) - 1) * 100, 1) if lf_raw > 0 else None)
            break
    annuals = sorted(val(r, "KCFJCXSYJLR") for r in f10rows
                     if r.get("REPORT_TYPE") == "年报" and val(r, "KCFJCXSYJLR") is not None)
    pctile = None
    if len(annuals) >= 5:
        lf_annual = None
        for r in f10rows:
            if r.get("REPORT_TYPE") == "年报":
                lf_annual = val(r, "KCFJCXSYJLR")
                break
        if lf_annual is not None:
            pctile = round(sum(1 for v in annuals if v <= lf_annual) / len(annuals), 2)
    return {"扣非最新": lf, "扣非YoY": yoy, "周期分位": pctile, "年报数": len(annuals)}


# ---------------- 多轮漏斗选股 ----------------

def _verify_financial(code):
    """轮次3: 财务核验。返回 (code, 通过?, 原因, 附带数据)"""
    try:
        mkt, sfx, _ = ss.market(code)
        earn = ss.fetch_earnings(code)
        divs = ss.fetch_dividends(code, sfx)
        if not earn:
            return code, False, "财务数据缺失", None
        h1, fy = earn.get("H1", {}), earn.get("FY", {})
        name_ind = None
        np1 = h1.get("归母净利亿")
        if np1 is not None and np1 < 0:
            return code, False, "当期亏损", None
        rev = h1.get("营收YoY") if h1.get("营收YoY") is not None else fy.get("营收YoY")
        nps = h1.get("净利YoY") if h1.get("净利YoY") is not None else fy.get("净利YoY")
        if rev is None or rev < -10:
            return code, False, f"营收恶化({rev if rev is not None else 'N/A'}%)", None
        if nps is None or nps <= 0:
            return code, False, f"净利负增长({nps if nps is not None else 'N/A'}%)", None
        cfo = h1.get("每股经营现金流")
        if cfo is not None and cfo < 0:
            return code, False, "经营现金流为负", None
        roe = h1.get("ROE")
        if roe is not None and roe * 2 < 6:
            return code, False, f"ROE年化不足6%({round(roe*2,1)}%)", None
        yrs = set(r["公告日"][:4] for r in divs if r["公告日"])
        if len(yrs) < 3:
            return code, False, f"分红不足3年({len(yrs)}年)", None
        f10 = fetch_f10(code, sfx)
        pa = profit_analysis(f10)
        if pa:
            lf, yoy = pa["扣非最新"], pa["扣非YoY"]
            if lf is not None and lf < 0:
                return code, False, "扣非为负(主业实际亏损,利润靠非经常损益)", None
            if yoy is not None and yoy <= -30:
                return code, False, f"扣非同比下滑{yoy}%(利润质量恶化)", None
        return code, True, "PASS", {"earn": earn, "divs": divs, "dyrs": len(yrs), "f10": pa}
    except Exception as e:
        return code, False, f"核验异常:{type(e).__name__}", None


def fetch_kline_multi(code, mkt):
    """K线三级源: 腾讯fqkline → 新浪日K(500根)"""
    closes, dates = ss.fetch_kline(code, mkt)
    if len(closes) >= 120:
        return closes, dates
    sym = market_prefix(code) + code
    url = (f"https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
           f"?symbol={sym}&scale=240&ma=no&datalen=500")
    d = _gj(url, retry=2)
    if isinstance(d, list) and len(d) >= 120:
        return ([float(x["close"]) for x in d], [x["day"] for x in d])
    return closes, dates


def _verify_position(code):
    """轮次4: 位置核验。不在52周顶部、未处深跌通道"""
    try:
        mkt, _, _ = ss.market(code)
        closes, dates = fetch_kline_multi(code, mkt)
        if len(closes) < 120:
            return code, False, "K线不足", None
        cur = closes[-1]
        w52 = closes[-244:] if len(closes) >= 244 else closes
        dd = (1 - cur / max(w52)) * 100
        m60 = (cur / closes[-61] - 1) * 100 if len(closes) >= 61 else 0
        off_lo = (cur / min(w52) - 1) * 100
        if dd < 5:
            return code, False, f"处于52周高位附近(距顶{dd:.0f}%)", None
        if m60 <= -15:
            return code, False, f"下跌通道未止(60日{m60:.0f}%)", None
        if off_lo < 4:
            return code, False, "正在创新低路径", None
        return code, True, "PASS", {"tech": ss.tech_metrics(closes, dates)}
    except Exception as e:
        return code, False, f"核验异常:{type(e).__name__}", None


def funnel(strategy, final_n=10):
    stages = []
    stocks = fetch_all_stocks()
    if not stocks:
        return {"error": "全A列表抓取失败，请稍后重试"}
    stages.append({"name": "轮次1 · 全市场", "rule": "全部A股(沪深主板/创业板/科创板)", "count": len(stocks), "out": []})

    # 轮次2 快照硬过滤
    pool, out2 = [], {}
    for s in stocks:
        nm = s.get("name") or ""
        pe = _fnum(s["pe"])
        cap = s.get("mktcap亿")
        price = _fnum(s["price"])
        if "ST" in nm.upper() or "退" in nm:
            out2["ST/退市"] = out2.get("ST/退市", 0) + 1; continue
        if pe is not None and (pe <= 0 or pe > 150):
            out2["亏损或PE>150"] = out2.get("亏损或PE>150", 0) + 1; continue
        if cap is not None and cap < 30:
            out2["市值<30亿"] = out2.get("市值<30亿", 0) + 1; continue
        if price is not None and price < 1.5:
            out2["仙股(<1.5元)"] = out2.get("仙股(<1.5元)", 0) + 1; continue
        pool.append(s)
    stages.append({"name": "轮次2 · 基础质量过滤", "rule": "剔除ST/退、亏损或PE>150、市值<30亿、仙股", "count": len(pool), "out": out2})

    # 轮次3a 策略初筛(快照分) → 取候选池前60
    has_dy = any(s.get("dy") is not None for s in stocks)
    if strategy == "dividend" and not has_dy:
        return {"error": "当前为腾讯备用源(无股息率字段)，高股息漏斗暂不可用；请稍后重试"}
    scored = []
    for s in pool:
        pe, dy = _fnum(s["pe"]), _fnum(s["dy"])
        cap, chg60, ytd = s.get("mktcap亿"), _fnum(s["chg60"]), _fnum(s["ytd"])
        if strategy in ("qarp", "composite"):
            if pe and 3 <= pe < 30 and cap and cap > 50 and (chg60 is None or chg60 > -20):
                scored.append((s, (5.0 - min(pe, 15) / 5) + (dy or 0) * 0.5 + (2 if (ytd or 0) < 0 else 0)))
        if strategy in ("dividend", "composite"):
            if dy and dy >= 4 and pe and 3 <= pe < 60:
                scored.append((s, dy * 2 - min(pe, 40) / 4))
        if strategy in ("lowpos", "composite"):
            if ytd is not None and ytd < -15 and pe and 3 <= pe < 60:
                scored.append((s, -ytd - min(pe, 50) / 5))
    scored.sort(key=lambda x: -x[1])
    cand, seen = [], set()
    for s, sc in scored:
        if s["code"] in seen:
            continue
        seen.add(s["code"]); cand.append(s)
        if len(cand) >= 60:
            break
    stages.append({"name": "轮次3 · 策略初筛", "rule": {"qarp": "QARP: PE3-30+市值>50亿+非急跌", "dividend": "股息率≥4%+PE3-60",
                   "lowpos": "YTD<-15%+PE3-60", "composite": "QARP∪高股息∪深回调 三策略并集"}[strategy] + "，按分取前60", "count": len(cand), "out": {}})

    # 轮次3b 财务核验(并发8)
    fin_ok, fin_out = [], {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for code, ok, reason, extra in ex.map(_verify_financial, [s["code"] for s in cand]):
            if ok:
                fin_ok.append((code, extra))
            else:
                cat = reason.split("(")[0]
                fin_out[cat] = fin_out.get(cat, 0) + 1
    stages.append({"name": "轮次4 · 财务核验", "rule": "盈利≥0、营收>-10%、净利>0、现金流>0、ROE年化≥6%、分红≥3年、扣非为正且同比>-30%(逐股实查)", "count": len(fin_ok), "out": fin_out})

    # 轮次4 位置核验(并发6)
    pos_ok, pos_out = [], {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_verify_position, code): code for code, _ in fin_ok}
        extra_map = {code: extra for code, extra in fin_ok}
        for fut in as_completed(futs):
            code, ok, reason, extra = fut.result()
            if ok:
                pos_ok.append((code, extra["tech"]))
            else:
                cat = reason.split("(")[0]
                pos_out[cat] = pos_out.get(cat, 0) + 1
    stages.append({"name": "轮次5 · 位置核验", "rule": "距52周高点回撤≥5%、60日动量>-15%、不在创新低路径(逐股实查K线)", "count": len(pos_ok), "out": pos_out})

    # 终选: 完整评分 + 行业分散 + 取N
    fin_map = {code: extra for code, extra in fin_ok}
    info_map = {s["code"]: s for s in stocks}
    finals = []
    for code, tech in pos_ok:
        s = info_map.get(code, {})
        try:
            mkt, sfx, _ = ss.market(code)
            quote = ss.fetch_quote(code, mkt) or {}
            earn = fin_map[code]["earn"]; divs = fin_map[code]["divs"]
            pa = fin_map[code].get("f10")
            cyc = (pa or {}).get("周期分位")
            S, K = ss.score_all(earn, tech, quote, divs, [])
            K["周期分位"] = cyc
            K["扣非YoY"] = (pa or {}).get("扣非YoY")
            lines, total, g = ss.expert_opinion(s.get("name") or code, S, K, tech, K["亏损"])
            if cyc is not None and cyc >= 0.9 and ((pa or {}).get("扣非YoY") or 0) < 0:
                total = max(0, total - 10)   # 周期顶部回落: 利润10年高位+同比下滑, 估值锚打折
                g = ss.grade(total)[0]
                lines.append(f"- **周期顶部提示**：年度扣非处近10年分位 {int(cyc*100)}% 且同比回落，估值应打折")
            finals.append({
                "code": code, "name": s.get("name"), "ind": s.get("ind"),
                "total": total, "grade": g,
                "price": quote.get("现价"), "pe": K["PE"], "dy": K["股息率"],
                "roe": K["ROE"], "cfo": K["每股现金流"], "rev": K["营收YoY"],
                "m60": tech["近60日%"], "dd": tech["距52周高点%"],
                "kf": (pa or {}).get("扣非最新"), "kfyoy": (pa or {}).get("扣非YoY"), "cycle": cyc,
            })
        except Exception:
            continue
    finals.sort(key=lambda x: -x["total"])
    picked, ind_cnt = [], {}
    for f in finals:
        if len(picked) >= final_n:
            break
        ind = f.get("ind")
        if ind:  # 有行业数据才做分散约束(单行业≤2只); 备用源无行业则不限制
            if ind_cnt.get(ind, 0) >= 2:
                continue
            ind_cnt[ind] = ind_cnt.get(ind, 0) + 1
        picked.append(f)
    stages.append({"name": "轮次6 · 终选", "rule": f"综合评分排序+单行业≤2只，最终选出 {len(picked)} 只", "count": len(picked), "out": {}})
    return {"strategy": strategy, "stages": stages, "finals": picked,
            "all_graded": len(finals), "source": "东财" if has_dy else "腾讯备用源"}


# ---------------- 建仓决策引擎 ----------------

def build_decision(quote, tech, K, total, g):
    """长线建仓决策: 评级×位置×动量 → 明确触发价位(前收/实时均可, 长线口径)"""
    price = quote.get("现价")
    if not price or not tech:
        return None
    lo, hi = tech["52周低"], tech["52周高"]
    m60 = tech["近60日%"] if tech["近60日%"] is not None else -99
    ma20 = round(price / (1 + tech["相对MA20%"] / 100), 2)
    ma250 = round(price / (1 + tech["相对MA250%"] / 100), 2)
    stable = m60 >= -5
    falling = m60 <= -15
    ts = quote.get("行情时间") or ""
    basis = (f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]} 腾讯行情"
             if len(ts) >= 12 else "最近收盘价")
    add1 = round(max(price * 0.92, lo * 1.05), 2)
    add2 = round(max(price * 0.85, lo * 1.02), 2)
    stop = round(lo * 0.97, 2)
    trim = round(hi * 0.95, 2)
    loss = K.get("亏损")
    cfo = K.get("每股现金流")

    if g.startswith("A") and stable:
        action, color = "建仓", "green"
        summary = f"评分{total}达优质线且已企稳(60日{m60:+.1f}%)：现价可首批建仓(计划仓位40%)，分2~3批执行。"
        levels = [("现价 · 首批", price, "条件已满足，可直接下单"),
                  ("加仓一档", add1, "现价再跌约8%或接近52周低点5%上方，加30%"),
                  ("加仓二档", add2, "深度回踩区，加最后30%"),
                  ("右侧确认线", ma20, "站稳20日线上方为趋势确认(参考)"),
                  ("止损/失效", stop, "收盘跌破=逻辑证伪，无条件离场"),
                  ("减仓评估区", trim, "反弹接近前高，评估止盈")]
    elif g.startswith("A"):
        action, color = "等企稳再建仓", "orange"
        summary = f"评分{total}优质但仍在下跌(60日{m60:+.1f}%)：不接刀。首批触发=收盘站稳{ma20}(20日线)或60日动量转正。"
        levels = [("企稳触发", ma20, "收盘站稳20日线→首批建仓40%"),
                  ("激进回踩档", add1, "不愿等右侧者：接近52周低点5%上方小批介入"),
                  ("加仓二档", add2, "深度回踩区"),
                  ("止损/失效", stop, "收盘跌破无条件离场"),
                  ("减仓评估区", trim, "反弹接近前高评估止盈")]
    elif g.startswith("B") and stable:
        action, color = "小仓试探", "orange"
        summary = f"评分{total}良好且企稳：试探仓≤2成，回踩加仓，不宜重仓。"
        levels = [("试探仓", price, "≤计划仓位20%"),
                  ("加仓档", add1, "回踩8%再补"),
                  ("右侧确认线", ma20, "站稳后可升至半仓"),
                  ("止损/失效", stop, "收盘跌破清仓重估"),
                  ("减仓评估区", trim, "接近前高评估")]
    elif g.startswith("B") or (g.startswith("C") and not falling):
        action, color = "观察", "gold"
        summary = f"评分{total}中性：进入观察名单。介入触发=①收盘站稳{ma20}且60日动量转正，或②回踩{round(lo*1.05,2)}(52周低点上方5%)且无利空。"
        levels = [("介入触发A(右侧)", ma20, "站稳20日线+动量转正"),
                  ("介入触发B(左侧)", round(lo * 1.05, 2), "52周低点上方5%，需基本面无恶化"),
                  ("放弃线", stop, "跌破=移出观察名单")]
    elif g.startswith("C") and stable:
        action, color = "观望偏机会", "gold"
        summary = f"评分{total}中性但已企稳：等待评分改善(估值/现金流/成长任一修复)。{ma20}站稳可小仓试错。"
        levels = [("试错仓", price, "仅限评分改善信号出现后"),
                  ("右侧确认线", ma20, "站稳+动量转正才升级"),
                  ("放弃线", stop, "跌破移出观察")]
    elif falling and not loss:
        action, color = "等待", "red"
        summary = f"60日{m60:+.1f}%仍在下跌通道：不介入。等待①企稳信号(站上{ma20})②或基本面改善(财报确认)。"
        levels = [("企稳触发", ma20, "站稳20日线+60日动量转正再评估"),
                  ("放弃线", stop, "跌破=基本面再恶化，进一步远离")]
    elif loss:
        action, color = "回避" if g.startswith("D") else "等待反转证据", "red"
        summary = f"当期亏损+评分{total}：禁止建仓。升级条件=单季减亏/扭亏 且 60日动量转正，二者同时出现才重估。"
        levels = [("反转触发", None, "Q3/年报减亏扭亏 + 60日动量转正(双确认)"),
                  ("放弃线", stop, "再创新低=继续回避")]
    else:
        action, color = "回避", "red"
        summary = f"评分{total}偏弱：回避。重新评估条件=ROE/现金流/成长任一维度实质修复。"
        levels = [("重估触发", None, "下一财报期评分升至C以上")]
    notes = [f"价格基准：{basis}（长线口径，盘中/收盘均可）",
             f"价位逻辑：加仓档取「现价-8%」与「52周低点+5%」较高者，避免挂单在支撑位之下",
             f"仓位纪律：首批≤40%、总投入≤可投资金60%、留40%现金应对加档",
             f"财报窗口：所有触发价需避开财报当日博弈，正式财报落地后重估"]
    if cfo is not None and cfo < 0:
        notes.append("⚠ 现金流为负：任何加仓需财报确认现金流转正")
    if (K.get("周期分位") or 0) >= 0.9 and (K.get("扣非YoY") or 0) < 0:
        notes.insert(0, "⚠ 周期顶部风险：年度扣非处近10年高位且同比回落——加仓档更保守、减仓评估区提前执行")
    return {"action": action, "color": color, "summary": summary, "basis": basis,
            "levels": [{"label": l, "price": p, "note": n} for l, p, n in levels], "notes": notes}


# ---------------- 风险因子层(8层扫描, 补齐深度分析遗漏) ----------------

def risk_layers(code, K, quote, earn=None):
    """8层风险因子: 每层输出 {name, status: OK|WARN|RISK|NA, detail}
    权重: RISK=3分, WARN=1分 → risk_level: 低(≤1)/中(2~3)/高(4~6)/极高(≥7)
    注意: 形参 quote 为行情字典; URL编码用 _qquote, 避免遮蔽"""
    from urllib.parse import quote as _qquote
    layers = []

    def num(r, k):
        try:
            v = float(r.get(k))
            return v if v == v else None
        except Exception:
            return None

    mkt, sfx, _ = ss.market(code)
    scode = code + sfx

    # L1 资产负债表质量: 应收/存货占比与增速 vs 营收增速
    try:
        d = _gj('https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_DMSK_FN_BALANCE'
                '&columns=ALL&pageSize=1&pageNumber=1&sortColumns=REPORT_DATE&sortTypes=-1&filter='
                + _qquote(f'(SECURITY_CODE="{code}")'))
        rows = ((d or {}).get("result") or {}).get("data") or []
        if rows:
            r = rows[0]
            ta = num(r, "TOTAL_ASSETS")
            ar, inv = num(r, "ACCOUNTS_RECE"), num(r, "INVENTORY")
            ar_g, inv_g = num(r, "ACCOUNTS_RECE_RATIO"), num(r, "INVENTORY_RATIO")
            rd = str(r.get("REPORT_DATE"))[:10]
            if ta and ar is not None:
                ar_pct = ar / ta * 100
                inv_pct = (inv / ta * 100) if inv is not None else None
                rev_g = K.get("营收YoY")
                msgs, st = [], "OK"
                if ar_pct > 30:
                    msgs.append(f"应收账款占总资产 {ar_pct:.0f}%（偏高,回款风险）"); st = "WARN"
                if inv_pct is not None and inv_pct > 40:
                    msgs.append(f"存货占总资产 {inv_pct:.0f}%（偏高,跌价风险）"); st = "WARN"
                if ar_g is not None and rev_g is not None and ar_g > rev_g + 20 and ar_pct > 15:
                    msgs.append(f"应收同比 +{ar_g:.0f}% 远超营收同比 +{rev_g:.0f}%（利润含水量预警）"); st = "RISK"
                inv_txt = f"{inv_pct:.0f}%" if inv_pct is not None else "N/A"
                layers.append({"name": "应收/存货质量", "status": st, "date": rd,
                               "detail": "；".join(msgs) or f"应收占总资产 {ar_pct:.0f}%、存货 {inv_txt}（占比正常）"})
            else:
                layers.append({"name": "应收/存货质量", "status": "NA", "date": rd, "detail": "数据不可得"})
        else:
            layers.append({"name": "应收/存货质量", "status": "NA", "date": "", "detail": "接口无数据"})
    except Exception as e:
        layers.append({"name": "应收/存货质量", "status": "NA", "date": "", "detail": f"核验异常:{type(e).__name__}"})
    time.sleep(0.4)

    # L2 商誉: 商誉/净资产
    try:
        d = _gj('https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_F10_FINANCE_GBALANCE'
                '&columns=ALL&pageSize=1&pageNumber=1&sortColumns=REPORT_DATE&sortTypes=-1&filter='
                + _qquote(f'(SECUCODE="{scode}")'))
        rows = ((d or {}).get("result") or {}).get("data") or []
        if rows:
            r = rows[0]
            gw, eq = num(r, "GOODWILL"), num(r, "TOTAL_EQUITY")
            rd = str(r.get("REPORT_DATE"))[:10]
            if gw and eq:
                pct_ = gw / eq * 100
                st = "RISK" if pct_ > 20 else ("WARN" if pct_ > 10 else "OK")
                layers.append({"name": "商誉减值", "status": st, "date": rd,
                               "detail": f"商誉 {gw/1e8:.1f} 亿 / 净资产 {eq/1e8:.0f} 亿 = {pct_:.1f}%"
                                         + ("（>20% 高危,一旦减值直接吞噬利润）" if st == "RISK" else
                                            ("（>10% 需跟踪并购标的业绩承诺）" if st == "WARN" else "（占比安全）"))})
            else:
                layers.append({"name": "商誉减值", "status": "OK", "date": rd, "detail": "无商誉或商誉为零（无减值雷）"})
        else:
            layers.append({"name": "商誉减值", "status": "NA", "date": "", "detail": "接口无数据"})
    except Exception as e:
        layers.append({"name": "商誉减值", "status": "NA", "date": "", "detail": f"核验异常:{type(e).__name__}"})
    time.sleep(0.4)

    # L3 股权质押(数据可能滞后, 口径标注)
    try:
        d = _gj('https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_CSDC_LIST'
                '&columns=ALL&pageSize=1&pageNumber=1&sortColumns=TRADE_DATE&sortTypes=-1&filter='
                + _qquote(f'(SECUCODE="{scode}")'))
        rows = ((d or {}).get("result") or {}).get("data") or []
        if rows and num(rows[0], "PRAT") is not None:
            r = rows[0]
            prat = num(r, "PRAT")
            rd = str(r.get("TRADE_DATE"))[:10]
            st = "RISK" if prat > 40 else ("WARN" if prat > 20 else "OK")
            layers.append({"name": "股权质押", "status": st, "date": rd,
                           "detail": f"质押比例 {prat:.1f}%（数据日 {rd}，中国结算口径可能滞后）"
                                     + ("（>40% 平仓/控制权风险）" if st == "RISK" else ("（>20% 需跟踪）" if st == "WARN" else ""))})
        else:
            layers.append({"name": "股权质押", "status": "OK", "date": "", "detail": "无有效质押记录（或数据滞后未收录）"})
    except Exception as e:
        layers.append({"name": "股权质押", "status": "NA", "date": "", "detail": f"核验异常:{type(e).__name__}"})
    time.sleep(0.4)

    # L4 股东户数(筹码集中度)
    try:
        d = _gj('https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_HOLDERNUMLATEST'
                '&columns=ALL&pageSize=1&pageNumber=1&filter=' + _qquote(f'(SECUCODE="{scode}")'))
        rows = ((d or {}).get("result") or {}).get("data") or []
        if rows:
            r = rows[0]
            hn, pre, chg = num(r, "HOLDER_NUM"), num(r, "PRE_HOLDER_NUM"), num(r, "HOLDER_NUM_RATIO")
            rd = str(r.get("END_DATE"))[:10]
            if hn and pre and chg is not None:
                st = "WARN" if chg > 15 else ("OK" if chg < -5 else "OK")
                note = ("（户数大增=筹码分散,警惕主力派发）" if chg > 15 else
                        ("（户数下降=筹码集中,主力吸筹或缩量）" if chg < -5 else "（筹码结构稳定）"))
                layers.append({"name": "筹码集中度", "status": st, "date": rd,
                               "detail": f"股东户数 {hn:,.0f} 户，环比 {chg:+.1f}%{note}"})
            else:
                layers.append({"name": "筹码集中度", "status": "NA", "date": rd, "detail": "户数变化数据不可得"})
        else:
            layers.append({"name": "筹码集中度", "status": "NA", "date": "", "detail": "接口无数据"})
    except Exception as e:
        layers.append({"name": "筹码集中度", "status": "NA", "date": "", "detail": f"核验异常:{type(e).__name__}"})
    time.sleep(0.4)

    # L5 大股东/高管减持(近90天)
    try:
        import datetime as _dt
        d90 = (_dt.datetime.strptime(ss.TODAY, "%Y-%m-%d") - _dt.timedelta(days=90)).strftime("%Y-%m-%d")
        d = _gj('https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_SHAREHOLDER_HOLDCHANGE'
                '&columns=ALL&pageSize=10&pageNumber=1&sortColumns=CHANGE_DATE&sortTypes=-1&filter='
                + _qquote(f'(SECUCODE="{scode}")(CHANGE_DATE>=\'{d90}\')'))
        rows = ((d or {}).get("result") or {}).get("data") or []
        if rows:
            sells = [r for r in rows if (num(r, "CHANGE_NUM") or 0) < 0]
            if sells:
                names = "、".join(str(r.get("HOLDER_NAME"))[:10] for r in sells[:3])
                layers.append({"name": "重要股东减持", "status": "WARN", "date": d90,
                               "detail": f"近90天 {len(sells)} 笔减持（{names} 等），需查公告动机"})
            else:
                layers.append({"name": "重要股东减持", "status": "OK", "date": d90, "detail": "近90天无减持记录（有增持类变动）"})
        else:
            layers.append({"name": "重要股东减持", "status": "OK", "date": d90, "detail": "近90天无大股东/高管持股变动"})
    except Exception as e:
        layers.append({"name": "重要股东减持", "status": "NA", "date": "", "detail": f"核验异常:{type(e).__name__}"})
    time.sleep(0.4)

    # L6 毛利率趋势(同期可比: H1 vs 上年H1, FY vs 上年FY, 避免报告期季节性失真)
    try:
        gm = {e["日期"][:4] + ("-H1" if "-06-30" in e["日期"] else "-FY"): e.get("毛利率")
              for e in (earn or []) if e.get("毛利率") is not None}
        pairs = []
        for y in {k[:4] for k in gm}:
            if f"{y}-H1" in gm and f"{int(y)-1}-H1" in gm:
                pairs.append((f"{y}中报", gm[f"{y}-H1"], gm[f"{int(y)-1}-H1"]))
            if f"{y}-FY" in gm and f"{int(y)-1}-FY" in gm:
                pairs.append((f"{y}年报", gm[f"{y}-FY"], gm[f"{int(y)-1}-FY"]))
        if pairs:
            worst = [(tag, cur - pre) for tag, cur, pre in pairs if cur - pre < -2]
            latest_tag, latest_cur = pairs[0][0], pairs[0][1]
            if worst:
                t, dp = worst[0]
                layers.append({"name": "毛利率趋势", "status": "WARN", "date": "",
                               "detail": f"{t}毛利率同比下滑 {abs(dp):.1f}pct，竞争加剧或成本上行；共 {len(worst)} 期同比下滑超2pct（{'、'.join(x for x, _ in worst)}）"})
            else:
                layers.append({"name": "毛利率趋势", "status": "OK", "date": "",
                               "detail": f"最新毛利率 {latest_cur:.1f}%（{latest_tag}），各期同比无连续恶化"})
        else:
            layers.append({"name": "毛利率趋势", "status": "NA", "date": "", "detail": "同期可比期数不足"})
    except Exception as e:
        layers.append({"name": "毛利率趋势", "status": "NA", "date": "", "detail": f"核验异常:{type(e).__name__}"})

    # L7 营收-现金流背离
    rev, cfo = K.get("营收YoY"), K.get("每股现金流")
    if rev is not None and cfo is not None and rev > 20 and cfo < 0:
        layers.append({"name": "营收-现金流背离", "status": "RISK", "date": "",
                       "detail": f"营收 +{rev:.0f}% 高增但每股现金流 {cfo} 元为负（扩张失血,利润未回款,Q3财报必须验证）"})
    elif cfo is not None and cfo < 0:
        layers.append({"name": "营收-现金流背离", "status": "WARN", "date": "",
                       "detail": f"每股经营现金流 {cfo} 元为负（利润未转化为现金）"})
    else:
        layers.append({"name": "营收-现金流背离", "status": "OK", "date": "",
                       "detail": f"每股经营现金流 {cfo} 元" + ("，与营收匹配" if rev is not None else "")})

    # L8 流动性(换手率/成交活跃度)
    try:
        to = quote.get("换手率")
        if to is not None:
            st = "WARN" if to < 0.3 else "OK"
            layers.append({"name": "交易流动性", "status": st, "date": "",
                           "detail": f"换手率 {to:.2f}%" + ("（<0.3% 成交萎缩,大资金进出困难）" if st == "WARN" else "（流动性正常）")})
        else:
            layers.append({"name": "交易流动性", "status": "NA", "date": "", "detail": "换手率数据不可得"})
    except Exception as e:
        layers.append({"name": "交易流动性", "status": "NA", "date": "", "detail": f"核验异常:{type(e).__name__}"})

    score = sum(3 if l["status"] == "RISK" else (1 if l["status"] == "WARN" else 0) for l in layers)
    level = "低" if score <= 1 else ("中" if score <= 3 else ("高" if score <= 6 else "极高"))
    return {"layers": layers, "score": score, "level": level}




def _kline_with_source(code, mkt):
    """K线三级源(带来源标记): 腾讯fqkline(前复权) → 新浪日K(不复权)"""
    closes, dates = ss.fetch_kline(code, mkt)
    if len(closes) >= 120:
        return closes, dates, "腾讯 fqkline（前复权）"
    sym = market_prefix(code) + code
    url = (f"https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
           f"?symbol={sym}&scale=240&ma=no&datalen=500")
    d = _gj(url, retry=2)
    if isinstance(d, list) and len(d) >= 120:
        return ([float(x["close"]) for x in d], [x["day"] for x in d],
                "新浪日K（不复权，除权日附近回撤计算或有失真）")
    return closes, dates, "腾讯 fqkline（前复权）"


def build_holding_decision(quote, tech, K, total, g, risk_block=False):
    """持仓视角三态决策: 持有(hold)/加仓(add)/弃仓(exit), 与 build_decision 的未持仓买入视角互补。
    规则透明可复算:
      弃仓 = 任一硬伤命中: ①亏损且净利同比恶化 ②评分<45(D级) ③扣非YoY≤-30%且60日≤-15%(双杀) ④距顶回撤≥50%且创新低路径
      加仓 = 无硬伤 且 非亏损 且 评级A/B 且 60日企稳(≥-5%) 且 现金流非负或扣非增长 且 无RISK级风险因子(risk_block)
      持有 = 其余: 给出「转加仓」与「转弃仓」双向触发价位
    """
    price = quote.get("现价")
    if not price or not tech:
        return None
    lo, hi = tech["52周低"], tech["52周高"]
    m60 = tech["近60日%"] if tech["近60日%"] is not None else -99
    ma20 = round(price / (1 + tech["相对MA20%"] / 100), 2)
    dd = tech["距52周高点%"]
    stable = m60 >= -5
    falling = m60 <= -15
    loss = K.get("亏损")
    cfo = K.get("每股现金流")
    kf = K.get("扣非YoY")
    npy = K.get("净利YoY")
    stop = round(lo * 0.97, 2)
    trim = round(hi * 0.95, 2)
    add1 = round(max(price * 0.92, lo * 1.05), 2)
    add2 = round(max(price * 0.85, lo * 1.02), 2)

    exit_reasons = []
    fundamental_bad = (loss and (npy is not None and npy < 0)) or (kf is not None and kf <= -30)
    if loss and (npy is not None and npy < 0):
        exit_reasons.append(f"当期亏损且同比恶化（净利YoY {npy:+.1f}%）")
    if total < 45 and fundamental_bad:
        exit_reasons.append(f"综合评分 {total} < 45 且基本面恶化（亏损/扣非大降），多项不达标")
    elif total < 45:
        pass  # 纯位置拉低的D级(基本面无恶化)不走弃仓, 由持有态给双向触发
    if kf is not None and kf <= -30 and m60 <= -15:
        exit_reasons.append(f"扣非同比 {kf:+.1f}% 恶化 + 60日 {m60:+.1f}% 下跌，基本面技术面双杀")
    if dd <= -50 and falling:
        exit_reasons.append(f"距52周高点 {dd:.1f}% 深度回撤且仍在创新低路径")

    can_add = (not exit_reasons and not loss and (g.startswith("A") or g.startswith("B"))
               and stable and (cfo is None or cfo >= 0 or (kf or 0) > 0) and not risk_block)

    notes_common = ["价位口径：与建仓决策一致（52周区间反推），以收盘价确认触发",
                    "财报窗口：正式财报落地后所有决策需重估，避开财报日博弈",
                    "本决策为「已持仓」视角；若尚未持仓请参考扫描页的建仓决策卡"]
    if cfo is not None and cfo < 0:
        notes_common.append("⚠ 现金流为负：加仓需等财报确认现金流转正，持仓者重点跟踪应收/存货")
    if (K.get("周期分位") or 0) >= 0.9 and (kf or 0) < 0:
        notes_common.insert(0, "⚠ 周期顶部风险：年度扣非处近10年高位且同比回落——减仓评估区应提前执行")

    if exit_reasons:
        full_exit = len(exit_reasons) >= 2 or (total < 45 and fundamental_bad)
        action, color = ("弃仓(清仓)", "red") if full_exit else ("弃仓(减半)", "red")
        summary = ("命中硬伤：" + "；".join(exit_reasons) + "。"
                   + ("多项硬伤叠加，建议清仓离场，回收资金转配评分更高的标的。" if full_exit
                      else "建议至少减半仓控制风险，剩余仓位仅在反转证据出现后保留。"))
        levels = [("清仓/减半执行", price, "条件已命中，不以回本价为锚"),
                  ("反弹离场参考", ma20, "若来不及离场，反抽20日线(弱反弹高位)分批撤"),
                  ("无条件红线", stop, f"收盘跌破{stop}，剩余仓位全部离场"),
                  ("反转复核条件", None, "单季扭亏 + 60日动量转正 双确认后，才可重新评估")]
        return {"verdict": "exit", "action": action, "color": color, "summary": summary,
                "levels": [{"label": l, "price": p, "note": n} for l, p, n in levels],
                "notes": notes_common, "reasons": exit_reasons}

    if can_add:
        action, color = "加仓", "green"
        cond = f"评分{total}({g.strip()}) + 60日{m60:+.1f}%企稳" + (" + 现金流为正" if (cfo or 0) >= 0 else " + 扣非仍在增长")
        summary = f"基本面与位置均支持加仓：{cond}。回踩分批加，单次≤现仓位1/3，避免一次打满。"
        levels = [("加仓一档", add1, "现价回落8%或接近52周低点5%上方时执行"),
                  ("加仓二档", add2, "深度回踩区，最后一批"),
                  ("右侧加仓参考", ma20, "直接放量站稳20日线者可右侧加仓(追价但确定性高)"),
                  ("止损/减仓红线", stop, "收盘跌破=持仓逻辑证伪，全部离场"),
                  ("止盈评估区", trim, "接近前高分批止盈或移动止损")]
        return {"verdict": "add", "action": action, "color": color, "summary": summary,
                "levels": [{"label": l, "price": p, "note": n} for l, p, n in levels],
                "notes": notes_common, "reasons": []}

    action, color = "持有", "gold"
    up = f"转「加仓」：收盘站稳 {ma20}（20日线）且 60日动量转正" + ("，或财报确认现金流转正" if (cfo or 0) < 0 else "")
    down = f"转「弃仓」：收盘跌破 {stop}（52周低点×0.97），或财报出现新增硬伤（亏损/扣非大幅恶化/解禁冲击）"
    summary = (f"评分{total}({g.strip()})、60日{m60:+.1f}%，处于「可持有但不满足加仓标准」的中间态："
               f"{'尚在下跌通道，等右侧信号' if falling else '评级未达加仓线，等评分或动量改善'}"
               + ("；另有RISK级风险因子未解除（见风险筛查表），暂缓加仓" if risk_block else "") + "。")
    levels = [("转加仓触发", ma20, "站稳20日线+动量转正→按加仓规则分批"),
              ("加仓预备档(激进)", add1, "不愿等右侧者：接近52周低点5%上方小批补，仓位≤1/4"),
              ("转弃仓红线", stop, "收盘跌破无条件离场，不等反弹"),
              ("止盈评估区", trim, "接近前高评估部分止盈")]
    return {"verdict": "hold", "action": action, "color": color, "summary": summary,
            "levels": [{"label": l, "price": p, "note": n} for l, p, n in levels],
            "notes": notes_common + [f"转加仓条件：{up}", f"转弃仓条件：{down}"], "reasons": []}


def _build_data_range(quote, dates, closes, ksrc, earn, divs, lifts, f10_n):
    """数据范围声明: 每项数据的来源/覆盖区间/截至时间/口径备注"""
    ts = quote.get("行情时间") or ""
    qtime = (f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}" if len(ts) >= 12 else "最近收盘")
    earn_dates = sorted({e.get("日期") for e in earn if e.get("日期")})
    div_from = min((r.get("公告日") or "") for r in divs) if divs else "-"
    div_to = max((r.get("公告日") or "") for r in divs) if divs else "-"
    return [
        {"项": "实时行情", "来源": "腾讯 qt.gtimg.cn", "覆盖": f"截至 {qtime}",
         "口径": "盘中=实时快照；收盘后=当日收盘价（长线口径通用）"},
        {"项": "日K线", "来源": ksrc, "覆盖": f"{dates[0]} ~ {dates[-1]}（{len(closes)} 根）",
         "口径": "约2年窗口；技术位（52周高低/均线/动量）基于此计算"},
        {"项": "财务数据", "来源": "东财业绩报表", "覆盖": "；".join(earn_dates) or "-",
         "口径": "最新中报+年报+两个同期；季报披露后自动更新，评分随报告期滚动"},
        {"项": "扣非/周期分位", "来源": "东财F10主要指标", "覆盖": f"近 {f10_n} 期（约11年）",
         "口径": "周期分位基于其中年报样本（≥5个年报才计算），扣非YoY同比匹配上期"},
        {"项": "分红记录", "来源": "东财分红送配明细", "覆盖": f"{div_from} ~ {div_to}（近8笔）",
         "口径": "按方案公告日；分红年数取近5年窗口"},
        {"项": "限售解禁", "来源": "东财解禁时间表", "覆盖": "未来 3 个月",
         "口径": "免费接口，大宗交易/询价转让减持未包含"},
        {"项": "资产负债表结构", "来源": "东财资产负债表摘要", "覆盖": "最新报告期",
         "口径": "应收/存货占总资产比及同比增速，用于利润含水量判断"},
        {"项": "商誉/质押/户数/减持", "来源": "东财F10资产负债表·中结算·股东变动",
         "覆盖": "最新可得期（质押数据可能滞后）",
         "口径": "商誉/净资产比、质押比例、股东户数环比、近90天减持记录；审计意见/大宗减持未覆盖"},
    ]


OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output")


def deep_analysis(code):
    """个股深度分析: 复用 scan_json 全量数据 → 持仓三态决策 + 数据范围声明 + Markdown 报告落盘"""
    base = scan_json(code)
    if "error" in base:
        return base
    c = base["code"]
    mkt, sfx, _ = ss.market(c)
    closes, dates, ksrc = _kline_with_source(c, mkt)
    K = {**base["key"], "扣非YoY": base.get("kfyoy"), "周期分位": base.get("cycle")}
    rl = risk_layers(c, K, base["quote"], earn=base["earn"])
    risk_block = any(l["status"] == "RISK" for l in rl["layers"])
    hd = build_holding_decision(base["quote"], base["tech"], K, base["total"], base["grade"], risk_block=risk_block)
    dr = _build_data_range(base["quote"], dates, closes, ksrc, base["earn"], base["divs"], base["lifts"], 44)
    # 高风险因子注入持仓决策注释
    if hd:
        for l in rl["layers"]:
            if l["status"] == "RISK":
                hd["notes"].append(f"🔴 风险扫描[{l['name']}]：{l['detail']}")
            elif l["status"] == "WARN":
                hd["notes"].append(f"⚠ 风险扫描[{l['name']}]：{l['detail']}")

    # ---- Markdown 报告 ----
    name, q = base["name"], base["quote"]
    L = [f"# {name}（{c}）个股深度分析报告",
         "",
         f"> 生成时间：{ss.TODAY} ｜ 综合评分：**{base['total']}/{base['grade']}** ｜ 现价：{q.get('现价')} 元",
         "",
         "## 一、数据范围声明",
         "",
         "| 数据项 | 来源 | 覆盖范围 | 口径备注 |",
         "|---|---|---|---|"]
    L += [f"| {r['项']} | {r['来源']} | {r['覆盖']} | {r['口径']} |" for r in dr]
    L += ["", "## 二、综合评分", "", "| 维度 | 得分 | 满分 | 备注 |", "|---|---|---|---|"]
    L += [f"| {s['dim']} | {s['score']} | {s['full'] if s['full'] else '—'} | {s['note']} |" for s in base["scorecard"]]
    L += ["", "## 三、基本面分析", "", "| 报告期 | 营收(亿) | 归母净利(亿) | ROE% | 毛利率% | 营收YoY% | 净利YoY% |", "|---|---|---|---|---|---|---|"]
    for e in base["earn"]:
        L.append(f"| {e.get('日期')} | {e.get('营收亿')} | {e.get('归母净利亿')} | {e.get('ROE')} | {e.get('毛利率')} | {e.get('营收YoY')} | {e.get('净利YoY')} |")
    kf = base.get("kf")
    cyc = base.get("cycle")
    L += ["",
          f"- 扣非净利（最新期）：{kf if kf is not None else 'N/A'} 亿，同比 {base.get('kfyoy') if base.get('kfyoy') is not None else 'N/A'}%",
          f"- 利润趋势：{base.get('profit_trend')}；周期分位（年度扣非近10年）：{f'{cyc:.0%}' if cyc is not None else 'N/A'}",
          f"- 分红年数（记录窗口内）：{base['key'].get('分红年数')} 年；股息率：{base['key'].get('股息率')}%",
          "- 解禁：", ""]
    L += [f"  - {r['date']} 解禁 {r['num']} 股（占 {r['ratio']}%）" for r in base["lifts"]] or ["  - 未来3个月无限售解禁"]
    t = base["tech"]
    L += ["", "## 四、技术面分析", "",
          f"- 现价 {q.get('现价')}；52周区间 **{t['52周低']} ~ {t['52周高']}**",
          f"- 距52周高点 {t['距52周高点%']:.1f}%｜距52周低点 +{t['距52周低点%']:.1f}%｜相对MA250 {t['相对MA250%']:+.1f}%｜相对MA20 {t['相对MA20%']:+.1f}%",
          f"- 动量：60日 {t['近60日%']:+.1f}%｜120日 {(t.get('近120日%') or 0):+.1f}%｜年初至今 {t.get('YTD%') or 0:+.1f}%",
          "", "## 五、持仓决策（三态：持有/加仓/弃仓）", ""]
    if hd:
        L += [f"### 结论：**{hd['action']}**", "", hd["summary"], "",
              "| 动作 | 价位 | 说明 |", "|---|---|---|"]
        L += [f"| {l['label']} | {l['price'] if l['price'] is not None else '—'} | {l['note']} |" for l in hd["levels"]]
        L += [""] + [f"- {n}" for n in hd["notes"]]
    L += ["", "## 六、风险因子逐层筛查（8层）", "",
          f"**综合风险等级：{rl['level']}（风险分 {rl['score']}，RISK=3分/WARN=1分）**", "",
          "| 风险层 | 状态 | 数据日期 | 说明 |", "|---|---|---|---|"]
    st_ico = {"OK": "✅", "WARN": "⚠️", "RISK": "🔴", "NA": "—"}
    L += [f"| {l['name']} | {st_ico[l['status']]} {l['status']} | {l['date'] or '—'} | {l['detail']} |" for l in rl["layers"]]
    L += ["", "## 七、专家意见与风险清单", ""] + base["opinion"]
    L += ["", "---",
          "*本报告由开源工具 a-stock-scanner 自动生成，全部数据来自公开免费接口，仅供研究参考，不构成投资建议。"
          "评分与决策基于数据范围声明所列快照，正式财报披露后应重新运行。*", ""]
    os.makedirs(OUT_DIR, exist_ok=True)
    md_path = os.path.abspath(os.path.join(OUT_DIR, f"深度分析_{c}_{ss.TODAY}.md"))
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))

    base["holding_decision"] = hd
    base["data_range"] = dr
    base["risk_layers"] = rl
    base["kline_source"] = ksrc
    base["kline_span"] = f"{dates[0]} ~ {dates[-1]}"
    base["report_file"] = md_path
    return base


# ---------------- 全景扫描(JSON) ----------------

def scan_json(code):
    code = re.sub(r"\D", "", code)
    if not code:
        return {"error": "无效代码"}
    mkt, sfx, _ = ss.market(code)
    quote = ss.fetch_quote(code, mkt) or {}
    time.sleep(0.4)
    closes, dates = fetch_kline_multi(code, mkt)
    tech = ss.tech_metrics(closes, dates) if len(closes) >= 60 else {}
    time.sleep(0.4)
    earn = ss.fetch_earnings(code)
    time.sleep(0.4)
    divs = ss.fetch_dividends(code, sfx)
    lifts = ss.fetch_lift(code, sfx)
    if not earn or not tech:
        return {"error": "数据不足(可能为新上市/停牌/北交所)", "code": code}
    S, K = ss.score_all(earn, tech, quote, divs, lifts)
    f10 = fetch_f10(code, sfx)
    pa = profit_analysis(f10)
    cyc = (pa or {}).get("周期分位")
    kfyoy = (pa or {}).get("扣非YoY")
    K["周期分位"] = cyc
    K["扣非YoY"] = kfyoy
    op_lines, total, g = ss.expert_opinion(quote.get("名称") or code, S, K, tech, K["亏损"])
    if cyc is not None and cyc >= 0.9 and (kfyoy or 0) < 0:
        total = max(0, total - 10)
        g = ss.grade(total)[0]
        op_lines.append(f"- **周期顶部提示**：年度扣非处近10年分位 {int(cyc*100)}% 且同比回落 {kfyoy}%，利润率或不可持续，估值锚应打折")
    elif cyc is not None and cyc >= 0.9:
        op_lines.append(f"- 利润创新高：年度扣非处近10年分位 {int(cyc*100)}% 且仍在增长（成长股特征，非周期风险）")
    # 趋势补充: 4期净利趋势
    profit_seq = [earn[t]["归母净利亿"] for t in ["prevFY", "FY", "prevH1", "H1"] if t in earn and earn[t]["归母净利亿"] is not None]
    trend = "连续增长" if len(profit_seq) >= 3 and all(profit_seq[i] <= profit_seq[i + 1] for i in range(len(profit_seq) - 1)) and profit_seq[-1] > 0 else (
        "连续下滑" if len(profit_seq) >= 3 and all(profit_seq[i] >= profit_seq[i + 1] for i in range(len(profit_seq) - 1)) else "波动")
    scorecard = []
    for k, (a, b, full) in S.items():
        scorecard.append({
            "dim": k, "score": (-(a) if k == "风险扣分" else a + b), "full": (None if k == "风险扣分" else full),
            "note": S["风险扣分"][1] if k == "风险扣分" else "",
        })
    return {
        "code": code, "name": quote.get("名称"), "quote": quote, "tech": tech,
        "earn": [earn[t] for t in ["H1", "FY", "prevH1", "prevFY"] if t in earn],
        "divs": divs[:8], "lifts": [{"date": str(r.get("FREE_DATE"))[:10], "num": r.get("LIFT_NUM"), "ratio": r.get("TOTAL_RATIO")} for r in lifts],
        "scorecard": scorecard, "total": total, "grade": g,
        "decision": build_decision(quote, tech, K, total, g),
        "key": {k: K.get(k) for k in ["ROE", "毛利率", "营收YoY", "净利YoY", "每股现金流", "PE", "PEG", "分红年数", "股息率", "亏损", "周期分位"]},
        "kf": (pa or {}).get("扣非最新"), "kfyoy": (pa or {}).get("扣非YoY"), "cycle": cyc,
        "opinion": op_lines, "profit_trend": trend, "time": ss.TODAY,
    }


# ---------------- HTTP ----------------

HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>A股全景扫描</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--txt:#1a1f27;--sub:#5b6472;--line:#e4e7ec;--up:#d63a3a;--dn:#1a9c6b;--acc:#2b5fd9;--gold:#c98a1b}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--txt);font-size:14px}
.wrap{max-width:1080px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:22px;margin-bottom:4px}
.sub{color:var(--sub);font-size:12px;margin-bottom:16px}
.searchbar{display:flex;gap:8px;margin-bottom:14px}
.searchbar input{flex:1;padding:11px 14px;border:1px solid var(--line);border-radius:10px;font-size:15px;outline:none;background:var(--card)}
.searchbar input:focus{border-color:var(--acc)}
.searchbar button{padding:11px 22px;border:0;border-radius:10px;background:var(--acc);color:#fff;font-size:14px;cursor:pointer}
.sugg{position:relative}
.sugg-list{position:absolute;top:-6px;left:0;right:0;background:var(--card);border:1px solid var(--line);border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.08);max-height:320px;overflow:auto;z-index:10;display:none}
.sugg-item{padding:9px 14px;cursor:pointer;display:flex;justify-content:space-between}
.sugg-item:hover{background:#eef3ff}
.sugg-item .c{color:var(--sub);font-size:12px}
.tabs{display:flex;gap:6px;margin-bottom:14px}
.tabs button{padding:8px 16px;border:1px solid var(--line);background:var(--card);border-radius:20px;cursor:pointer;font-size:13px;color:var(--sub)}
.tabs button.on{background:var(--acc);color:#fff;border-color:var(--acc)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:14px}
.score-hero{display:flex;align-items:center;gap:20px;flex-wrap:wrap}
.gauge{width:110px;height:110px;border-radius:50%;display:flex;flex-direction:column;align-items:center;justify-content:center;color:#fff;flex-shrink:0}
.gauge .n{font-size:30px;font-weight:700;line-height:1}
.gauge .g{font-size:12px;opacity:.9}
.hero-r{flex:1;min-width:220px}
.hero-r .nm{font-size:19px;font-weight:700}
.hero-r .px{font-size:15px;margin:4px 0}
.kv{display:flex;flex-wrap:wrap;gap:6px 18px;color:var(--sub);font-size:12px;margin-top:6px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{color:var(--sub);font-weight:500;font-size:12px}
.bar{height:6px;border-radius:3px;background:#edf0f4;overflow:hidden;margin-top:4px}
.bar i{display:block;height:100%;border-radius:3px}
.op li{margin:6px 0 6px 18px;line-height:1.6}
.op b{color:var(--acc)}
.up{color:var(--up)}.dn{color:var(--dn)}.warn{color:var(--gold)}
.muted{color:var(--sub);font-size:12px}
.loading{padding:60px;text-align:center;color:var(--sub)}
.tag{display:inline-block;padding:2px 8px;border-radius:6px;font-size:11px;background:#eef3ff;color:var(--acc);margin-right:6px}
.logo-t{font-size:13px;color:var(--sub);margin-bottom:2px}
</style>
</head>
<body>
<div class="wrap">
  <h1>A股全景扫描</h1>
  <div class="sub">数据源：腾讯行情 · 东财数据中心 ｜ 评分 = 盈利20+成长20+财务15+估值15+位置15+回报10−风险扣分 ｜ 仅供研究参考</div>

  <div class="tabs">
    <button class="on" data-tab="scan">个股扫描</button>
    <button data-tab="screen">策略选股</button>
    <button data-tab="funnel">多轮漏斗选股</button>
    <button data-tab="logic">选股逻辑</button>
  </div>

  <div id="tab-scan">
    <div class="searchbar sugg">
      <input id="q" placeholder="输入股票代码或名称，如 603019 / 中科曙光 / 贵州茅台" autocomplete="off">
      <button onclick="doScan(document.getElementById('q').value.trim())">扫描</button>
      <div class="sugg-list" id="sugg"></div>
    </div>
    <div id="deepBox" style="display:none;margin-top:14px"></div>
    <div id="result"><div class="card muted">输入代码开始扫描；也可在「策略选股」中点击任意标的直达扫描。</div></div>
  </div>

  <div id="tab-screen" style="display:none">
    <div class="searchbar">
      <button onclick="runScreen('qarp')">价值成长初筛 (QARP)</button>
      <button onclick="runScreen('dividend')">高股息初筛</button>
      <button onclick="runScreen('lowpos')">深度回调初筛</button>
    </div>
    <div id="screenResult" class="card muted">选择一个策略运行全市场初筛（约5~20秒，首次加载全A列表较慢）。初筛仅基于快照指标，选中后请逐股扫描核验。</div>
  </div>

  <div id="tab-funnel" style="display:none">
    <div class="card">
      <div class="logo-t">六轮漏斗：全市场 → 基础质量过滤 → 策略初筛(前60) → 财务核验(逐股实查) → 位置核验(逐股实查K线) → 评分终选(行业分散)</div>
      <div class="searchbar" style="margin-top:10px">
        <select id="fStrat" style="padding:11px;border:1px solid var(--line);border-radius:10px;font-size:14px;background:var(--card)">
          <option value="composite">综合策略 (QARP∪高股息∪深回调)</option>
          <option value="qarp">价值成长 QARP</option>
          <option value="dividend">高股息</option>
          <option value="lowpos">深度回调</option>
        </select>
        <select id="fN" style="padding:11px;border:1px solid var(--line);border-radius:10px;font-size:14px;background:var(--card)">
          <option value="5">最终 5 只</option>
          <option value="10" selected>最终 10 只</option>
          <option value="15">最终 15 只</option>
          <option value="20">最终 20 只</option>
        </select>
        <button onclick="runFunnel()">开始漏斗选股</button>
      </div>
      <p class="muted" style="margin-top:8px">⚠ 财务/位置两轮为逐股实查（并发拉取），全程约 1~3 分钟；若候选不足将返回实际可用数量。</p>
    </div>
    <div id="funnelResult" class="card muted">尚未运行。建议先用「综合策略 + 10只」体验完整漏斗。</div>
  </div>

  <div id="tab-logic" style="display:none">
    <div class="card">
      <h3 style="margin-bottom:10px">评分体系（100分 − 风险扣分）</h3>
      <table>
        <tr><th>维度</th><th>满分</th><th>规则</th></tr>
        <tr><td>盈利能力</td><td>20</td><td>ROE(H1年化)≥15%→12分、≥10%→9、≥6%→6；毛利率≥40%→8、≥25%→6、≥15%→4</td></tr>
        <tr><td>成长性</td><td>20</td><td>营收YoY与净利YoY各10分：≥30%→10、≥15%→8、≥5%→5、≥0→3</td></tr>
        <tr><td>财务健康</td><td>15</td><td>每股经营现金流>0→10分；现金流为正且高增再+5；为负→0</td></tr>
        <tr><td>估值</td><td>15</td><td>PE≤15→12、≤25→9、≤35→6、≤50→3；PEG&lt;0.8 额外+3；亏损→0</td></tr>
        <tr><td>技术位置</td><td>15</td><td>52周回撤15~45%→6分(安全垫)；年线上0~15%→5；60日动量>0→5（右侧）</td></tr>
        <tr><td>股东回报</td><td>10</td><td>分红年数≥5年→5分；股息率≥4%→5、≥2%→3、≥1%→1</td></tr>
        <tr><td>风险扣分</td><td>—</td><td>当期亏损−15｜经营现金流为负−5｜3个月内解禁−8｜PE>100−5｜无分红−3</td></tr>
      </table>
    </div>
    <div class="card">
      <h3 style="margin-bottom:10px">三条策略初筛逻辑</h3>
      <p style="line-height:1.8">
      <b>① 价值成长 QARP</b>：0&lt;PE&lt;30 + 市值&gt;50亿 + 60日跌幅不超20%，按「估值低分+股息加分+超跌加分」排序——捕捉优质公司被错杀。<br>
      <b>② 高股息</b>：股息率≥4% + PE&gt;0，按「股息率×2−PE/4」排序——分红是"无法造假的证据"，适合防守复利。<br>
      <b>③ 深度回调</b>：年初至今跌幅&gt;15% + PE&gt;0，按「超跌深度−估值惩罚」排序——左侧逆向候选池，务必逐股核验基本面再动手。</p>
      <p class="muted" style="margin-top:8px">初筛为快照指标，通过后请点入个股扫描看完整评分，A/B 级才值得进一步研究。</p>
    </div>
    <div class="card">
      <h3 style="margin-bottom:10px">评级与操作映射</h3>
      <table>
        <tr><th>评级</th><th>分数</th><th>操作</th></tr>
        <tr><td>A · 优质股</td><td>≥75</td><td>可考虑建仓/持有，分2~3批</td></tr>
        <tr><td>B · 良好</td><td>≥60</td><td>观察或小仓位</td></tr>
        <tr><td>C · 中性</td><td>≥45</td><td>等待改善信号（动量转正/财报确认）</td></tr>
        <tr><td>D · 偏弱</td><td>&lt;45</td><td>建议回避/减仓</td></tr>
      </table>
    </div>
  </div>
</div>

<script>
const $=s=>document.querySelector(s);
let suggTimer=null,suggPick=false;

document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('.tabs button').forEach(x=>x.classList.remove('on'));
  b.classList.add('on');
  ['scan','screen','funnel','logic'].forEach(t=>$('#tab-'+t).style.display=t===b.dataset.tab?'':'none');
});

const qEl=$('#q'),suggEl=$('#sugg');
qEl.addEventListener('input',()=>{clearTimeout(suggTimer);const v=qEl.value.trim();if(v.length<1){suggEl.style.display='none';return}suggTimer=setTimeout(()=>search(v),200)});
qEl.addEventListener('keydown',e=>{if(e.key==='Enter'){suggEl.style.display='none';doScan(qEl.value.trim())}});
document.addEventListener('click',e=>{if(!e.target.closest('.sugg'))suggEl.style.display='none'});

async function search(q){
  const r=await fetch('/api/search?q='+encodeURIComponent(q));const d=await r.json();
  if(!d.results||!d.results.length){suggEl.style.display='none';return}
  suggEl.innerHTML=d.results.map(s=>`<div class="sugg-item" onclick="pick('${s.code}','${s.name}')"><span>${s.name}</span><span class="c">${s.code}</span></div>`).join('');
  suggEl.style.display='block';
}
function pick(c,n){qEl.value=n+' '+c;suggEl.style.display='none';doScan(c)}

function fmt(v,d=2){return (v===null||v===undefined||v==='-')?'—':Number(v).toFixed(d)}
function pct(v){if(v===null||v===undefined||v==='-')return '—';const n=Number(v);const cls=n>0?'up':(n<0?'dn':'');return `<span class="${cls}">${n>0?'+':''}${n.toFixed(1)}%</span>`}

async function doScan(code){
  code=(code||'').replace(/[^0-9]/g,'');
  if(!code)return;
  $('#result').innerHTML='<div class="card loading">正在扫描 '+code+' …（拉取行情/K线/财务/分红/解禁，约5~10秒）</div>';
  try{
    const r=await fetch('/api/scan?code='+code);const d=await r.json();
    if(d.error){$('#result').innerHTML='<div class="card warn">✗ '+d.error+'</div>';return}
    render(d);
  }catch(e){$('#result').innerHTML='<div class="card warn">请求失败：'+e+'</div>'}
}

function render(d){
  const q=d.quote,t=d.tech,k=d.key;
  const gc=d.total>=75?'#1a9c6b':(d.total>=60?'#2b5fd9':(d.total>=45?'#c98a1b':'#d63a3a'));
  const barC=v=>v>=75?'#1a9c6b':(v>=50?'#2b5fd9':(v>=30?'#c98a1b':'#d63a3a'));
  const sc=d.scorecard.filter(x=>x.dim!=='风险扣分').map(x=>`
    <tr><td>${x.dim}</td><td style="width:130px">${x.score} / ${x.full}<div class="bar"><i style="width:${Math.max(0,Math.min(100,x.score/x.full*100))}%;background:${barC(x.score/x.full*100)}"></i></div></td></tr>`).join('')
    +(d.scorecard.filter(x=>x.dim==='风险扣分').map(x=>`<tr><td>风险扣分</td><td class="warn">${x.score}（${x.note}）</td></tr>`).join(''));
  const earn=d.earn.map(e=>`<tr><td>${e.日期.slice(0,7)}</td><td>${fmt(e.营收亿,1)}</td><td class="${e.归母净利亿<0?'dn':''}">${fmt(e.归母净利亿,2)}</td><td>${fmt(e.ROE)}</td><td>${fmt(e.毛利率,1)}</td><td>${pct(e.营收YoY)}</td><td class="${(e.每股经营现金流||0)<0?'dn':''}">${fmt(e.每股经营现金流,3)}</td></tr>`).join('');
  const divs=d.divs.length?d.divs.map(r=>`<div style="padding:4px 0;border-bottom:1px dashed var(--line)"><span class="muted">${r.公告日}</span> ｜ ${r.方案||''}</div>`).join(''):'<span class="muted">无分红记录</span>';
  const lifts=d.lifts.length?d.lifts.map(r=>`<div class="warn">⚠ ${r.date} 解禁 ${r.num} 股（${r.ratio}%）</div>`).join(''):'<span class="dn">✅ 未来3个月无解禁</span>';
  const dec=d.decision;
  const decColor=dec?{green:'#1a9c6b',orange:'#c98a1b',gold:'#8a7a1b',red:'#d63a3a'}[dec.color]||'#5b6472':'#5b6472';
  const decHtml=dec?`
  <div class="card" style="border-left:4px solid ${decColor}">
    <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:8px">
      <span style="background:${decColor};color:#fff;padding:5px 16px;border-radius:8px;font-size:15px;font-weight:700">${dec.action}</span>
      <span class="muted">价格基准：${dec.basis}（长线口径，盘中/收盘均可）</span>
      <button style="margin-left:auto" onclick="doDeep('${d.code}')">🧪 深度分析（持仓/加仓/弃仓）</button>
    </div>
    <p style="line-height:1.7">${dec.summary}</p>
    <table style="margin-top:10px">
      <tr><th>触发类型</th><th>价格</th><th>距现价</th><th>操作说明</th></tr>
      ${dec.levels.map(l=>`<tr>
        <td>${l.label}</td>
        <td><b>${l.price===null||l.price===undefined?'—':fmt(l.price)}</b></td>
        <td>${l.price===null||l.price===undefined?'—':pct((l.price/(d.quote.现价||1)-1)*100)}</td>
        <td class="muted" style="white-space:normal">${l.note}</td></tr>`).join('')}
    </table>
    ${dec.notes.map(n=>`<p class="muted" style="margin-top:6px">· ${n}</p>`).join('')}
  </div>`:'';

  $('#result').innerHTML=`
  <div class="card">
    <div class="score-hero">
      <div class="gauge" style="background:${gc}"><div class="n">${d.total}</div><div class="g">${d.grade}</div></div>
      <div class="hero-r">
        <div class="nm">${d.name} <span class="muted">${d.code}</span> <span class="tag">${d.profit_trend}</span>${k.亏损?'<span class="tag warn">当期亏损</span>':''}${(d.cycle||0)>=0.9?(d.kfyoy||0)<0?`<span class="tag warn">⚠周期高位回落(分位${Math.round(d.cycle*100)}%)</span>`:`<span class="tag">利润创新高(分位${Math.round(d.cycle*100)}%)</span>`:''}</div>
        <div class="px">现价 <b>${fmt(q.现价)}</b> ${pct(q['涨跌%'])}<span class="muted"> ｜ 总市值 ${fmt(q.总市值亿,0)}亿 ｜ 换手 ${fmt(q.换手率,2)}% ｜ ${d.time}</span></div>
        <div class="kv">
          <span>52周: ${fmt(t['52周低'])} ~ ${fmt(t['52周高'])}</span>
          <span>距高点 ${pct(t['距52周高点%'])}</span>
          <span>距低点 ${pct(t['距52周低点%'])}</span>
          <span>年线 ${pct(t['相对MA250%'])}</span>
          <span>60日 ${pct(t['近60日%'])}</span>
          <span>YTD ${pct(t['YTD%'])}</span>
          <span>PE ${fmt(k.PE,1)} ${k.PEG||''}</span>
          <span>股息率 ${fmt(k.股息率)}%</span>
          <span>扣非(H1) ${d.kf===null||d.kf===undefined?'—':fmt(d.kf,2)+'亿'}${d.kfyoy!==null&&d.kfyoy!==undefined?` <span class="${d.kfyoy<0?'dn':'up'}">(${d.kfyoy>0?'+':''}${d.kfyoy}%)</span>`:''}</span>
        </div>
      </div>
    </div>
  </div>
  ${decHtml}
  <div class="card"><div class="logo-t">评分卡</div><table>${sc}</table></div>
  <div class="card"><div class="logo-t">多期财务</div><table>
    <tr><th>报告期</th><th>营收(亿)</th><th>归母净利(亿)</th><th>ROE%</th><th>毛利率%</th><th>营收YoY</th><th>每股现金流</th></tr>${earn}</table></div>
  <div style="display:flex;gap:14px;flex-wrap:wrap">
    <div class="card" style="flex:1;min-width:280px"><div class="logo-t">分红记录</div>${divs}</div>
    <div class="card" style="flex:1;min-width:280px"><div class="logo-t">限售解禁</div>${lifts}</div>
  </div>
  <div class="card"><div class="logo-t">专家意见</div><ul class="op">${d.opinion.map(l=>`<li>${l}</li>`).join('')}</ul>
    <p class="muted" style="margin-top:10px">*报告由自动化工具生成，仅供研究参考，不构成投资建议；财报披露后应重新评估。*</p></div>`;
}

async function doDeep(code){
  const box=$('#deepBox');
  box.style.display='';
  box.className='card loading';
  box.innerHTML='深度分析中…（在扫描数据基础上追加F10扣非/持仓三态决策/报告生成，约8~15秒）';
  box.scrollIntoView({behavior:'smooth',block:'start'});
  try{
    const r=await fetch('/api/deep?code='+code);const d=await r.json();
    if(d.error){box.className='card warn';box.textContent='✗ '+d.error;return}
    const hd=d.holding_decision;
    const hdColor=hd?{green:'#1a9c6b',orange:'#c98a1b',gold:'#8a7a1b',red:'#d63a3a'}[hd.color]||'#5b6472':'#5b6472';
    const reasonHtml=(hd.reasons&&hd.reasons.length)?`<p style="margin:6px 0"><b>命中硬伤：</b>${hd.reasons.join('；')}</p>`:'';
    const drRows=d.data_range.map(r=>`<tr><td><b>${r['项']}</b></td><td class="muted">${r['来源']}</td><td>${r['覆盖']}</td><td class="muted" style="white-space:normal">${r['口径']}</td></tr>`).join('');
    const rl=d.risk_layers;
    const rlIco={OK:'✅',WARN:'⚠️',RISK:'🔴',NA:'—'};
    const rlLevel=rl?{低:'#1a9c6b',中:'#c98a1b',高:'#d67a1b',极高:'#d63a3a'}[rl.level]||'#5b6472':'#5b6472';
    const rlHtml=rl?`
    <div class="logo-t" style="margin-top:14px">风险因子逐层筛查（8层）· 综合风险：<span style="color:${rlLevel};font-weight:700">${rl.level}</span>（风险分 ${rl.score}）</div>
    <table style="margin-top:8px"><tr><th>风险层</th><th>状态</th><th>数据日期</th><th>说明</th></tr>
    ${rl.layers.map(l=>`<tr><td><b>${l.name}</b></td>
      <td style="white-space:nowrap">${rlIco[l.status]||''} ${l.status}</td>
      <td class="muted">${l.date||'—'}</td>
      <td class="muted" style="white-space:normal">${l.detail}</td></tr>`).join('')}</table>`
    :'';
    box.className='card';
    box.innerHTML=`
    <div class="logo-t">深度分析报告 · ${d.name}（${d.code}）<span class="muted"> ｜ 生成于 ${d.time} ｜ Markdown 已存至 ${d.report_file.split('/').slice(-2).join('/')}</span></div>
    ${hd?`
    <div style="border-left:4px solid ${hdColor};padding:10px 14px;margin:10px 0;background:var(--bg2,rgba(0,0,0,.03));border-radius:0 8px 8px 0">
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
        <span style="background:${hdColor};color:#fff;padding:6px 18px;border-radius:8px;font-size:16px;font-weight:700">已持仓 → ${hd.action}</span>
        <span class="muted">评分 ${d.total}/${d.grade} ｜ K线: ${d.kline_span}（${d.kline_source}）</span>
      </div>
      ${reasonHtml}
      <p style="line-height:1.7;margin:8px 0 0">${hd.summary}</p>
      <table style="margin-top:10px">
        <tr><th>动作</th><th>价位</th><th>距现价</th><th>说明</th></tr>
        ${hd.levels.map(l=>`<tr>
          <td>${l.label}</td>
          <td><b>${l.price===null||l.price===undefined?'—':fmt(l.price)}</b></td>
          <td>${l.price===null||l.price===undefined?'—':pct((l.price/(d.quote.现价||1)-1)*100)}</td>
          <td class="muted" style="white-space:normal">${l.note}</td></tr>`).join('')}
      </table>
      ${hd.notes.map(n=>`<p class="muted" style="margin-top:6px">· ${n}</p>`).join('')}
    </div>
    <div class="logo-t" style="margin-top:14px">数据范围声明（所有结论仅在下述范围内有效）</div>
    <table style="margin-top:8px"><tr><th>数据项</th><th>来源</th><th>覆盖范围</th><th>口径备注</th></tr>${drRows}</table>
    ${rlHtml}
    <p class="muted" style="margin-top:10px">完整报告（六大章节：数据范围/评分/基本面/技术面/持仓决策/风险清单）已保存为 Markdown 文件，可直接分享或归档。</p>`
    :'<p class="warn">持仓决策不可用（行情或K线数据不足）</p>'}`;
  }catch(e){box.className='card warn';box.textContent='请求失败：'+e}
}

async function runScreen(st){  const box=$('#screenResult');
  box.className='card loading';box.textContent='正在全市场初筛…（首次需加载全部A股快照，约10~30秒）';
  try{
    const r=await fetch('/api/screen?strategy='+st);const d=await r.json();
    if(d.error){box.className='card warn';box.textContent='✗ '+d.error;return}
    const names={qarp:'价值成长 QARP',dividend:'高股息',lowpos:'深度回调'};
    const rows=d.stocks.map((s,i)=>`<tr style="cursor:pointer" onclick="pick('${s.code}','${s.name}')">
      <td>${i+1}</td><td>${s.name}</td><td class="muted">${s.code}</td>
      <td>${fmt(s.price)}</td><td>${fmt(s.pe,1)}</td><td>${fmt(s.pb,2)}</td>
      <td>${fmt(s.dy)}%</td><td>${s.mktcap亿?fmt(s.mktcap亿,0)+'亿':'—'}</td>
      <td>${pct(s.chg60)}</td><td>${pct(s.ytd)}</td></tr>`).join('');
    box.className='card';
    box.innerHTML=`<div class="logo-t">${names[st]} 初筛 · 共 ${d.count} 只，显示前 ${d.stocks.length}（点击任意行进入逐股扫描）</div>
    <table><tr><th>#</th><th>名称</th><th>代码</th><th>现价</th><th>PE</th><th>PB</th><th>股息率</th><th>市值</th><th>60日</th><th>YTD</th></tr>${rows}</table>`;
  }catch(e){box.className='card warn';box.textContent='请求失败：'+e}
}

async function runFunnel(){
  const st=$('#fStrat').value, n=$('#fN').value;
  const box=$('#funnelResult');
  box.className='card loading';
  box.innerHTML='漏斗运行中…<br><span class="muted">轮次1-2(快照) → 轮次3(初筛) → 轮次4财务核验(逐股) → 轮次5位置核验(逐股) → 轮次6终选<br>约需 1~3 分钟，请勿关闭页面</span>';
  try{
    const r=await fetch('/api/funnel?strategy='+st+'&final='+n);const d=await r.json();
    if(d.error){box.className='card warn';box.textContent='✗ '+d.error;return}
    const stageNames=['轮次1 · 全市场','轮次2 · 基础质量过滤','轮次3 · 策略初筛','轮次4 · 财务核验','轮次5 · 位置核验','轮次6 · 终选'];
    const maxC=Math.max(...d.stages.map(s=>s.count),1);
    const funnel=d.stages.map((s,i)=>{
      const w=Math.max(8, s.count/maxC*100);
      return `<div style="margin:10px 0">
        <div style="display:flex;justify-content:space-between;font-size:12px"><span><b>${s.name}</b> <span class="muted">${s.rule}</span></span><b>${s.count}</b></div>
        <div style="background:#edf0f4;border-radius:6px;height:22px;overflow:hidden"><div style="width:${w}%;height:100%;background:${i===d.stages.length-1?'#1a9c6b':'#2b5fd9'};border-radius:6px;display:flex;align-items:center;color:#fff;font-size:11px;padding-left:8px">${s.count}</div></div>
        ${Object.keys(s.out||{}).length?'<div class="muted" style="margin-top:3px">剔除：'+Object.entries(s.out).map(([k,v])=>k+'×'+v).join(' ｜ ')+'</div>':''}
      </div>`;
    }).join('');
    const names={composite:'综合策略',qarp:'QARP价值成长',dividend:'高股息',lowpos:'深度回调'};
    const rows=d.finals.map((f,i)=>`<tr style="cursor:pointer" onclick="pick('${f.code}','${f.name}')">
      <td>${i+1}</td><td><b>${f.name}</b><div class="muted">${f.code}</div></td><td>${f.ind||'—'}</td>
      <td><b style="color:${f.total>=75?'#1a9c6b':(f.total>=60?'#2b5fd9':'#c98a1b')}">${f.total}</b> ${f.grade}</td>
      <td>${fmt(f.price)}</td><td>${fmt(f.pe,1)}</td><td>${fmt(f.dy)}%</td>
      <td>${fmt(f.roe,1)}%</td><td class="${(f.cfo||0)<0?'dn':''}">${fmt(f.cfo,3)}</td>
      <td>${f.kfyoy===null||f.kfyoy===undefined?'—':`<span class="${f.kfyoy<0?'dn':'up'}">${f.kfyoy>0?'+':''}${f.kfyoy}%</span>`}</td>
      <td>${pct(f.dd)}</td><td>${pct(f.m60)}</td></tr>`).join('');
    box.className='card';
    box.innerHTML=`<div class="logo-t">${names[st]} · ${d.source} · 候选 ${d.all_graded} 只参与终评，最终选出 ${d.finals.length} 只（点击行进入完整扫描）</div>
      ${funnel}
      <table style="margin-top:12px"><tr><th>#</th><th>标的</th><th>行业</th><th>评分</th><th>现价</th><th>PE</th><th>股息率</th><th>ROE</th><th>每股现金流</th><th>扣非YoY</th><th>距52周高点</th><th>60日</th></tr>${rows}</table>
      <p class="muted" style="margin-top:10px">*漏斗结果仅供研究参考，不构成投资建议；建仓前请点击标的查看完整报告与触发价位。*</p>`;
  }catch(e){box.className='card warn';box.textContent='请求失败：'+e}
}
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        try:
            if u.path == "/":
                self._send(200, "text/html; charset=utf-8", HTML)
            elif u.path == "/api/search":
                q = (qs.get("q", [""])[0] or "").strip().lower()
                stocks = fetch_all_stocks()
                res = []
                for s in stocks:
                    if q.isdigit() and s["code"].startswith(q):
                        res.append(s)
                    elif q and not q.isdigit() and q in s["name"].lower():
                        res.append(s)
                    if len(res) >= 12:
                        break
                self._send(200, "application/json; charset=utf-8",
                           json.dumps({"results": res}, ensure_ascii=False))
            elif u.path == "/api/funnel":
                st = qs.get("strategy", ["composite"])[0]
                try:
                    n = max(3, min(20, int(qs.get("final", ["10"])[0])))
                except Exception:
                    n = 10
                res = funnel(st, n)
                self._send(200, "application/json; charset=utf-8",
                           json.dumps(res, ensure_ascii=False))
            elif u.path == "/api/scan":
                d = scan_json(qs.get("code", [""])[0])
                self._send(200, "application/json; charset=utf-8",
                           json.dumps(d, ensure_ascii=False))
            elif u.path == "/api/deep":
                d = deep_analysis(qs.get("code", [""])[0])
                self._send(200, "application/json; charset=utf-8",
                           json.dumps(d, ensure_ascii=False))
            elif u.path == "/api/screen":
                st = qs.get("strategy", ["qarp"])[0]
                if st not in ("qarp", "dividend", "lowpos"):
                    self._send(400, "application/json; charset=utf-8", json.dumps({"error": "未知策略"}))
                    return
                res = screen(st)
                self._send(200, "application/json; charset=utf-8",
                           json.dumps(res, ensure_ascii=False))
            else:
                self._send(404, "text/plain", "not found")
        except Exception as e:
            try:
                self._send(500, "application/json; charset=utf-8", json.dumps({"error": str(e)}, ensure_ascii=False))
            except Exception:
                pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    print(f"A股全景扫描 Web 版: http://localhost:{port}", flush=True)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
