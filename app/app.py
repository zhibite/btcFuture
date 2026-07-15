"""
OKX 期货马丁格尔策略 - FastAPI Web服务
提供Web界面查看交易状态和管理策略
"""

import os
import sys
import time
import json
import yaml
import asyncio
from datetime import datetime
from typing import Optional, Dict, Any
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

# 导入交易模块
from okx_client import OKXClient
from risk_manager import RiskManager, RiskConfig
from martingale import MartingaleStrategy, MartingaleConfig, SimulatedMarket


# ============ 全局状态 ============
class TradeState:
    """交易状态管理器"""
    def __init__(self):
        self.client: Optional[OKXClient] = None
        self.risk_manager: Optional[RiskManager] = None
        self.strategy: Optional[MartingaleStrategy] = None
        self.market: Optional[SimulatedMarket] = None
        self.running = False
        self.config: Dict[str, Any] = {}
        self.trades: list = []
        self.last_update = time.time()

_state = TradeState()


# ============ FastAPI 应用 ============
app = FastAPI(
    title="BTC马丁策略交易系统",
    description="OKX期货马丁格尔策略Web控制台",
    version="1.0.0"
)

# 静态文件和模板
BASE_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ============ 配置加载 ============
def load_config() -> Dict[str, Any]:
    """加载配置文件"""
    config_path = BASE_DIR / "config.yaml"
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        return {
            'SIMULATION': True,
            'SYMBOL': 'BTC-USDT-SWAP',
            'LEVERAGE': 2,
            'FIRST_ORDER_SIZE': 10,
            'MULTIPLIER': 1.5,
            'PRICE_INTERVAL': 0.008,
            'MAX_DCA_COUNT': 7,
            'TAKE_PROFIT': 0.02,
            'MAX_LOSS_RATE': 0.25,
            'TREND_PAUSE_RATE': 0.05,
            'TOTAL_CAPITAL': 2000,
            'AUTO_LOOP': False
        }


def save_config(config: Dict[str, Any]):
    """保存配置"""
    config_path = BASE_DIR / "config.yaml"
    with open(config_path, 'w', encoding='utf-8') as f:
        yaml.dump(config, f, allow_unicode=True)


def initialize_trading():
    """初始化交易组件"""
    config = load_config()
    _state.config = config

    # 创建客户端
    _state.client = OKXClient(
        api_key=config.get('API_KEY', ''),
        secret_key=config.get('SECRET_KEY', ''),
        passphrase=config.get('PASSPHRASE', ''),
        simulation=config.get('SIMULATION', True)
    )

    if config.get('SIMULATION', True):
        _state.client.reset_sim_balance(config.get('TOTAL_CAPITAL', 2000))

    # 创建风控
    risk_config = RiskConfig(
        max_loss_rate=config.get('MAX_LOSS_RATE', 0.25),
        max_drawdown=0.30,
        trend_pause_rate=config.get('TREND_PAUSE_RATE', 0.05),
        min_balance=config.get('TOTAL_CAPITAL', 2000) * 0.2
    )
    _state.risk_manager = RiskManager(
        config=risk_config,
        initial_balance=config.get('TOTAL_CAPITAL', 2000)
    )

    # 创建策略
    strategy_config = MartingaleConfig(
        leverage=config.get('LEVERAGE', 2),
        first_order_size=config.get('FIRST_ORDER_SIZE', 10),
        multiplier=config.get('MULTIPLIER', 1.5),
        price_interval=config.get('PRICE_INTERVAL', 0.008),
        max_dca_count=config.get('MAX_DCA_COUNT', 7),
        take_profit=config.get('TAKE_PROFIT', 0.02),
        symbol=config.get('SYMBOL', 'BTC-USDT-SWAP'),
        auto_loop=config.get('AUTO_LOOP', False)
    )
    _state.strategy = MartingaleStrategy(
        client=_state.client,
        risk_manager=_state.risk_manager,
        config=strategy_config
    )
    _state.strategy.initialize()

    # 创建模拟市场
    _state.market = SimulatedMarket(initial_price=65000.0, volatility=0.001)


