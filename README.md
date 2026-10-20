## 目标

一个可后台常驻的 Python 量化扫描脚本：通过 **Tushare rt_min 分时行情** 计算 **KDJ+MACD B/S 买卖点**，在出现 BUY（抄底）或 SELL（卖出）时向邮箱、Telegram 或企业微信发送通知。

## 策略

- **KDJ**
  - \(RSV=(C-LLV(L,9))/(HHV(H,9)-LLV(L,9))*100\)
  - \(K=SMA(RSV,3,1)\)
  - \(D=SMA(K,3,1)\)
  - \(J=3K-2D\)
- **MACD**
  - \(DIF=EMA(C,12)-EMA(C,26)\)
  - \(DEA=EMA(DIF,9)\)
  - \(MACD=(DIF-DEA)*2\)
- **B（抄底）**
  - \(J<15\) 且 \(MACD<0\) 且 \(DIF<0\) 且 \(DEA<0\) 且 \(MACD>REF(MACD,1)\)
- **S（卖出）**
  - \(J>85\) 且 \(MACD>0\) 且 \(DIF>0\) 且 \(DEA>0\) 且 \(MACD<REF(MACD,1)\)

## 环境安装（Conda，新建 quant 环境）

在 PowerShell 里进入项目目录后执行：

```bash
conda create -n quant python=3.11 -y
conda activate quant
pip install -r requirements.txt
```

## 配置

1. 复制 `.env.example` 为 `.env`。
2. 填写 `TUSHARE_TOKEN`。
3. 按需开启邮件、Telegram、企业微信。
4. 按序号配置票池和企业微信 webhook。

### 票池配置格式

```env
TICKERS_1_FILE=tickers_1.txt
POOL_1_NAME=票池1
TICKERS_2_FILE=tickers_2.txt
POOL_2_NAME=票池2
# TICKERS_3_FILE=tickers_3.txt
# POOL_3_NAME=龙头池
# TICKERS_4_FILE=tickers_4.txt
# POOL_4_NAME=自选池
```

### 企业微信配置格式

```env
ENABLE_WECOM=true
WEWORK_1_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
WEWORK_2_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=yyy
# WEWORK_3_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=zzz
```

规则是一一对应的：

- `TICKERS_1_FILE` → `WEWORK_1_WEBHOOK_URL`
- `TICKERS_2_FILE` → `WEWORK_2_WEBHOOK_URL`
- `TICKERS_3_FILE` → `WEWORK_3_WEBHOOK_URL`
- 以此类推

如果你希望推送标题和日志里显示自定义票池名，可以额外配置：

- `POOL_1_NAME=票池1`
- `POOL_2_NAME=趋势池`
- `POOL_3_NAME=龙头池`

增强版支持非连续序号，例如你可以直接配置：

- `TICKERS_1_FILE`
- `TICKERS_3_FILE`
- `TICKERS_8_FILE`

程序会自动识别已配置的序号，不要求必须连续编号。

你后续如果想新增票池，只需要：

1. 新建一个文件，例如 `tickers_3.txt`
2. 在 `.env` 里增加：

```env
TICKERS_3_FILE=tickers_3.txt
POOL_3_NAME=龙头池
WEWORK_3_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=your_third_key
```

不需要再修改 `main.py`。

### 票池文件内容格式

每个票池文件都支持以下格式：

```text
301123.SH #奕东电子
603112.SH #华翔股份
600519.SH
```

说明：

- `#` 后面的内容会作为备注保留，并用于通知展示。
- 同一文件内同一 `ts_code` 重复出现时，按首次出现的那一行生效。
- 不同票池出现相同股票时，行情缓存仍然共用同一个缓存文件，但推送记录按票池分别处理。

## 运行

### 单次运行

```bash
python main.py
```

程序会按 `.env` 中的 `POLL_INTERVAL_PER_MINUTES` 轮询。无信号则只打印日志；有信号则发送汇总通知。

## 常用配置项

- `POLL_INTERVAL_PER_MINUTES`：每分钟轮询次数
- `MIN_FREQ`：分时频率（1MIN,5MIN,15MIN,30MIN,60MIN）
- `MIN_BARS_REQUIRED`：计算信号所需最少K线
- `SIGNAL_COOLDOWN_MINUTES`：同一标的同方向信号冷却时间（现在按票池分别冷却）
- `HEARTBEAT_INTERVAL_MINUTES`：心跳日志间隔
- `ENABLE_TRADING_HOURS`：是否只在 A 股交易时段抓取
- `ENABLE_EMAIL`：是否发送邮件
- `ENABLE_TELEGRAM`：是否发送 Telegram
- `ENABLE_WECOM`：是否发送企业微信
- `WEWORK_SEND_INTERVAL_SECONDS`：企业微信发送最小间隔

## 说明

- 项目结构：`main.py`（主程序）+ `strategy.py`（策略）+ `email_push.py`（邮件推送）+ `tg_push.py`（Telegram 推送）+ `wecom_push.py`（企业微信推送）。
- 当前行情源为 Tushare `rt_min`，且包含 `http://tushare.xyz` 兼容设置。
- 现在程序支持通过 `.env` 按序号扩展任意多个票池，无需再修改代码。

## 离线测试入口

测试与历史工具已整理，当前命令及安全边界见 [tests/README.md](tests/README.md)，删除/保留清单见 [测试精简整理报告](docs/测试精简整理报告.md)。

```bash
/opt/miniconda3/envs/quant/bin/python -B run_tests.py regression
/opt/miniconda3/envs/quant/bin/python -B run_tests.py known
```

第一条验证日常回归；第二条明确复现9项未修算法缺陷，当前预期退出1，不代表测试通过。不要执行根目录无范围的测试发现，以免导入手动联网/历史诊断工具。
