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
from database import Database, get_db


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

# 创建 Jinja2 环境，禁用缓存以避免 unhashable type 错误
import jinja2
env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(BASE_DIR / "templates")),
    auto_reload=True,
    cache_size=0  # 禁用缓存
)
templates = Jinja2Templates(env=env)


# ============ 配置加载/保存 ============
def load_config() -> Dict[str, Any]:
    """从YAML文件加载基础配置"""
    config_path = BASE_DIR / "config.yaml"
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
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
            'AUTO_LOOP': False,
            'DIRECTION': 'long',
            'API_KEY': '',
            'SECRET_KEY': '',
            'PASSPHRASE': ''
        }


def load_persistent_config() -> Dict[str, Any]:
    """从数据库加载持久化配置"""
    db = get_db()
    return db.get_all_config()


def save_config_to_db(config: Dict[str, Any]):
    """保存配置到数据库"""
    db = get_db()
    db.set_all_config(config)


def save_config(config: Dict[str, Any]):
    """保存配置到YAML文件"""
    config_path = BASE_DIR / "config.yaml"
    with open(config_path, 'w', encoding='utf-8') as f:
        yaml.dump(config, f, allow_unicode=True)


def get_merged_config() -> Dict[str, Any]:
    """获取合并后的配置（YAML + 数据库，数据库优先）"""
    yaml_config = load_config()
    db_config = load_persistent_config()
    # 合并配置，数据库中的值覆盖YAML中的值
    merged = {**yaml_config, **db_config}
    return merged


def initialize_trading():
    """初始化交易组件"""
    # 加载合并配置
    config = get_merged_config()

    # 如果数据库没有配置，从YAML加载默认值
    db_config = load_persistent_config()
    if not db_config:
        # 首次使用，加载YAML默认值并保存到数据库
        yaml_defaults = load_config()
        if yaml_defaults:
            save_config_to_db(yaml_defaults)
            config = yaml_defaults

    _state.config = config

    # 获取数据库中的余额（如果存在）
    db = get_db()
    saved_balance = db.get_balance()
    initial_balance = config.get('TOTAL_CAPITAL', 2000)

    # 创建客户端
    _state.client = OKXClient(
        api_key=config.get('API_KEY', ''),
        secret_key=config.get('SECRET_KEY', ''),
        passphrase=config.get('PASSPHRASE', ''),
        simulation=config.get('SIMULATION', True)
    )

    # 恢复余额
    if saved_balance is not None:
        _state.client.sim_balance = saved_balance
    elif config.get('SIMULATION', True):
        _state.client.reset_sim_balance(initial_balance)

    # 创建风控
    risk_config = RiskConfig(
        max_loss_rate=config.get('MAX_LOSS_RATE', 0.25),
        max_drawdown=0.30,
        trend_pause_rate=config.get('TREND_PAUSE_RATE', 0.05),
        min_balance=initial_balance * 0.2
    )
    _state.risk_manager = RiskManager(
        config=risk_config,
        initial_balance=initial_balance
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
        auto_loop=config.get('AUTO_LOOP', False),
        direction=config.get('DIRECTION', 'long')
    )
    _state.strategy = MartingaleStrategy(
        client=_state.client,
        risk_manager=_state.risk_manager,
        config=strategy_config
    )
    _state.strategy.initialize()

    # 恢复策略状态
    saved_position = db.get_state('current_position')
    if saved_position and not _state.strategy.position.is_empty():
        # 恢复持仓信息
        _state.strategy.position.side = saved_position.get('side', 'none')
        _state.strategy.position.total_size = saved_position.get('total_size', 0)
        _state.strategy.position.avg_price = saved_position.get('avg_price', 0)

    # 创建模拟市场
    _state.market = SimulatedMarket(initial_price=65000.0, volatility=0.001)


def save_state():
    """保存当前状态到数据库"""
    if not _state.client:
        return

    db = get_db()

    # 保存余额
    db.set_balance(_state.client.get_sim_balance())

    # 保存持仓状态
    if _state.strategy and not _state.strategy.position.is_empty():
        pos = _state.strategy.position
        db.set_state('current_position', {
            'side': pos.side.value,
            'total_size': pos.total_size,
            'avg_price': pos.avg_price,
            'dca_count': pos.dca_count,
            'first_entry_price': pos.first_entry_price
        })


# ============ 路由定义 ============

@app.on_event("startup")
async def startup_event():
    """应用启动"""
    initialize_trading()


@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭时保存状态"""
    save_state()


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
    direction = _state.config.get('DIRECTION', 'long')
    if not pos.is_empty():
        if direction == "long":
            unrealized_pnl = (current_price - pos.avg_price) * pos.total_size
            pnl_rate = (current_price - pos.avg_price) / pos.avg_price * 100
        else:
            unrealized_pnl = (pos.avg_price - current_price) * pos.total_size
            pnl_rate = (pos.avg_price - current_price) / pos.avg_price * 100

    return {
        "success": True,
        "timestamp": datetime.now().isoformat(),
        "price": current_price,
        "balance": balance,
        "direction": direction,
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
    db = get_db()

    # 尝试从数据库获取
    trades = db.get_trades()
    summary = db.get_trades_summary()

    # 如果数据库为空，尝试从内存获取
    if not trades and _state.strategy:
        trades = _state.strategy.recorder.trades
        summary = _state.strategy.recorder.get_summary()

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
async def update_config(request: Request):
    """更新配置"""
    body = await request.json()

    config = _state.config.copy()
    config.update({
        'API_KEY': body.get('api_key', ''),
        'SECRET_KEY': body.get('secret_key', ''),
        'PASSPHRASE': body.get('passphrase', ''),
        'SIMULATION': body.get('simulation', True),
        'LEVERAGE': body.get('leverage', 2),
        'FIRST_ORDER_SIZE': body.get('first_order_size', 10),
        'MULTIPLIER': body.get('multiplier', 1.5),
        'PRICE_INTERVAL': body.get('price_interval', 0.008),
        'MAX_DCA_COUNT': body.get('max_dca_count', 7),
        'TAKE_PROFIT': body.get('take_profit', 0.02),
        'MAX_LOSS_RATE': body.get('max_loss_rate', 0.25),
        'AUTO_LOOP': body.get('auto_loop', False),
        'DIRECTION': body.get('direction', 'long')
    })

    # 保存到数据库（持久化）
    save_config_to_db(config)
    _state.config = config

    # 同时保存到YAML（备份）
    save_config(config)

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

    # 保存状态
    save_state()

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

    # 保存状态
    save_state()

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

    # 保存状态
    save_state()

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

    # 保存重置后的余额
    save_state()

    # 清空交易记录
    db = get_db()
    db.clear_trades()
    db.set_state('current_position', None)

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

    # 保存状态
    save_state()

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
