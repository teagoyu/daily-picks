# daily-picks

每个交易日自动生成美股/港股/A股选股报告，上传至腾讯 COS，并推送飞书通知。

## 依赖

- Python 3.10+（仅标准库，无需 pip install）
- [Longbridge CLI](https://open.longbridge.com/docs/cli/) — 行情数据来源
- [coscmd](https://cloud.tencent.com/document/product/436/10976) — 报告上传（可选）

```bash
pip install coscmd
longbridge auth login   # 登录一次后 token 自动续期
```

## 快速开始

```bash
cp .env.example .env
# 填入 COS 密钥和飞书 Webhook

python3 daily_picks.py              # 生成 + 上传
python3 daily_picks.py --no-upload  # 仅本地生成
python3 daily_picks.py --dry-run    # 预览评分，不生成 HTML
```

## 定时任务（每个交易日 08:00）

```bash
# crontab -e
0 8 * * 1-5 /Users/yuhao/trade/daily-picks/run_daily_picks.sh
```

## 项目结构

```
daily-picks/
├── daily_picks.py       # 主程序
├── run_daily_picks.sh   # cron 启动脚本
├── .env                 # 密钥配置（不提交）
├── .env.example         # 配置模板
└── output/
    ├── daily_picks_YYYY-MM-DD.html
    └── logs/
        └── daily_picks_YYYY-MM-DD.log
```

## 选股逻辑（满分 100）

| 维度 | 权重 | 说明 |
|-----|------|------|
| 成交量动能 | 25% | 成交量 vs 均量比较 |
| 价格动能 | 20% | 日涨幅 |
| PE 吸引力 | 15% | 合理 PE 区间加分 |
| 均线结构 | 25% | MA5/MA20 多头排列 |
| 价值缺口 | 15% | 距周期高点折价程度 |
