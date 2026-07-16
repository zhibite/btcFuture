"""
OKX 期货马丁格尔策略 - FastAPI Web服务
提供Web界面查看交易状态和管理策略
"""

import os
import sys
import time
import yaml
import asyncio
from datetime import datetime
from typing import Optional, Dict, Any
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Form
from fastapi.responses import HTMLResponse
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
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ============ 自定义 template renderer ============
def render_template(template_name: str, request: Request = None, **context):
    """直接渲染模板返回 HTMLResponse，绕过 TemplateResponse 的缓存 key 问题"""
    template = templates.get_template(template_name)
    return HTMLResponse(template.render(request=request, **context))


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
    merged = {**yaml_config, **db_config}
    return merged


def get_active_mode() -> str:
    """获取当前激活模式: 'simulation' 或 'live'"""
    db = get_db()
    return db.get_active_mode('simulation')


def load_mode_config(mode: str) -> Dict[str, Any]:
    """加载指定模式的配置。若未配置过则从 YAML 加载默认值"""
    db = get_db()
    cfg = db.get_mode_config(mode)
    if not cfg:
        yaml_defaults = load_config()
        if yaml_defaults:
            cfg = yaml_defaults.copy()
            cfg['SIMULATION'] = (mode == 'simulation')
            db.set_mode_config(mode, cfg)
    else:
        cfg['SIMULATION'] = (mode == 'simulation')
    return cfg


def save_mode_config(mode: str, config: Dict[str, Any]):
    """保存指定模式的配置"""
    db = get_db()
    db.set_mode_config(mode, config)


