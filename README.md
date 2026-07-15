# BTC马丁策略交易系统 (btcFuture)

基于OKX期货的马丁格尔策略交易系统，支持模拟交易、实盘交易和Web界面管理。

## 快速开始

### 本地开发

```bash
cd app
pip install -r requirements.txt
python app.py
```

访问 http://localhost:8037

### Docker 部署 (Coolify)

在 Coolify 中部署时配置：
- Build Command: `docker build -t btc-martingale .`
- Run Command: `docker run -d -p 8037:8037 --env-file .env btc-martingale`

或使用 docker-compose：

```bash
docker build -t btc-martingale .
docker run -d -p 8037:8037 --env-file .env btc-martingale
```

## 项目结构

```
btcFuture/
├── app/                    # 应用目录
│   ├── app.py             # FastAPI 主入口
│   ├── main.py            # 命令行入口
│   ├── okx_client.py     # OKX API 客户端
│   ├── risk_manager.py    # 风控模块
│   ├── martingale.py       # 马丁策略核心
│   ├── config.yaml         # 配置文件
│   ├── requirements.txt    # Python 依赖
│   ├── templates/          # Jinja2 模板
│   └── static/             # 静态文件
├── Dockerfile              # Docker 配置
├── requirements.txt        # Python 依赖 (链接到 app/)
├── .env.example           # 环境变量示例
├── .dockerignore          # Docker 忽略文件
├── .gitignore            # Git 忽略文件
├── docker-compose.yml     # Docker Compose 配置
└── README.md
```

## 配置

复制并编辑 `.env.example` 为 `.env`：

```bash
cp .env.example .env
```

或在 Coolify 中配置环境变量。

## 功能

- **首页**: 实时交易状态、持仓详情、浮动盈亏
- **交易记录**: 完整交易历史和统计
- **配置**: 可视化策略参数调整

## API 接口

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/status` | GET | 获取交易状态 |
| `/api/trades` | GET | 获取交易记录 |
| `/api/config` | GET/POST | 获取/更新配置 |
| `/api/action/open` | POST | 开仓 |
| `/api/action/close` | POST | 平仓 |
| `/api/action/add` | POST | 加仓 |
| `/api/action/reset` | POST | 重置余额 |
| `/api/simulate` | POST | 模拟价格变动 |

## 策略参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| 杠杆 | 2x | 风险与效率平衡 |
| 首单 | 10 USDT | 总资金0.5% |
| 加仓倍数 | 1.5 | 控制风险 |
| 价格间隔 | 0.8% | 补仓触发 |
| 最大加仓 | 7次 | 保留缓冲资金 |
| 止盈目标 | 2% | 覆盖手续费 |
| 硬止损 | 25% | 强制平仓线 |

## 风险提示

本程序仅供学习研究使用，实盘交易风险由用户自行承担。
