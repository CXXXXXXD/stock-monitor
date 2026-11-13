import datetime
import os
import sys
import time
import akshare as ak
import pandas as pd
import requests

# 自动读取你在 GitHub Secrets 里配置的通知凭证
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip()
EVENT_NAME = os.environ.get("GITHUB_EVENT_NAME", "schedule")


def send_notification(title: str, content: str):
    """万能通知函数：自动识别 PushPlus(个人微信)、飞书、企业微信"""
    if not WEBHOOK_URL:
        print("未检测到 WEBHOOK_URL 凭证，仅在日志中打印：\n", title, "\n", content)
        return

    full_text = f"【{title}】\n{content}"

    # 情况1：如果填的是 PushPlus 的 Token（不以 http 开头），直接推送到个人微信
    if not WEBHOOK_URL.startswith("http"):
        url = "http://www.pushplus.plus/send"
        payload = {
            "token": WEBHOOK_URL,
            "title": title,
            "content": content.replace("\n", "<br>"),
            "template": "html"
        }
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        print(f"微信 PushPlus 推送成功: {title}")
    # 情况2：如果填的是飞书或企业微信的 https 机器人网址
    else:
        payload = {
            "msg_type": "text",
            "msgtype": "text",
            "content": {"text": full_text},
            "text": {"content": full_text},
        }
        resp = requests.post(WEBHOOK_URL, json=payload, timeout=15)
        resp.raise_for_status()
        print(f"Webhook 机器人推送成功: {title}")


def wait_until_1440():
    """如果是每日定时任务唤醒，精准等待至北京时间 14:40:00；如果是手动点击测试，则立即执行"""
    if EVENT_NAME == "workflow_dispatch":
        print("检测到你手动点击了测试按钮，跳过等待，立即为你执行选股并发送测试通知...")
        return

    now = datetime.datetime.now()
    target = now.replace(hour=14, minute=40, second=0, microsecond=0)

    if now < target:
        wait_seconds = (target - now).total_seconds()
        print(f"当前时间 {now.strftime('%H:%M:%S')}，距离 14:40:00 还有 {wait_seconds:.1f} 秒，正在精准卡点等待...")
        time.sleep(wait_seconds)
    else:
        print(f"当前时间 {now.strftime('%H:%M:%S')} 已到或已过 14:40，立即开始扫描...")


def is_trading_day() -> bool:
    """检查今天是不是A股法定节假日"""
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    try:
        trade_dates = ak.tool_trade_date_hist_sina()
        date_list = pd.to_datetime(trade_dates["trade_date"]).dt.strftime("%Y-%m-%d").values
        return today_str in date_list
    except Exception as e:
        print(f"交易日历获取失败，默认按交易日处理: {e}")
        return True


def fetch_spot_with_retry(retries: int = 3) -> pd.DataFrame:
    """获取A股实时行情（自带3次防抖重试）"""
    for attempt in range(1, retries + 1):
        try:
            return ak.stock_zh_a_spot_em()
        except Exception as e:
            print(f"第 {attempt} 次拉取行情失败: {e}")
            if attempt < retries:
                time.sleep(3)
            else:
                raise


def main():
    # 1. 检查是否休市
    if not is_trading_day():
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        send_notification(
            "14:40 尾盘监测：今日休市",
            f"时间: {now_str}\n状态: 今日为法定节假日或非交易日，监测系统运行正常，祝你假期愉快！",
        )
        return

    # 2. 精准等待至 14:40:00
    wait_until_1440()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 3. 拉取实时数据
    df = fetch_spot_with_retry(retries=3)

    # 4. 大盘情绪风控
    up_count = len(df[df["涨跌幅"] > 0])
    down_limit_count = len(df[(df["涨跌幅"] <= -9.8) & (~df["名称"].str.contains("ST|退", na=False))])
    market_status = f"两市上涨: {up_count} 家 | 跌停(非ST): {down_limit_count} 家"

    if up_count < 1800 or down_limit_count >= 15:
        msg = (
            f"时间: {now_str}\n"
            f"盘面: {market_status}\n"
            f"结论: 今日大盘情绪偏弱（上涨不足1800家或跌停过多），触发风控保护，【无合适标的，建议空仓休息】。"
        )
        send_notification("14:40 尾盘监测：风控空仓", msg)
        return

    # 5. 六维量化筛选
    cond_code = ~df["名称"].str.contains("ST|退", na=False) & ~df["代码"].str.startswith(("8", "4", "92", "688"))
    cond_pct = (df["涨跌幅"] >= 2.5) & (df["涨跌幅"] <= 5.5)
    cond_vol = (df["量比"] >= 1.3) & (df["换手率"] >= 5.0) & (df["换手率"] <= 12.0)
    cond_cap = (df["流通市值"] >= 3e9) & (df["流通市值"] <= 2.5e10)
    cond_intraday = (df["最新价"] >= df["最高"] * 0.985) & (df["最新价"] > df["今开"])

    candidates = df[cond_code & cond_pct & cond_vol & cond_cap & cond_intraday].copy()
    candidates = candidates.sort_values(by="量比", ascending=False).head(12)

    # 6. 验证 MA5 大于 MA20 多头排列
    final_picks = []
    for _, row in candidates.iterrows():
        code = row["代码"]
        try:
            hist = ak.stock_zh_a_hist(symbol=code, period="daily", adjust="qfq").tail(25)
            if len(hist) >= 20:
                ma5 = hist["收盘"].tail(5).mean()
                ma20 = hist["收盘"].tail(20).mean()
                if row["最新价"] > ma5 > ma20:
                    final_picks.append(
                        f"• {row['名称']} ({code})\n"
                        f"  现价: {row['最新价']} | 涨幅: {row['涨跌幅']}% | 量比: {row['量比']} | 换手: {row['换手率']}%"
                    )
        except Exception:
            continue

    # 7. 发送通知（无论有没有选出股票，都会通知你）
    if final_picks:
        picks_text = "\n".join(final_picks[:5])
        msg = (
            f"时间: {now_str}\n"
            f"盘面: {market_status}\n"
            f"精选尾盘候选池（共 {len(final_picks)} 只，展示前 5）：\n"
            f"{picks_text}\n\n"
            f"操作纪律：买入前请看一眼分时图，确认白线全天在黄线（均价线）上方，避开 14:30 后突然直线拉升的诱多票；次日早盘 10:00 前无论盈亏准时卖出。"
        )
        send_notification(f"14:40 尾盘监测：发现 {len(final_picks)} 只标的", msg)
    else:
        msg = (
            f"时间: {now_str}\n"
            f"盘面: {market_status}\n"
            f"结论: 经过涨幅、量比、换手率及均线多头严格过滤，今日【0 只股票符合标准】。\n"
            f"操作纪律：宁可错过，不可做错，今天继续空仓看戏！"
        )
        send_notification("14:40 尾盘监测：今日无合适标的", msg)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"程序运行异常: {exc}", file=sys.stderr)
        try:
            send_notification("14:40 尾盘监测：运行报错", f"行情接口连接超时或异常，错误信息：{exc}")
        except Exception:
            pass
        sys.exit(1)