# ============ 路由定义 ============

@app.on_event("startup")
async def startup_event():
    """应用启动"""
    initialize_trading()


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """首页 - 交易状态"""
    return templates.TemplateResponse("index.html", {
        "request": request,
        "title": "交易状态"
    })


@app.get("/trades", response_class=HTMLResponse)
async def trades_page(request: Request):
    """交易记录页面"""
    return templates.TemplateResponse("trades.html", {
        "request": request,
        "title": "交易记录"
    })


@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    """配置页面"""
    return templates.TemplateResponse("config.html", {
        "request": request,
        "title": "策略配置"
    })


# ============ API 接口 ============

@app.get("/api/status")
async def get_status():
    """获取交易状态"""
    if not _state.strategy:
        initialize_trading()

    pos = _state.strategy.position
    balance = _state.client.get_sim_balance()
    current_price = _state.market.get_price() if _state.market else _state.client.get_current_price(_state.config.get('SYMBOL', 'BTC-USDT-SWAP'))
    risk_status = _state.risk_manager.check_loss_risk(balance)

    # 计算浮动盈亏
    unrealized_pnl = 0
    pnl_rate = 0
    if not pos.is_empty():
        unrealized_pnl = (current_price - pos.avg_price) * pos.total_size
        pnl_rate = (current_price - pos.avg_price) / pos.avg_price * 100

    return {
        "success": True,
        "timestamp": datetime.now().isoformat(),
        "price": current_price,
        "balance": balance,
        "position": {
            "side": pos.side.value,
            "total_size": pos.total_size,
            "avg_price": pos.avg_price,
            "dca_count": pos.dca_count,
            "first_entry_price": pos.first_entry_price,
            "first_entry_time": pos.first_entry_time
        } if not pos.is_empty() else None,
        "unrealized_pnl": unrealized_pnl,
        "pnl_rate": pnl_rate,
        "risk": {
            "level": risk_status.level.value,
            "loss_rate": risk_status.current_loss_rate * 100,
            "message": risk_status.message,
            "emergency_exit": risk_status.emergency_exit
        },
        "strategy": {
            "symbol": _state.strategy.config.symbol,
            "leverage": _state.strategy.config.leverage,
            "max_dca": _state.strategy.config.max_dca_count,
            "next_dca_price": _state.strategy.get_next_dca_price(),
            "target_profit_price": _state.strategy.get_target_profit_price(),
            "breakeven_price": _state.strategy.get_breakeven_price()
        },
        "stats": _state.strategy.stats
    }


@app.get("/api/trades")
async def get_trades():
    """获取交易记录"""
    trades = _state.strategy.recorder.trades if _state.strategy else []
    summary = _state.strategy.recorder.get_summary() if _state.strategy else {}

    return {
        "success": True,
        "trades": trades,
        "summary": summary
    }


@app.get("/api/config")
async def get_config():
    """获取当前配置"""
    return {
        "success": True,
        "config": _state.config
    }


@app.post("/api/config")
async def update_config(
    leverage: int = Form(...),
    first_order_size: float = Form(...),
    multiplier: float = Form(...),
    price_interval: float = Form(...),
    max_dca_count: int = Form(...),
    take_profit: float = Form(...),
    max_loss_rate: float = Form(...),
    auto_loop: bool = Form(False)
):
    """更新配置"""
    config = _state.config.copy()
    config.update({
        'LEVERAGE': leverage,
        'FIRST_ORDER_SIZE': first_order_size,
        'MULTIPLIER': multiplier,
        'PRICE_INTERVAL': price_interval,
        'MAX_DCA_COUNT': max_dca_count,
        'TAKE_PROFIT': take_profit,
        'MAX_LOSS_RATE': max_loss_rate,
        'AUTO_LOOP': auto_loop
    })

    save_config(config)
    _state.config = config

    # 重新初始化策略
    initialize_trading()

    return {"success": True, "message": "配置已更新"}


