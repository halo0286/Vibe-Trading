#!/usr/bin/env python3
"""验证脚本：用 TUSHARE_TOKEN + Ollama 拉取汇川技术(300124.SZ)数据并跑分析。

用法：
    export TUSHARE_TOKEN="5a01d6d91ae9efc0de0358e385c633c66fe967320d1be511bb27324a"
    python verify_huichuan.py

验证项：
1. tushare 能否连通并拉到汇川技术日线数据
2. Vibe-Trading 的数据加载器能否处理该数据
3. Ollama 本地模型是否可用（可选）
4. logsystem 集成是否正常记录日志
"""

import os
import sys
import time

# 确保 agent/src 在 path 里
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "agent"))

TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "")
STOCK_CODE = "300124.SZ"  # 汇川技术
STOCK_NAME = "汇川技术"


def check_tushare():
    """检查 tushare 连通性与数据拉取。"""
    print(f"\n{'='*60}")
    print(f"[1] 检查 TUSHARE_TOKEN 与 {STOCK_NAME}({STOCK_CODE}) 数据")
    print(f"{'='*60}")

    if not TUSHARE_TOKEN:
        print("❌ TUSHARE_TOKEN 未设置，请 export TUSHARE_TOKEN=...")
        return False

    try:
        import tushare as ts
        print(f"✅ tushare 版本: {ts.__version__}")
    except ImportError:
        print("❌ tushare 未安装")
        return False

    # 直接传 token，避免 set_token() 写 ~/tk.csv（沙箱 HOME 不可写）
    pro = ts.pro_api(token=TUSHARE_TOKEN)

    # 拉最近 30 天日线
    from datetime import datetime, timedelta
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=45)).strftime("%Y%m%d")

    try:
        df = pro.daily(ts_code=STOCK_CODE, start_date=start_date, end_date=end_date)
        if df is not None and len(df) > 0:
            print(f"✅ 成功拉取 {len(df)} 条日线数据")
            print(f"   时间范围: {df['trade_date'].min()} ~ {df['trade_date'].max()}")
            print(f"   最新收盘: {df.iloc[0]['close']} (日期: {df.iloc[0]['trade_date']})")
            print(f"   列: {list(df.columns)}")
            return True
        else:
            print(f"⚠️  拉取到空数据（可能非交易日或 token 权限不足）")
            return False
    except Exception as e:
        print(f"❌ 拉取失败: {type(e).__name__}: {e}")
        return False


def check_vibe_data_loader():
    """检查 Vibe-Trading 数据加载器能否处理 A股数据。"""
    print(f"\n{'='*60}")
    print(f"[2] 检查 Vibe-Trading 数据加载器")
    print(f"{'='*60}")

    try:
        from src.data import loaders
        print(f"✅ src.data.loaders 可导入")
        # 列出可用的 loader
        loader_names = [x for x in dir(loaders) if not x.startswith("_")]
        print(f"   可用模块: {loader_names[:10]}")
        return True
    except ImportError as e:
        print(f"⚠️  src.data.loaders 导入失败: {e}")
        print(f"   （可能需要更多依赖，不影响 tushare 直连验证）")
        return False


def check_ollama():
    """检查 Ollama 本地模型可用性。"""
    print(f"\n{'='*60}")
    print(f"[3] 检查 Ollama 本地模型")
    print(f"{'='*60}")

    import urllib.request
    import json

    ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    try:
        req = urllib.request.Request(f"{ollama_url}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            models = [m["name"] for m in data.get("models", [])]
            if models:
                print(f"✅ Ollama 可用，已加载模型: {', '.join(models[:5])}")
                return True
            else:
                print(f"⚠️  Ollama 运行中但无模型，请先 ollama pull <model>")
                return False
    except Exception as e:
        print(f"⚠️  Ollama 不可达 ({ollama_url}): {type(e).__name__}: {e}")
        print(f"   （Ollama 为可选，不影响数据拉取验证）")
        return False


def check_logsystem():
    """检查 task2 logsystem 集成是否正常。"""
    print(f"\n{'='*60}")
    print(f"[4] 检查 logsystem 集成")
    print(f"{'='*60}")

    try:
        from src.logsystem import configure, LogConfig, get_logger, trace_scope, log_call
        print(f"✅ src.logsystem 可导入")

        import tempfile
        d = tempfile.mkdtemp()
        configure(LogConfig(log_dir=d, level="INFO", file_format="kv", enable_async_analysis=False))
        lg = get_logger("verify")

        @log_call(step="verify_step")
        def test_func(x):
            return x * 2

        with trace_scope(business_id="verify-huichuan"):
            result = test_func(21)
            lg.info("verify result: %s", result)

        import os as _os
        logs = [f for f in _os.listdir(d) if f.endswith(".log")]
        if logs:
            content = open(_os.path.join(d, logs[0]), encoding="utf-8").read()
            has_bid = "verify-huichuan" in content
            has_step = "verify_step" in content
            has_cost = "cost_ms" in content
            print(f"✅ 日志文件生成: {logs[0]}")
            print(f"   business_id: {'✅' if has_bid else '❌'}")
            print(f"   step: {'✅' if has_step else '❌'}")
            print(f"   cost_ms: {'✅' if has_cost else '❌'}")
            return has_bid and has_step and has_cost
        else:
            print(f"❌ 未生成日志文件")
            return False
    except Exception as e:
        print(f"❌ logsystem 验证失败: {type(e).__name__}: {e}")
        return False


def main():
    print("=" * 60)
    print(f"Vibe-Trading 验证脚本 — {STOCK_NAME}({STOCK_CODE})")
    print(f"TUSHARE_TOKEN: {'已设置' if TUSHARE_TOKEN else '未设置'}")
    print("=" * 60)

    results = {}
    results["tushare"] = check_tushare()
    results["data_loader"] = check_vibe_data_loader()
    results["ollama"] = check_ollama()
    results["logsystem"] = check_logsystem()

    print(f"\n{'='*60}")
    print("汇总")
    print(f"{'='*60}")
    for k, v in results.items():
        status = "✅ PASS" if v else ("⚠️ SKIP/WARN" if k in ("data_loader", "ollama") else "❌ FAIL")
        print(f"  {k:<15} {status}")

    all_critical = results["tushare"] and results["logsystem"]
    print(f"\n{'✅ 核心验证通过' if all_critical else '❌ 有核心项未通过'}")
    return 0 if all_critical else 1


if __name__ == "__main__":
    raise SystemExit(main())
