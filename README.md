# water-plane 水上飞机选股 skill

抓"竞价平线蓄势、开盘起飞"的股票（A股集合竞价形态选股）。

## 核心口径

| 条件 | 口径 | 范围 |
|------|------|------|
| 平线波动 | 9:20–9:23 竞价价格 (最高-最低)/均价 | ≤ 0.7% |
| 竞价涨幅 | 9:20–9:23 竞价均价相对昨收 | -3% ~ +3% |
| 开盘涨幅 | 9:25 开盘价相对昨收 | -1% ~ +1% |
| 竞价成交额 | 两头 | ≥1000万 或 ≤30万 |

放量组/缩量组各按评分留 top100，合并最多 200 只。

## 使用

```bash
python3 scripts/water_plane.py --mode am   # 早盘
python3 scripts/water_plane.py --mode pm   # 收盘验证
```

## 配置

复制 `scripts/ths_config.example.json` 为 `scripts/ths_config.json`，填入同花顺账号密码。

依赖：`thsdk`、同花顺账号（thsdk 需登录态）。