@app.post("/api/action/open")
async def action_open():
    """开仓"""
    if not _state.strategy:
        return {"success": False, "message": "策略未初始化"}

    if not _state.strategy.position.is_empty():
        return {"success": False, "message": "已有持仓"}

    _state.strategy.last_price = _state.market.get_price()
    success = _state.strategy.check_and_open()

    return {
        "success": success,
        "message": "开仓成功" if success else "开仓失败"
    }


@app.post("/api/action/close")
async def action_close():
    """平仓"""
    if not _state.strategy:
        return {"success": False, "message": "策略未初始化"}

    if _state.strategy.position.is_empty():
        return {"success": False, "message": "无持仓"}

    _state.strategy.last_price = _state.market.get_price()
    success = _state.strategy._close_position()

    return {
        "success": success,
        "message": "平仓成功" if success else "平仓失败"
    }


@app.post("/api/action/add")
async def action_add():
    """加仓"""
    if not _state.strategy:
        return {"success": False, "message": "策略未初始化"}

    if _state.strategy.position.is_empty():
        return {"success": False, "message": "无持仓"}

    _state.strategy.last_price = _state.market.get_price()
    success = _state.strategy.check_and_add_position()

    return {
        "success": success,
        "message": "加仓成功" if success else "加仓条件未满足"
    }


@app.post("/api/action/reset")
async def action_reset():
    """重置余额"""
    if not _state.client:
        return {"success": False, "message": "客户端未初始化"}

    initial = _state.config.get('TOTAL_CAPITAL', 2000)
    _state.client.reset_sim_balance(initial)
    _state.risk_manager.reset(initial)
    _state.strategy.stats = {
        'total_cycles': 0,
        'profit_cycles': 0,
        'loss_cycles': 0,
        'total_profit': 0.0,
        'total_loss': 0.0,
        'start_time': time.time()
    }

    return {"success": True, "message": f"余额已重置为 {initial} USDT"}


@app.post("/api/simulate")
async def simulate_price(action: str = Form(...)):
    """模拟价格变动"""
    if not _state.market:
        return {"success": False, "message": "市场未初始化"}

    if action == "drop_small":
        _state.market.simulate_drop(0.005)
    elif action == "drop_medium":
        _state.market.simulate_drop(0.01)
    elif action == "drop_large":
        _state.market.simulate_drop(0.02)
    elif action == "rally_small":
        _state.market.simulate_rally(0.005)
    elif action == "rally_medium":
        _state.market.simulate_rally(0.01)
    elif action == "rally_large":
        _state.market.simulate_rally(0.02)
    else:
        _state.market.tick()

    new_price = _state.market.get_price()
    _state.strategy.last_price = new_price

    # 运行策略逻辑
    _state.strategy.run_one_cycle()

    return {
        "success": True,
        "price": new_price
    }


# ============ WebSocket 实时更新 ============

class ConnectionManager:
    """WebSocket连接管理器"""
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except:
                pass


manager = ConnectionManager()


@app.websocket("/ws/status")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket实时推送"""
    await manager.connect(websocket)
    try:
        while True:
            # 获取最新状态
            status = await get_status()
            await websocket.send_json(status)
            await asyncio.sleep(2)  # 每2秒推送一次
    except WebSocketDisconnect:
        manager.disconnect(websocket)


# ============ 启动入口 ============

def run_server(host: str = None, port: int = None):
    """启动服务器"""
    if host is None:
        host = os.getenv("HOST", "0.0.0.0")
    if port is None:
        port = int(os.getenv("PORT", "8037"))
    print(f"启动服务器: http://{host}:{port}")
    print("按 Ctrl+C 停止")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8037")))
    args = parser.parse_args()
    run_server(args.host, args.port)