def initialize_trading():
    """初始化交易组件"""
    db = get_db()
    mode = get_active_mode()
    config = load_mode_config(mode)

    # 获取数据库中当前 mode 的余额（如果存在）
    saved_balance = db.get_balance(mode=mode)
    default_initial = config.get('TOTAL_CAPITAL', 2000)

    # 创建客户端
    _state.client = OKXClient(
        api_key=config.get('API_KEY', ''),
        secret_key=config.get('SECRET_KEY', ''),
        passphrase=config.get('PASSPHRASE', ''),
        simulation=config.get('SIMULATION', True)
    )

    # 每次重新初始化都清空 startup_warnings，避免列表无限增长
    _state.startup_warnings = []

    # 恢复余额 - 统一从 OKX API 读取（模拟盘/实盘走同一接口，区别在 header）
    balance_resp = _state.client.get_balance()
    try:
        if isinstance(balance_resp, dict) and balance_resp.get('code') == '0' and balance_resp.get('data'):
            real_balance = float(balance_resp['data'][0].get('totalEq', 0))
            _state.client.cached_balance = real_balance
            _state.client.sim_balance = real_balance
        elif saved_balance is not None:
            _state.client.cached_balance = saved_balance
            _state.client.sim_balance = saved_balance
        else:
            _state.client.reset_cached_balance(default_initial)
    except Exception as e:
        print(f"获取余额失败: {e}")
        if saved_balance is not None:
            _state.client.cached_balance = saved_balance
            _state.client.sim_balance = saved_balance
        else:
            _state.client.reset_cached_balance(default_initial)

    # 初始本金：实盘用第一次 OKX 拉到的真实余额（用户的真实本金），模拟盘用配置默认值
    # 这样亏损率/回撤都从 0% 起算，不会出现"300U 显示 85% 亏损"的误报
    is_live = not config.get('SIMULATION', True)
    has_real_balance = is_live and _state.client.cached_balance and _state.client.cached_balance > 0
    initial_balance = _state.client.cached_balance if has_real_balance else default_initial

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

    # 预加载合约规格 + 首单不变量检查（防止 _open_position 算出 0.5 张之类被 OKX 拒）
    symbol = strategy_config.symbol

    # 探测账户持仓模式（net_mode / long_short_mode）—— 不同模式 posSide 取值不同
    try:
        acct_cfg = _state.client.get_account_config()
        _state.client.logger.info(
            f"[ACCOUNT] posMode={acct_cfg['posMode']} acctLv={acct_cfg.get('acctLv', '')} "
            f"source={acct_cfg.get('_source', '')}"
        )
        # 实盘若 fallback 到默认值（未知 posMode），必须强提醒：后续下单会出错
        if (not config.get('SIMULATION', True)
                and acct_cfg.get('_source') == 'fallback'):
            warn_msg = (
                "⚠️  无法从 OKX 拉取真实账户配置（posMode），"
                "已 fallback 到 'net_mode'。"
                "若你的真实账户是 long_short_mode（双向持仓），"
                "首次下单会被 OKX 拒绝（51174）。"
                "请检查 API Key 是否包含 '读取' 账户配置权限。"
            )
            _state.client.logger.warning(warn_msg)
            _state.startup_warnings.append(warn_msg)
    except Exception as e:
        _state.client.logger.warning(f"[ACCOUNT] 获取账户配置失败：{e}，使用默认 posMode={_state.client._pos_mode}")

    try:
        spec = _state.client.get_instrument_spec(symbol)
        ct_val = spec['ctVal']
        lot_sz = spec['lotSz']
        min_sz = spec['minSz']
        _state.client.logger.info(
            f"[INSTRUMENT] {symbol}: ctVal={ct_val} lotSz={lot_sz} "
            f"minSz={min_sz} ctValCcy={spec.get('ctValCcy')}"
        )

        # 用当前已知价格估算首单名义价值（如果能拿到）
        try:
            ref_price = _state.client.get_current_price(symbol)
        except Exception:
            ref_price = 0.0

        if ref_price > 0:
            first_margin = strategy_config.first_order_size
            min_margin_for_min_sz = (min_sz * ct_val * ref_price) / strategy_config.leverage
            _state.client.logger.info(
                f"[CHECK] 首单 {first_margin} USDT @ {strategy_config.leverage}x "
                f"@ {ref_price:.2f} → "
                f"最小可下单名义={min_sz * ct_val * ref_price:.4f} USDT / "
                f"所需保证金={min_margin_for_min_sz:.4f} USDT"
            )
            if first_margin < min_margin_for_min_sz:
                warn_msg = (
                    f"⚠️  首单 {first_margin} USDT 保证金低于最小可下单量 "
                    f"{min_margin_for_min_sz:.4f} USDT (minSz={min_sz} lotSz={lot_sz})。"
                    f"实盘会被 OKX 拒（51119/51008 等）。"
                    f"建议把 FIRST_ORDER_SIZE 调到 >= {min_margin_for_min_sz:.2f}。"
                )
                _state.client.logger.warning(warn_msg)
                _state.startup_warnings.append(warn_msg)
    except Exception as e:
        _state.client.logger.warning(f"[INSTRUMENT] 预加载合约规格失败：{e}")

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
        _state.strategy.position.dca_count = saved_position.get('dca_count', 0)
        _state.strategy.position.first_entry_price = saved_position.get('first_entry_price', 0)
        _state.strategy.position.first_entry_time = saved_position.get('first_entry_time', '')
        # last_dca_price 必须同步恢复，否则 get_next_dca_price() 会算出"从 0 跌 0.8%"的错位
        _state.strategy.last_dca_price = saved_position.get('last_dca_price', saved_position.get('avg_price', 0))
        _state.strategy.base_price = saved_position.get('base_price', _state.strategy.last_dca_price)

    # 同步 _state.config，给只读路由（/api/status、/api/action/reset）使用
    _state.config = config

    # 创建模拟市场
    _state.market = SimulatedMarket(initial_price=65000.0, volatility=0.001)


