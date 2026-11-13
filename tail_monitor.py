import datetime
import os
import time
import requests

# ==========================================
# 如果你有特定想盯盘的股票，把代码填在下面括号里，例如：["600519", "000858"]
# 如果保持为空 []，则只执行全市场 5000+ 只股票自动海选
MY_STOCKS = []
# ==========================================

EVENT_NAME = os.environ.get("GITHUB_EVENT_NAME", "schedule")


def save_report(title: str, content: str):
    """将选股结果直接写成仓库首页 README.md 表格，无需任何推送配置"""
    report_md = f"# 📊 {title}\n\n{content}\n"

    with open("README.md", "w", encoding="utf-8") as f:
        f.write(report_md)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(report_md)

    print(report_md)


def wait_until_1440():
    """定时任务精准卡点 14:40；手动点击 Run workflow 则立即查询"""
    if EVENT_NAME == "workflow_dispatch":
        print("检测到手动触发，跳过等待，立即按当前时间扫描全市场...")
        return

    now = datetime.datetime.now()
    target = now.replace(hour=14, minute=40, second=0, microsecond=0)
    if now < target:
        time.sleep((target - now).total_seconds())


def format_symbol(code: str) -> str:
    code = code.strip()
    if code.startswith(("sh", "sz")):
        return code
    return f"sh{code}" if code.startswith("6") else f"sz{code}"


def generate_all_a_share_codes() -> list:
    """生成沪深主板与创业板代码池（自动排除ST、北交所、科创板）"""
    codes = []
    # 沪市主板 (600/601/603/605)
    for prefix in [600, 601, 603]:
        codes.extend([f"sh{prefix}{i:03d}" for i in range(1000)])
    codes.extend([f"sh605{i:03d}" for i in range(600)])
    # 深市主板 (000/001/002/003)
    for prefix in ["000", "001", "002"]:
        codes.extend([f"sz{prefix}{i:03d}" for i in range(1000)])
    codes.extend([f"sz003{i:03d}" for i in range(300)])
    # 创业板 (300/301)
    codes.extend([f"sz300{i:03d}" for i in range(1000)])
    codes.extend([f"sz301{i:03d}" for i in range(700)])
    return codes


def fetch_tencent_quotes(symbols: list) -> list:
    """使用腾讯财经高速批量接口拉取实时行情（不封海外IP，3秒扫完全市场）"""
    results = []
    session = requests.Session()
    batch_size = 550

    for i in range(0, len(symbols), batch_size):
        batch = symbols[i : i + batch_size]
        url = f"http://qt.gtimg.cn/q={','.join(batch)}"
        try:
            resp = session.get(url, timeout=10)
            resp.encoding = "gbk"
            for line in resp.text.splitlines():
                if "~" not in line:
                    continue
                parts = line.split("~")
                if len(parts) < 50:
                    continue
                name = parts[1].strip()
                code = parts[2].strip()
                if not name or "ST" in name or "退" in name:
                    continue

                price = float(parts[3] or 0)
                open_p = float(parts[5] or 0)
                if price <= 0 or open_p <= 0:
                    continue

                results.append({
                    "symbol": f"sh{code}" if code.startswith("6") else f"sz{code}",
                    "代码": code,
                    "名称": name,
                    "最新价": price,
                    "今开": open_p,
                    "最高": float(parts[33] or 0),
                    "涨跌幅": float(parts[32] or 0),
                    "换手率": float(parts[38] or 0),
                    "流通市值_亿": float(parts[44] or 0),
                    "量比": float(parts[49] or 0),
                })
        except Exception as e:
            print(f"分批拉取警告: {e}")
    return results


def check_ma_bullish(symbol: str, current_price: float):
    """通过腾讯日K线接口计算 MA5 与 MA20"""
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,25,qfq"
    try:
        resp = requests.get(url, timeout=8)
        data = resp.json().get("data", {}).get(symbol, {})
        klines = data.get("qfqday") or data.get("day") or []
        if len(klines) < 20:
            return False, 0.0, 0.0
        closes = [float(k[2]) for k in klines]
        ma5 = round(sum(closes[-5:]) / 5, 2)
        ma20 = round(sum(closes[-20:]) / 20, 2)
        return (current_price > ma5 > ma20), ma5, ma20
    except Exception:
        return False, 0.0, 0.0


