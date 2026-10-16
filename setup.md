# Windows 环境从零安装与运行

## 1) 准备软件

- 安装 [Miniconda](https://docs.conda.io/en/latest/miniconda.html) 或 Anaconda
- 安装 Git（可选，如果你要从仓库拉代码）

安装完成后，打开 **PowerShell**。

## 2) 获取项目代码

如果你已经有项目目录，跳过这一步。  
如果要从 Git 拉取：

```powershell
git clone <your-repo-url> QuantTrading
cd QuantTrading
```

## 3) 创建并安装 Python 环境

在项目根目录执行：

```powershell
conda create -n quant python=3.11 -y
conda activate quant
pip install -r requirements.txt
```

## 4) 配置参数

复制配置模板并编辑：

```powershell
copy .env.example .env
notepad .env
```

至少要配置：

- `TUSHARE_TOKEN`
- `MIN_FREQ`（如 `1MIN`）
- `POLL_INTERVAL_PER_MINUTES`（如 `1`）
- `MIN_BARS_REQUIRED`（调试可先 `1~5`，正式建议更高）
- `ENABLE_EMAIL`（先调试可设 `false`）
- 邮件参数：`SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS/MAIL_TO`

编辑 `tickers.txt`，每行一个 `ts_code`（例如 `600000.SH`）。

## 5) 先测试邮件是否可用（可选）

```powershell
conda run -n quant python test_email_send.py
```

看到 `OK: test email sent.` 代表邮件配置可用。

## 6) 启动 / 查看状态 / 停止

打开PowerShell/控制台，进入项目根目录：

启动脚本：

```bash
.\run_windows\windows_start.bat
```

运行中查看状态（最近20条日志）：

```
.\run_windows\windows_status.bat
```

停止运行：

```powershell
.\run_windows\windows_stop.bat
```

日志文件：

- 当日日志：`logs/YYYY-MM-DD_quant_alert.log`
- 当日错误日志：`logs/YYYY-MM-DD_quant_alert.err.log`
- 当前日志路径记录：`runtime/current_logs.env`

---

# Linux 环境从零安装与运行

## 1) 准备软件

- 安装 Miniconda（推荐）
- 安装 Git（可选）

## 2) 获取项目代码

```bash
git clone <your-repo-url> QuantTrading
cd QuantTrading
```

## 3) 创建并安装 Python 环境

```bash
conda create -n quant python=3.11 -y
conda activate quant
pip install -r requirements.txt
```

## 4) 配置参数

```bash
cp .env.example .env
vi .env
```

至少要配置：

- `TUSHARE_TOKEN`
- `MIN_FREQ`（如 `1MIN`）
- `POLL_INTERVAL_PER_MINUTES`（如 `1`）
- `MIN_BARS_REQUIRED`
- `ENABLE_EMAIL`
- 邮件参数：`SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS/MAIL_TO`

并编辑 `tickers.txt`，每行一个 `ts_code`。

## 5) 测试邮件（可选）

```bash
conda run -n quant python test_email_send.py
```

## 6) 授权并运行脚本

首次赋执行权限：

```bash
chmod +x run_linux/linux_start.sh run_linux/linux_status.sh run_linux/linux_stop.sh
```

启动 / 状态 / 停止：

```bash
./run_linux/linux_start.sh
./run_linux/linux_status.sh
./run_linux/linux_stop.sh
```

日志文件：

- 当日日志：`logs/YYYY-MM-DD_quant_alert.log`
- 当日错误日志：`logs/YYYY-MM-DD_quant_alert.err.log`
- 当前日志路径记录：`runtime/current_logs.env`

## 7) 常见问题

- `权限不足`：检查 Tushare 接口权限是否开通（至少要能访问 `rt_min`）
- `连接超时`：网络波动，建议重试或稍后再试
- `K线不足`：降低 `MIN_BARS_REQUIRED` 做联调，正式再调高