def save_state():
    """保存当前状态到数据库"""
    if not _state.client:
        return

    db = get_db()
    mode = get_active_mode()

    # 保存余额（按 mode 拆分）
    db.set_balance(_state.client.get_cached_balance(), mode=mode)

    # 保存持仓状态
    if _state.strategy and not _state.strategy.position.is_empty():
        pos = _state.strategy.position
        db.set_state('current_position', {
            'side': pos.side.value,
            'total_size': pos.total_size,
            'avg_price': pos.avg_price,
            'dca_count': pos.dca_count,
            'first_entry_price': pos.first_entry_price,
            'first_entry_time': pos.first_entry_time,
            'last_dca_price': _state.strategy.last_dca_price,
            'base_price': _state.strategy.base_price,
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
    return render_template("index.html", request, title="交易状态")


@app.get("/trades", response_class=HTMLResponse)
async def trades_page(request: Request):
    """交易记录页面"""
    return render_template("trades.html", request, title="交易记录")


@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    """配置页面"""
    return render_template("config.html", request, title="策略配置")


# ============ API 接口 ============

@app.get("/api/status")
async def get_status(refresh_price: bool = True, refresh_balance: bool = True):
    """获取交易状态
    refresh_price: 是否重新拉取 OKX 实时价格
    refresh_balance: 是否重新拉取 OKX 账户余额
    """
    if not _state.strategy:
        initialize_trading()

    pos = _state.strategy.position
    mode = get_active_mode()
    symbol = _state.config.get('SYMBOL', 'BTC-USDT-SWAP')

    # ---------- 价格 ----------
    # 模拟盘：本地 SimulatedMarket（可被 set_market_price 等接口更新）
    # 实盘：直接请求 OKX /api/v5/market/ticker 公开行情
    current_price = 0.0
    price_source = 'unknown'
    if refresh_price:
        if mode == 'simulation':
            if _state.market:
                current_price = _state.market.get_price()
                price_source = 'simulated'
            else:
                current_price = 65000.0
                price_source = 'default'
        else:
            try:
                px = _state.client.get_current_price(symbol)
                if px and px > 0:
                    current_price = px
                    price_source = 'okx'
                elif _state.market:
                    current_price = _state.market.get_price()
                    price_source = 'simulated_fallback'
                else:
                    current_price = 0.0
                    price_source = 'unavailable'
            except Exception as e:
                print(f"获取 OKX 价格失败: {e}")
                current_price = _state.market.get_price() if _state.market else 0.0
                price_source = 'error'
    else:
        if _state.market:
            current_price = _state.market.get_price()
        else:
            current_price = 0.0

    # ---------- 余额 ----------
    balance = _state.client.cached_balance or 0
    balance_source = 'local'
    api_ok = bool(_state.client.api_key and _state.client.secret_key and _state.client.passphrase)

    if refresh_balance and api_ok:
        try:
            balance_resp = _state.client.get_balance()
            if isinstance(balance_resp, dict) and balance_resp.get('code') == '0' and balance_resp.get('data'):
                balance = float(balance_resp['data'][0].get('totalEq', 0))
                _state.client.cached_balance = balance
                _state.client.sim_balance = balance
                balance_source = 'okx'
                # 拿到 OKX 真实余额后立刻存进数据库对应 mode 行
                try:
                    db = get_db()
                    db.set_balance(balance, mode=mode)
                except Exception:
                    pass
            else:
                msg = balance_resp.get('msg', 'unknown') if isinstance(balance_resp, dict) else str(balance_resp)
                balance_source = f'okx_error:{msg}'
                print(f"获取余额失败: {balance_source}")
        except Exception as e:
            balance_source = f'exception:{e}'
            print(f"获取余额异常: {e}")
    elif not api_ok:
        balance_source = 'no_api_key'

    risk_status = _state.risk_manager.check_loss_risk(balance)

    # ---------- 浮动盈亏 ----------
    # OKX 永续合约盈亏公式: pnl = price_diff × total_sz × ctVal
    # 例如 BTC-USDT-SWAP: ctVal=0.01 BTC/张，size=0.03 张 -> 名义价值 = 0.03 × 0.01 BTC
    unrealized_pnl = 0
    pnl_rate = 0
    direction = _state.config.get('DIRECTION', 'long')
    ct_val = 1.0
    spec = getattr(_state.client, '_instrument_specs', {}).get(
        getattr(_state.strategy, 'config', None) and _state.strategy.config.symbol, {}
    )
    if spec and 'ctVal' in spec:
        ct_val = spec['ctVal']
    if not pos.is_empty() and current_price > 0 and pos.avg_price > 0:
        if direction == "long":
            price_diff = current_price - pos.avg_price
        else:
            price_diff = pos.avg_price - current_price
        unrealized_pnl = price_diff * pos.total_size * ct_val
        pnl_rate = price_diff / pos.avg_price * 100

    return {
        "success": True,
        "timestamp": datetime.now().isoformat(),
        "mode": mode,
        "simulation": mode == 'simulation',
        "price": float(current_price or 0),
        "price_source": price_source,
        "balance": float(balance or 0),
        "balance_source": balance_source,
        "api_configured": api_ok,
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
        "startup_warnings": getattr(_state, 'startup_warnings', []),
        "instrument_spec": getattr(_state.client, '_instrument_specs', {}).get(
            _state.strategy.config.symbol, {}
        ),
        "account_config": getattr(_state.client, '_account_config', {
            'posMode': getattr(_state.client, '_pos_mode', 'net_mode'),
            '_source': 'not_loaded',
        }),
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
    """获取当前配置 + 模式信息"""
    db = get_db()
    mode = get_active_mode()
    cfg = load_mode_config(mode)
    return {
        "success": True,
        "mode": mode,
        "config": cfg,
        "modes": {
            "simulation": load_mode_config('simulation'),
            "live": load_mode_config('live')
        }
    }


@app.post("/api/config")
async def update_config(request: Request):
    """更新配置（保存到当前激活模式）"""
    body = await request.json()
    mode = get_active_mode()

    cfg = load_mode_config(mode)
    cfg.update({
        'API_KEY': body.get('api_key', ''),
        'SECRET_KEY': body.get('secret_key', ''),
        'PASSPHRASE': body.get('passphrase', ''),
        'LEVERAGE': body.get('leverage', 2),
        'FIRST_ORDER_SIZE': body.get('first_order_size', 10),
        'MULTIPLIER': body.get('multiplier', 1.5),
        'PRICE_INTERVAL': body.get('price_interval', 0.008),
        'MAX_DCA_COUNT': body.get('max_dca_count', 7),
        'TAKE_PROFIT': body.get('take_profit', 0.02),
        'MAX_LOSS_RATE': body.get('max_loss_rate', 0.25),
        'TREND_PAUSE_RATE': body.get('trend_pause_rate', 0.05),
        'AUTO_LOOP': body.get('auto_loop', False),
        'DIRECTION': body.get('direction', 'long'),
        'SYMBOL': body.get('symbol', 'BTC-USDT-SWAP'),
        'TOTAL_CAPITAL': body.get('total_capital', 2000),
        'CHECK_INTERVAL': body.get('check_interval', 2),
    })

    save_mode_config(mode, cfg)
    _state.config = cfg

    # 同时保存到YAML（备份当前激活模式）
    save_config(cfg)

    initialize_trading()

    return {"success": True, "message": f"[{mode}] 配置已更新", "mode": mode}


@app.post("/api/mode/switch")
async def switch_mode(request: Request):
    """切换 模拟盘 / 实盘"""
    body = await request.json()
    mode = body.get('mode', 'simulation')
    if mode not in ('simulation', 'live'):
        return {"success": False, "message": "mode 必须是 simulation 或 live"}

    db = get_db()
    db.set_active_mode(mode)

    # 重新加载该模式下的配置
    cfg = load_mode_config(mode)
    _state.config = cfg

    # 重新初始化策略/客户端
    initialize_trading()

    return {"success": True, "message": f"已切换到{mode}", "mode": mode, "config": cfg}


@app.get("/api/mode")
async def get_mode():
    """获取当前激活模式"""
    mode = get_active_mode()
    return {"success": True, "mode": mode}


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

    msg = "开仓成功" if success else "开仓失败"
    if not success and getattr(_state.strategy, 'last_error', None):
        msg = _state.strategy.last_error
    return {
        "success": success,
        "message": msg
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
    _state.client.reset_cached_balance(initial)
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
    """WebSocket实时推送 - 价格 1秒/次，完整状态(余额) 5秒/次"""
    await manager.connect(websocket)
    last_full = 0
    try:
        while True:
            now = asyncio.get_event_loop().time()
            # 每 1 秒推一次轻量价格
            try:
                tick = await get_status(refresh_price=True, refresh_balance=False)
                await websocket.send_json({
                    "type": "tick",
                    "timestamp": tick.get("timestamp"),
                    "price": tick.get("price"),
                    "price_source": tick.get("price_source"),
                    "unrealized_pnl": tick.get("unrealized_pnl"),
                    "pnl_rate": tick.get("pnl_rate"),
                    "position": tick.get("position"),
                    "stats": tick.get("stats"),
                    "risk": tick.get("risk"),
                })
            except WebSocketDisconnect:
                manager.disconnect(websocket)
                return
            except (RuntimeError, ConnectionResetError):
                # RuntimeError 通常由 starlette 内部状态不正确（已关闭）抛出
                manager.disconnect(websocket)
                return
            except Exception as e:
                # 单条 tick 失败不退出循环
                print(f"WS tick error: {e}")

            # 每 5 秒推一次完整状态（含余额）
            if now - last_full >= 5:
                last_full = now
                try:
                    full = await get_status(refresh_price=False, refresh_balance=True)
                    full["type"] = "full"
                    await websocket.send_json(full)
                except WebSocketDisconnect:
                    manager.disconnect(websocket)
                    return
                except (RuntimeError, ConnectionResetError):
                    manager.disconnect(websocket)
                    return
                except Exception as e:
                    print(f"WS full error: {e}")

            await asyncio.sleep(1)
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        # 兜底：避免后台协程崩溃
        print(f"WS endpoint error: {e}")
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