def main():
    wait_until_1440()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. 扫描全市场行情
    all_codes = generate_all_a_share_codes()
    stocks = fetch_tencent_quotes(all_codes)

    if not stocks:
        save_report("尾盘监测：网络波动", f"- **查询时间**：{now_str}\n- **状态**：未获取到行情数据，请稍后重试。")
        return

    # 2. 统计大盘情绪
    up_count = sum(1 for s in stocks if s["涨跌幅"] > 0)
    down_limit_count = sum(1 for s in stocks if s["涨跌幅"] <= -9.8)
    market_status = f"统计A股 **{len(stocks)}** 只 | 上涨 **{up_count}** 家 | 跌停(非ST) **{down_limit_count}** 家"

    sections = [
        f"- **查询时间**：{now_str}",
        f"- **大盘环境**：{market_status}\n",
    ]

    # 3. 如果你填了自选股 MY_STOCKS，优先生成你的自选股诊断表
    if MY_STOCKS:
        my_symbols = [format_symbol(c) for c in MY_STOCKS]
        my_quotes = fetch_tencent_quotes(my_symbols)
        if my_quotes:
            my_table = "### 🎯 我的指定股票实时诊断\n\n| 代码 | 名称 | 最新价 | 涨跌幅 | 量比 | 换手率 | MA5 | MA20 | 均线状态 |\n| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n"
            for s in my_quotes:
                is_bull, ma5, ma20 = check_ma_bullish(s["symbol"], s["最新价"])
                status_str = "✅ 多头强势" if is_bull else "⚠️ 暂未多头"
                my_table += f"| {s['代码']} | {s['名称']} | {s['最新价']} | {s['涨跌幅']}% | {s['量比']} | {s['换手率']}% | {ma5} | {ma20} | {status_str} |\n"
            sections.append(my_table)

    # 4. 全市场 14:40 尾盘六维筛选
    candidates = [
        s for s in stocks
        if 2.5 <= s["涨跌幅"] <= 5.5
        and s["量比"] >= 1.3
        and 5.0 <= s["换手率"] <= 12.0
        and 30.0 <= s["流通市值_亿"] <= 250.0
        and s["最新价"] >= s["最高"] * 0.985
        and s["最新价"] > s["今开"]
    ]
    candidates.sort(key=lambda x: x["量比"], reverse=True)

    final_rows = []
    for s in candidates[:15]:
        is_bull, ma5, ma20 = check_ma_bullish(s["symbol"], s["最新价"])
        if is_bull:
            final_rows.append(
                f"| {s['代码']} | {s['名称']} | {s['最新价']} | {s['涨跌幅']}% | {s['量比']} | {s['换手率']}% | {ma5} | {ma20} | {round(s['流通市值_亿'], 1)}亿 |"
            )

    if up_count < 1500 or down_limit_count >= 15:
        sections.append(
            "### ⚠️ 大盘风控提醒\n> 当前上涨家数偏少或跌停较多，触发防御风控阈值，**建议今日空仓观望**。\n"
        )

    if final_rows:
        header = "### 🚀 全市场 14:40 尾盘精选池（展示前 5）\n\n| 代码 | 名称 | 最新价 | 涨跌幅 | 量比 | 换手率 | MA5 | MA20 | 流通市值 |\n| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n"
        body = "\n".join(final_rows[:5])
        sections.append(
            f"{header}{body}\n\n"
            f"> **操作纪律**：买前请核对分时白线是否全天在黄线（均价线）上方，避开尾盘直线急拉诱多票；次日早盘 10:00 前准时离场。"
        )
        save_report(f"尾盘监测：精选出 {len(final_rows)} 只符合标准标的", "\n".join(sections))
    else:
        sections.append(
            "### 📭 全市场筛选结论\n经过涨幅(2.5%~5.5%)、量比(>1.3)、换手率(5%~12%)及 MA5>MA20 多头过滤，**当前 0 只股票命中标准，建议空仓休息**。"
        )
        save_report("尾盘监测：当前无合适标的", "\n".join(sections))


if __name__ == "__main__":
    main()
