#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股全景扫描评估工具 (Pure stdlib, 零依赖)
用法: python3 stock_scanner.py 603019 [更多代码...]
输出: output/股票报告_<code>_<日期>.md + 终端摘要 (评分/评级/专家意见)

数据源:
  腾讯行情 qt.gtimg.cn        -> 实时价/PE_TTM/市值/换手
  腾讯K线 web.ifzq.gtimg.cn    -> 2年日K(前复权): 位置/均线/动量
  东财 datacenter-web          -> 业绩报表(ROE/毛利率/增速/每股现金流)/分红/解禁
评分体系 (100分 - 风险扣分):
  盈利能力20 + 成长性20 + 财务健康15 + 估值15 + 技术位置15 + 股东回报10 - 风险扣分
"""
import json, ssl, sys, time, re, os
import urllib.request
from urllib.parse import quote
from datetime import date, datetime

TODAY = date.today().strftime("%Y-%m-%d")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output")
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def gj(url, retry=4, timeout=20):
    for i in range(retry + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout, context=ctx) as r:
                return json.loads(r.read().decode())
        except Exception:
            if i == retry:
                return None
            time.sleep(2)


def fnum(x):
    try:
        v = float(x)
        return v if v == v else None
    except Exception:
        return None


def market(code):
    if code.startswith(("6", "9", "5")):
        return "sh", ".SH", "1"
    if code.startswith(("4", "8")):
        return "bj", ".BJ", "0"
    return "sz", ".SZ", "0"


def sec_filter_date(code, dstr):
    return quote(f'(SECURITY_CODE="{code}")(REPORTDATE=\'{dstr}\')')


# ---------------- 数据采集 ----------------

def fetch_quote(code, mkt):
    try:
        url = f"https://qt.gtimg.cn/q={mkt}{code}"
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
            p = r.read().decode("gbk").split("~")
        if len(p) < 46:
            return None
        return {
            "名称": p[1], "现价": fnum(p[3]), "涨跌%": fnum(p[32]),
            "换手率": fnum(p[38]), "PE_TTM": fnum(p[39]),
            "流通市值亿": fnum(p[44]), "总市值亿": fnum(p[45]),
            "行情时间": p[30] if len(p) > 30 else "",
        }
    except Exception:
        return None


def fetch_kline(code, mkt, days=500):
    beg = f"{int(TODAY[:4]) - 2}-10-01"
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"param={mkt}{code},day,{beg},{TODAY},{days},qfq")
    d = gj(url)
    data = ((d or {}).get("data") or {}).get(f"{mkt}{code}") or {}
    kl = data.get("qfqday") or data.get("day") or []
    closes = [float(x[2]) for x in kl]
    dates = [x[0] for x in kl]
    return closes, dates if closes else ([], [])


def fetch_earnings(code):
    """多期业绩: 最近中报/年报 + 上年同期"""
    m, y = int(TODAY[5:7]), int(TODAY[:4])
    this_h1, this_fy = f"{y}-06-30", f"{y - 1}-12-31"
    prev_h1, prev_fy = f"{y - 1}-06-30", f"{y - 2}-12-31"
    out = {}
    for tag, rd in [("H1", this_h1), ("FY", this_fy), ("prevH1", prev_h1), ("prevFY", prev_fy)]:
        url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_LICO_FN_CPD"
               f"&columns=ALL&pageSize=3&pageNumber=1&filter={sec_filter_date(code, rd)}")
        d = gj(url)
        data = ((d or {}).get("result") or {}).get("data") or []
        if data:
            r = data[0]
            out[tag] = {
                "日期": rd, "营收亿": round((fnum(r.get("TOTAL_OPERATE_INCOME")) or 0) / 1e8, 1),
                "归母净利亿": round((fnum(r.get("PARENT_NETPROFIT")) or 0) / 1e8, 2),
                "ROE": round(fnum(r.get("WEIGHTAVG_ROE")), 2) if fnum(r.get("WEIGHTAVG_ROE")) is not None else None,
                "毛利率": round(fnum(r.get("XSMLL")), 2) if fnum(r.get("XSMLL")) is not None else None,
                "营收YoY": round(fnum(r.get("YSTZ")), 2) if fnum(r.get("YSTZ")) is not None else None,
                "净利YoY": round(fnum(r.get("SJLTZ")), 2) if fnum(r.get("SJLTZ")) is not None else None,
                "每股经营现金流": round(fnum(r.get("MGJYXJJE")), 3) if fnum(r.get("MGJYXJJE")) is not None else None,
            }
        time.sleep(0.4)
    return out


def fetch_dividends(code, mkt_sfx, n=12):
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?sortColumns=PLAN_NOTICE_DATE"
           "&sortTypes=-1&pageSize=12&pageNumber=1&reportName=RPT_SHAREBONUS_DET&columns=ALL"
           "&filter=" + quote(f'(SECUCODE="{code}{mkt_sfx}")'))
    d = gj(url)
    data = ((d or {}).get("result") or {}).get("data") or []
    rows = []
    for r in data[:n]:
        rows.append({"公告日": str(r.get("PLAN_NOTICE_DATE"))[:10], "方案": r.get("IMPL_PLAN_PROFILE")})
    return rows


def fetch_lift(code, mkt_sfx):
    m, y = int(TODAY[5:7]), int(TODAY[:4])
    end = f"{y + 1 if m > 9 else y}-{(m + 3) % 12 or 12:02d}-28"
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_LIFT_STAGE"
           f"&columns=ALL&pageSize=5&pageNumber=1&filter="
           + quote(f'(SECURITY_CODE="{code}")(FREE_DATE>=\'{TODAY}\')(FREE_DATE<=\'{end}\')'))
    d = gj(url)
    return ((d or {}).get("result") or {}).get("data") or []


# ---------------- 技术指标 ----------------

def tech_metrics(closes, dates):
    cur = closes[-1]
    w52 = closes[-244:] if len(closes) >= 244 else closes
    hi, lo = max(w52), min(w52)
    ma250 = sum(closes[-250:]) / min(250, len(closes))
    ma20 = sum(closes[-20:]) / min(20, len(closes))
    dd = round((1 - cur / hi) * 100, 1)          # 距52周高点回撤%
    off_lo = round((cur / lo - 1) * 100, 1)      # 距52周低点%
    pos_ma250 = round((cur / ma250 - 1) * 100, 1)
    pos_ma20 = round((cur / ma20 - 1) * 100, 1)
    base = None
    for dt, v in zip(dates, closes):
        if dt.startswith(TODAY[:4] - 1 if False else str(int(TODAY[:4]) - 1) + "-12"):
            base = v
    ytd = round((cur / base - 1) * 100, 1) if base else None
    m60 = round((cur / closes[-61] - 1) * 100, 1) if len(closes) >= 61 else None
    m120 = round((cur / closes[-121] - 1) * 100, 1) if len(closes) >= 121 else None
    return {"现价": cur, "52周高": hi, "52周低": lo, "距52周高点%": -dd,
            "距52周低点%": off_lo, "相对MA250%": pos_ma250, "相对MA20%": pos_ma20,
            "YTD%": ytd, "近60日%": m60, "近120日%": m120}


# ---------------- 评分 ----------------

def band(v, cuts, scores):
    for c, s in zip(cuts, scores):
        if v is not None and v >= c:
            return s
    return scores[-1] if v is not None else 0


def score_all(earn, tech, quote, divs, lifts):
    S, notes = {}, []
    h1, prev_h1 = earn.get("H1", {}), earn.get("prevH1", {})
    fy = earn.get("FY", {})
    roe = h1.get("ROE")
    gm = h1.get("毛利率") or fy.get("毛利率")
    rev_yoy = h1.get("营收YoY") if h1.get("营收YoY") is not None else fy.get("营收YoY")
    np_yoy = h1.get("净利YoY") if h1.get("净利YoY") is not None else fy.get("净利YoY")
    cfo = h1.get("每股经营现金流")
    eps_proxy = None
    if h1.get("归母净利亿") and quote.get("总市值亿"):
        eps_proxy = h1["归母净利亿"] * 1e8 / (quote["总市值亿"] * 1e8 / max(quote["现价"], 0.01)) / 1  # 每股收益近似
    loss = (h1.get("归母净利亿") or 0) < 0 if h1 else False
    pe = quote.get("PE_TTM")

    # 1 盈利能力 20
    s_roe = band(roe * 2 if roe is not None else None, [15, 10, 6, 0.1], [12, 9, 6, 3])  # H1年化
    s_gm = band(gm, [40, 25, 15, 5], [8, 6, 4, 2])
    S["盈利能力"] = (s_roe, s_gm, 20)

    # 2 成长性 20
    s_rev = band(rev_yoy, [30, 15, 5, 0], [10, 8, 5, 3])
    s_np = band(np_yoy, [30, 15, 5, 0], [10, 8, 5, 3])
    S["成长性"] = (s_rev, s_np, 20)

    # 3 财务健康 15
    s_cfo = 10 if (cfo or 0) > 0 else 0
    s_extra = 5 if loss else (3 if (cfo or 0) > 0 and rev_yoy and rev_yoy > 20 else 0)
    S["财务健康"] = (s_cfo, s_extra, 15)

    # 4 估值 15
    if loss or (pe is not None and pe <= 0):
        s_pe = 0
    else:
        s_pe = band(pe, [15, 25, 35, 50], [12, 9, 6, 3])
    peg_hint = ""
    if pe and rev_yoy and rev_yoy > 0:
        peg = pe / rev_yoy
        peg_hint = f"PEG≈{peg:.2f}"
        if peg < 0.8:
            s_pe = min(15, s_pe + 3)
    S["估值"] = (s_pe, 0, 15)

    # 5 技术位置 15
    dd = -tech["距52周高点%"]
    s_dd = 6 if 15 <= dd <= 45 else (4 if dd > 45 else 2)
    pm = tech["相对MA250%"]
    s_ma = 5 if 0 <= pm <= 15 else (4 if -10 <= pm < 0 else (3 if pm > 15 else 2))
    m60 = tech["近60日%"]
    s_mo = 5 if (m60 or -99) > 0 else (4 if (m60 or -99) >= -5 else (2 if (m60 or -99) >= -15 else 0))
    S["技术位置"] = (s_dd, s_ma + s_mo if s_ma + s_mo <= 9 else 9, 15)

    # 6 股东回报 10
    yrs = set(r["公告日"][:4] for r in divs if r["公告日"])
    n_yrs = len(yrs)
    div_txt = " ".join(r["方案"] or "" for r in divs[:3])
    dy = 0.0
    for mm in re.findall(r"10\s*(?:派|转增)?\s*([\d.]+)\s*元", div_txt):
        pass
    s_div = 5 if n_yrs >= 5 else (3 if n_yrs >= 3 else 0)
    # 股息率: 取最近一笔"10派X元"按现价折算
    dy = 0.0
    price = quote.get("现价") or 0
    for r in divs:
        m = re.search(r"10\s*派\s*([\d.]+)", r.get("方案") or "")
        if m and price:
            dy = float(m.group(1)) / 10 / price * 100
            break
    s_dy = band(dy, [4, 2, 1], [5, 3, 1])
    S["股东回报"] = (s_div, s_dy, 10)

    # 7 风险扣分
    risk, risk_notes = 0, []
    if loss:
        risk += 15; risk_notes.append("当期亏损(-15)")
    if cfo is not None and cfo < 0:
        risk += 5; risk_notes.append("经营现金流为负(-5)")
    if lifts:
        risk += 8; risk_notes.append(f"未来3个月有限售解禁({len(lifts)}笔, -8)")
    if pe and pe > 100:
        risk += 5; risk_notes.append("PE>100(-5)")
    if n_yrs == 0:
        risk += 3; risk_notes.append("近年无分红记录(-3)")
    S["风险扣分"] = (risk, "; ".join(risk_notes) or "无", 0)

    return S, {"ROE": roe, "毛利率": gm, "营收YoY": rev_yoy, "净利YoY": np_yoy,
               "每股现金流": cfo, "PE": pe, "PEG": peg_hint if loss is False else "",
               "分红年数": n_yrs, "股息率": round(dy, 2), "亏损": loss}


def grade(total):
    if total >= 75: return "A · 优质股", "可考虑建仓/持有"
    if total >= 60: return "B · 良好", "观察或小仓位"
    if total >= 45: return "C · 中性", "等待改善信号"
    return "D · 偏弱", "建议回避/减仓"


def expert_opinion(name, S, K, tech, loss):
    lines = []
    total = sum(s[0] + s[1] for k, s in S.items() if k != "风险扣分") - S["风险扣分"][0]
    g, act = grade(total)
    strengths = sorted([(k, s[0] + s[1], s[2]) for k, s in S.items() if k != "风险扣分"],
                       key=lambda x: x[1] / max(x[2], 1), reverse=True)
    top = strengths[0][0] if strengths else ""
    weak = strengths[-1][0] if strengths else ""
    lines.append(f"**评级：{g}（{total}/100 分）→ {act}**")
    lines.append("")
    lines.append(f"- **最强维度**：{top}；**最弱维度**：{weak}")
    if K["ROE"] is not None: lines.append(f"- 盈利：H1 ROE {K['ROE']:.1f}%（年化约 {K['ROE']*2:.0f}%），毛利率 {K['毛利率'] or 0:.1f}%")
    if K["营收YoY"] is not None: lines.append(f"- 成长：营收 YoY {K['营收YoY']:.1f}%，净利 YoY {K['净利YoY']}%")
    if K["PE"]: lines.append(f"- 估值：PE_TTM {K['PE']:.1f} {K['PEG']}")
    lines.append(f"- 现金流：每股经营现金流 {K['每股现金流'] if K['每股现金流'] is not None else 'N/A'} 元；分红年数 {K['分红年数']} 年，股息率约 {K['股息率']}%")
    lines.append(f"- 位置：距52周高点 {tech['距52周高点%']}%，相对年线 {tech['相对MA250%']}%，近60日 {tech['近60日%']}%")
    if S["风险扣分"][0]: lines.append(f"- **风险扣分**：{S['风险扣分'][1]}")
    lines.append("")
    # 操作触发
    m60 = tech["近60日%"] or -99
    if loss:
        lines.append("- 操作建议：当前亏损状态，任何买入需等待单季减亏/扭亏 + 60日动量转正的双确认。")
    elif m60 >= -5 and (tech["距52周高点%"] * -1) >= 15:
        lines.append(f"- 操作建议：深回撤({tech['距52周高点%']}%)+已企稳(60日 {tech['近60日%']}%)，具备左侧布局条件；建议分 2~3 批建仓，破 52 周低点止损。")
    elif m60 > 0:
        lines.append("- 操作建议：右侧动量为正，可持有或回调分批加仓；追高需控制单笔仓位。")
    else:
        lines.append("- 操作建议：仍在下跌通道，等待 60 日动量转正或站上 20 日线再介入。")
    return lines, total, g


# ---------------- 主流程 ----------------

def scan(code):
    mkt, sfx, em = market(code)
    print(f"[{code}] 拉取行情...", flush=True)
    quote = fetch_quote(code, mkt) or {}
    time.sleep(0.5)
    print(f"[{code}] 拉取K线...", flush=True)
    closes, dates = fetch_kline(code, mkt)
    tech = tech_metrics(closes, dates) if len(closes) >= 60 else {}
    time.sleep(0.5)
    print(f"[{code}] 拉取多期业绩...", flush=True)
    earn = fetch_earnings(code)
    time.sleep(0.5)
    print(f"[{code}] 拉取分红/解禁...", flush=True)
    divs = fetch_dividends(code, sfx)
    lifts = fetch_lift(code, sfx)

    if not earn or not tech:
        print(f"[{code}] 数据不足，跳过。earn={bool(earn)} kline={len(closes)}")
        return None

    S, K = score_all(earn, tech, quote, divs, lifts)
    name = quote.get("名称") or code
    op_lines, total, g = expert_opinion(name, S, K, tech, K["亏损"])

    # ---- 报告 ----
    L = []
    L.append(f"# {name}（{code}）全景扫描报告")
    L.append(f"\n*生成时间：{TODAY} {datetime.now().strftime('%H:%M')} | 数据源：腾讯行情/东财数据中心 | 仅供研究参考，不构成投资建议*\n")
    L.append("## 一、总评分卡\n")
    L.append("| 维度 | 得分/满分 | 明细 |")
    L.append("|---|---|---|")
    det = {
        "盈利能力": f"ROE(H1) {K['ROE'] or 'N/A'}% / 毛利率 {K['毛利率'] or 'N/A'}%",
        "成长性": f"营收YoY {K['营收YoY']}% / 净利YoY {K['净利YoY']}%",
        "财务健康": f"每股经营现金流 {K['每股现金流']}",
        "估值": f"PE_TTM {K['PE']} {K['PEG']}",
        "技术位置": f"距高点 {tech['距52周高点%']}% / 年线 {tech['相对MA250%']}% / 60日 {tech['近60日%']}%",
        "股东回报": f"近5年分红 {K['分红年数']} 年 / 股息率 {K['股息率']}%",
        "风险扣分": S["风险扣分"][1],
    }
    for k, (a, b, full) in S.items():
        L.append(f"| {k} | {a + b if k != '风险扣分' else -a} / {full if k != '风险扣分' else '—'} | {det.get(k, '')} |")
    L.append(f"| **总分** | **{total} / 100** | **评级 {g}** |")

    L.append("\n## 二、行情与位置\n")
    L.append(f"- 现价 **{quote.get('现价')}**（{quote.get('涨跌%')}%），总市值 {quote.get('总市值亿')} 亿，换手率 {quote.get('换手率')}%")
    L.append(f"- 52周区间：{tech['52周低']} ~ {tech['52周高']}；现价距高点 {tech['距52周高点%']}%、距低点 {tech['距52周低点%']}%")
    L.append(f"- 均线：相对年线(MA250) {tech['相对MA250%']}%，相对20日线 {tech['相对MA20%']}%")
    L.append(f"- 动量：年初至今 {tech['YTD%']}%，近60日 {tech['近60日%']}%")

    L.append("\n## 三、多期财务\n")
    L.append("| 报告期 | 营收(亿) | 归母净利(亿) | ROE% | 毛利率% | 营收YoY% | 每股现金流 |")
    L.append("|---|---|---|---|---|---|---|")
    for tag in ["H1", "FY", "prevH1", "prevFY"]:
        e = earn.get(tag)
        if e:
            L.append(f"| {e['日期'][:7]} | {e['营收亿']} | {e['归母净利亿']} | {e['ROE']} | {e['毛利率']} | {e['营收YoY']} | {e['每股经营现金流']} |")

    L.append("\n## 四、分红记录（近12笔）\n")
    if divs:
        for r in divs[:8]:
            L.append(f"- {r['公告日']} | {r['方案']}")
    else:
        L.append("- 无分红记录")

    L.append("\n## 五、限售解禁（未来3个月）\n")
    if lifts:
        for r in lifts:
            L.append(f"- {str(r.get('FREE_DATE'))[:10]} 解禁 {r.get('LIFT_NUM')} 股，占比 {r.get('TOTAL_RATIO')}%")
    else:
        L.append("- ✅ 无解禁")

    L.append("\n## 六、专家意见\n")
    L.extend(op_lines)
    L.append("\n---\n*本报告由自动化工具生成，评分基于公开数据快照，财报披露后应重新评估。*")

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"股票报告_{code}_{TODAY}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[{code}] {name} 总分 {total} → {g} | 报告: {path}")
    return path, total, g


if __name__ == "__main__":
    codes = [re.sub(r"\D", "", a) for a in sys.argv[1:] if re.sub(r"\D", "", a)]
    if not codes:
        print("用法: python3 stock_scanner.py 603019 [601012 ...]")
        sys.exit(1)
    for c in codes:
        scan(c)
