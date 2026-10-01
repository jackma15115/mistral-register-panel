<div align="center">

# Mistral Register + Live Panel

批量注册 **Mistral AI** 账号并自动创建与提取 API 密钥（Camoufox�? Web 监控面板
全自动邮箱验�?/ API Key 自动创建与提�?/ 格式化导�?/ 任务编排 / 代理�?/ 邮箱服务

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)

</div>

---

> **声明�?* 仅供自动化流程研究、自有环境联调与个人学习。请遵守 Mistral AI、邮箱及代理服务商条款与当地法律，勿用于未授权批量滥用�?
---

## 功能特�?
| 能力 | 说明 |
| :--- | :--- |
| **全自动注册与�?Key** | 自动填写邮箱、密码，自动轮询邮箱获取 6 �?OTP 验证码，验证登录后自动跳转进入后台并创建提取 `mstrl_...` 格式�?API 密钥 |
| **双标准格式导�?* | 1. **账号 CSV**（`account.csv`）：表头格式 `email,passwd,api_key`<br>2. **密钥文件**（`key.txt`）：一行一�?API Key |
| **丰富邮箱服务适配** | 原生深度支持 **TiMail**（如 `https://anymail.77669876.xyz`，支持子域名模式�?`keldie.cyou` 等域名池）、DuckMail、Inbucket、Outlook RT、CloudMail �?|
| **底层反指纹浏览器** | 基于 [Camoufox](https://camoufox.com/)（Gecko 真实内核与指纹防护），告别通用自动化检测与盾拦�?|
| **代理会话粘�?* | 单账号全流程（注册、收信、登录验证、后台建 Key）出�?IP 保持不变，防止中途跨地域跳变风控 |
| **外部代理池管�?* | 面板支持单条/批量导入 HTTP/SOCKS 代理，自带并发探活、自动冷却隔离与脱敏显示 |
| **Live 监控面板** | 实时状态看板、单�?多轮编排模式、动态并发调节、邮箱与代理池实时配置、一键下�?`account.csv` �?`key.txt` |
| **异常自愈机制** | 批处理由独立 supervisor 守护，遇到卡顿、超时或驱动异常自动按剩余未完成槽位断点续跑，不丢失已成功账�?|

---

## 导出格式说明

每个账号注册成功并创�?API 密钥后，系统会自动保存为以下格式�?
1. **`account.csv`**（同时保存到项目根目录及 `accounts/account.csv`）：
   ```csv
   email,passwd,api_key
   user1@example.com,Password#123!,mstrl_UnVC7s4TRas2B0AtzmfZaictDsi1pOCw_2XPG2I
   user2@example.com,Password#456!,mstrl_8f9a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a
   ```
2. **`key.txt`**（同时保存到项目根目录及 `accounts/key.txt`）：
   ```text
   mstrl_UnVC7s4TRas2B0AtzmfZaictDsi1pOCw_2XPG2I
   mstrl_8f9a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a
   ```
3. **单账号凭�?*（保存到 `accounts/{email}.txt`）：
   ```text
   email----password----api_key
   ```

---

## 快速上�?
### 1. 环境准备

- Python 3.10+
- 可选：网络代理（支�?HTTP / SOCKS5�?
```bash
# 克隆仓库
git clone https://github.com/ChrisWilson/mistral-register-panel.git
cd mistral-register-panel

# 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate          # Linux / macOS
# Windows: .venv\Scripts\Activate.ps1

# 安装依赖
pip install -r requirements.txt

# 必须：下�?Camoufox 浏览器二进制文件
python -m camoufox fetch

# 复制默认配置文件
cp config.example.json config.json
```

> **注意�?* �?Linux 无桌面环境下，请确保已安�?`xvfb` �?`xauth`（例�?`sudo apt-get install -y xvfb xauth`）�?
---

### 2. 配置文件说明 (`config.json`)

系统预设已调优，核心关键配置如下�?
```json
{
  "email_provider": "ti-temp-mail",
  "ti_temp_mail_base_url": "https://anymail.77669876.xyz",
  "ti_temp_mail_api_key": "",
  "ti_temp_mail_domain": "keldie.cyou",
  "ti_temp_mail_mode": "subdomain",
  "proxy": "",
  "register_count": 1,
  "register_workers": 1
}
```

- `email_provider`：指定临时邮箱服务，推荐 `ti-temp-mail`�?- `ti_temp_mail_base_url`：TiMail 接口地址（如 `https://anymail.77669876.xyz`）�?- `ti_temp_mail_domain`：邮箱域名（�?`keldie.cyou`）�?- `ti_temp_mail_mode`：`subdomain`（子域名泛域模式）或 `maindomain`（主域名模式）�?- `proxy`：固定代理地址（如 `http://127.0.0.1:7890` �?`socks5://127.0.0.1:1080`），若配置面板代理池则优先从代理池选择�?
---

### 3. 运行方式

#### 方式 A：无头批量命令行运行（推荐）

直接在命令行运行批量注册任务（参�?1 为目标成功账号数，参�?2 为并发浏览器数）�?
```bash
# 注册 10 个账号，并发 2
# Linux (无显示服务器自动使用 xvfb)�?xvfb-run -a python -u run_batch_headless.py 10 2

# Windows / macOS�?python -u run_batch_headless.py 10 2
```

#### 方式 B：Web 监控面板模式

启动内置 Web 监控面板进行图形化控制、代理池维护及一键导出：

```bash
export MONITOR_TOKEN='你的面板密码'   # 必设随机令牌
export MONITOR_HOST=127.0.0.1
export MONITOR_PORT=8787

python webui/monitor.py
```

在浏览器打开 `http://127.0.0.1:8787/`�?1. 在“面�?Token”输入框中输入上方配置的 `MONITOR_TOKEN`�?2. 可在线调整并发数、批次目标数并点击“启动”�?3. 可以在“代理池”页面导入外部代理，在“邮箱服务”页面切换测试邮箱渠道�?4. 注册完成后直接点击“导�?key.txt”或“导出账�?CSV”下载结果�?
#### 方式 C：桌�?GUI 界面

```bash
python mistral_register_ttk.py
```

---

## 项目结构

```text
.
├── mistral_register_ttk.py    # GUI / 运行主程序入�?├── grok_register_ttk.py       # 兼容执行核心
├── register_flow.py           # Mistral AI 页面交互、OTP 校验�?API Key 提取
├── browser_session.py         # Camoufox 浏览器生命周期与网络会话管理
├── batch_supervisor.py        # 批处理容错监督与断点续跑机制
├── run_batch_headless.py      # 无头批量运行主入�?├── webui/
�?  ├── monitor.py             # 监控面板 HTTP 服务与嵌入式 UI
�?  ├── proxy_store.py         # 代理池导入、探活与健康监控
�?  ├── email_provider_store.py# 邮箱配置与凭据安全存�?�?  └── account_exports.py     # 格式化导出逻辑
├── email_providers/           # 邮箱适配层（TiMail, DuckMail, Inbucket 等）
├── tests/                     # 自动化测试套�?├── config.example.json        # 配置文件示例
└── README.md
```

---

## 验证与测�?
修改代码或部署前，可运行测试套件保证系统稳定可靠�?
```bash
# Windows
powershell -File scripts/run_tests_windows.ps1

# Linux / macOS
scripts/run_tests.sh
```

---

## 协议

本项目基�?MIT License 开源�?
