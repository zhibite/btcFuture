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

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
import secrets
import uvicorn

# 导入交易模块
from okx_client import OKXClient
from risk_manager import RiskManager, RiskConfig
from martingale import MartingaleStrategy, MartingaleConfig, SimulatedMarket
from database import Database, get_db

import logging


# ============ 日志（输出到 stderr，docker logs -f 可直接看到） ============
def _setup_logging() -> logging.Logger:
    """给 root logger 装一个 StreamHandler，让 martingale/okx_client 里的 logger.info 真正落地。

    日志级别跟随 config.yaml 中的 LOG_LEVEL（默认 INFO）。不写文件，避免日志被
    容器临时目录吞掉；部署到 docker logs -f 时直接可见。
    """
    level_name = str(_state_log_level()).upper()
    level = getattr(logging, level_name, logging.INFO)

    fmt = '%(asctime)s [%(levelname)s] %(name)s %(message)s'
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter(fmt, datefmt='%Y-%m-%d %H:%M:%S'))

    root = logging.getLogger()
    root.setLevel(level)
    # 幂等：避免 reload/重复 startup 时挂多个 handler
    root.handlers.clear()
    root.addHandler(handler)
    # 第三方噪音降级
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    return logging.getLogger("app")


def _state_log_level() -> str:
    """从 _state.config 里读 LOG_LEVEL，启动前调用则回落到 'INFO'。"""
    try:
        cfg = getattr(_state, "config", None) or {}
        return cfg.get("LOG_LEVEL", "INFO") if isinstance(cfg, dict) else "INFO"
    except Exception:
        return "INFO"


_app_logger = None


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
        # 后台策略循环（live 模式下驱动 run_one_cycle 用）
        self.strategy_task: Optional[asyncio.Task] = None
        self.strategy_task_lock = None  # 延迟初始化（asyncio.Lock 不能在 __init__ 里建）

_state = TradeState()


# ============ 鉴权（写死凭据） ============
# 用户名固定 zengsw，密码默认 anywnbtc076034，可通过环境变量 BTC_FUTURE_PASSWORD 覆盖
# 仅保护"会改变交易/系统行为"的页面与接口：/config、模式切换、所有 /api/action/* 与 /api/simulate
# 只读接口（/、/trades、/api/status、/api/trades、/api/mode、/api/config、/ws/status）不强制登录
AUTH_USERNAME = "zengsw"
AUTH_PASSWORD = os.environ.get("BTC_FUTURE_PASSWORD", "anywnbtc076034")

# 会话密钥：每次启动重新生成（保持简单；如需多 worker 部署再加持久化）
SESSION_SECRET = secrets.token_urlsafe(32)


def is_authenticated(request: Request) -> bool:
    return bool(request.session.get("user"))


def _resolve_current_price(symbol: str) -> tuple[float, str]:
    """按当前 mode 解析当下应该使用的价格 + 数据源。

    - live 模式：去 OKX /api/v5/market/ticker 拉真实 ticker；拿不到返回 0
    - simulation 模式：直接用本地 SimulatedMarket（被 /api/simulate 推动）
    - 都拿不到：返回 (0.0, 'unavailable')

    返回 (price, source)：source ∈ {'okx','simulated','unavailable'}。
    """
    mode = get_active_mode()
    if mode == 'live' and _state.client:
        try:
            px = _state.client.get_current_price(symbol)
            if px and px > 0:
                return px, 'okx'
        except Exception as e:
            if _app_logger:
                _app_logger.warning(f"[PRICE] live 模式拉 OKX 价格失败：{e}")
        return 0.0, 'unavailable'
    if _state.market:
        return _state.market.get_price(), 'simulated'
    return 0.0, 'unavailable'


def require_login(request: Request):
    """依赖：未登录则 302 到 /login。用于受保护路由。"""
    if not is_authenticated(request):
        # 保留 next 参数，登录后跳转回
        next_url = request.url.path
        if request.url.query:
            next_url = f"{next_url}?{request.url.query}"
        raise HTTPFoundRedirect(f"/login?next={next_url}")
    return request


class HTTPFoundRedirect(Exception):
    """依赖层用 raise 中转到登录页（302）"""
    def __init__(self, location: str):
        self.location = location


# ============ FastAPI 应用 ============
app = FastAPI(
    title="BTC马丁策略交易系统",
    description="OKX期货马丁格尔策略Web控制台",
    version="1.0.0"
)

# Session 中间件（cookie 存已登录标记）
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="btc_future_session",
    max_age=60 * 60 * 24 * 7,  # 7 天
    same_site="lax",
)


# ============ Exception handler：把依赖层 raise 的重定向翻成 302 ============
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi import HTTPException


@app.exception_handler(HTTPFoundRedirect)
async def _redirect_handler(request: Request, exc: HTTPFoundRedirect):
    return RedirectResponse(url=exc.location, status_code=302)

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
    # 修复：原来的判断是 `if saved_position and not _state.strategy.position.is_empty()`，
    # 但启动时 _state.strategy.position 永远是默认空仓（side=NONE, total_size=0），
    # 导致 saved_position 永远不会被恢复，git pull / 重启后持仓就"凭空消失"。
    # 现在改成"DB 里如果有 saved_position，就覆盖回写到策略对象里"。
    saved_position = db.get_state('current_position')
    if saved_position:
        # 防呆：saved_position 可能是手贱被 set_state('current_position', None) 之后又 set_state({}, ...) 之类
        if not isinstance(saved_position, dict):
            if _app_logger:
                _app_logger.warning(f"[RESTORE] current_position 类型异常：{type(saved_position).__name__}，跳过恢复")
        elif saved_position.get('side') in (None, '', 'none') or saved_position.get('total_size', 0) <= 0:
            # 没有真实方向或张数，视为"空仓记录"——不恢复，但仍保留 DB 中的值以便审计
            if _app_logger:
                _app_logger.info(
                    f"[RESTORE] DB 中 current_position 是空仓记录 (side={saved_position.get('side')}, "
                    f"size={saved_position.get('total_size', 0)})，策略按空仓启动"
                )
        else:
            # 恢复持仓信息。注意：DB 里存的是字符串 ('long'/'short'/'none')，
            # Position.side 字段是 PositionSide enum——必须显式转 enum，否则后续
            # `pos.side == PositionSide.LONG` 这种比较永远 False，会引发各种诡异 bug。
            from martingale import PositionSide
            side_raw = str(saved_position.get('side', 'none')).lower()
            try:
                side_enum = PositionSide(side_raw)
            except ValueError:
                if _app_logger:
                    _app_logger.warning(f"[RESTORE] DB 中 side='{side_raw}' 无法解析为 PositionSide，按 LONG 处理")
                side_enum = PositionSide.LONG

            _state.strategy.position.side = side_enum
            _state.strategy.position.total_size = float(saved_position.get('total_size', 0))
            _state.strategy.position.avg_price = float(saved_position.get('avg_price', 0))
            _state.strategy.position.dca_count = int(saved_position.get('dca_count', 0))
            _state.strategy.position.first_entry_price = float(saved_position.get('first_entry_price', 0))
            _state.strategy.position.first_entry_time = saved_position.get('first_entry_time', '')
            _state.strategy.position.margin = float(saved_position.get('margin', 0))
            # last_dca_price 必须同步恢复，否则 get_next_dca_price() 会算出"从 0 跌 0.8%"的错位
            _state.strategy.last_dca_price = float(
                saved_position.get('last_dca_price', saved_position.get('avg_price', 0))
            )
            _state.strategy.position.base_price = float(
                saved_position.get('base_price', _state.strategy.last_dca_price)
            )
            # cycle_state 要切回 POSITION_OPEN，否则 run_one_cycle 会走 IDLE 分支去尝试开新仓
            from martingale import CycleState
            _state.strategy.cycle_state = CycleState.POSITION_OPEN
            if _app_logger:
                _app_logger.info(
                    f"[RESTORE] 从 DB 恢复持仓: 方向={side_enum.value} "
                    f"张数={_state.strategy.position.total_size} 均价={_state.strategy.position.avg_price:.2f} "
                    f"加仓={_state.strategy.position.dca_count}/{_state.strategy.config.max_dca_count} "
                    f"last_dca={_state.strategy.last_dca_price:.2f}"
                )

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
            'margin': pos.margin,
            'last_dca_price': _state.strategy.last_dca_price,
            'base_price': _state.strategy.base_price,
        })


# ============ 路由定义 ============

@app.on_event("startup")
async def startup_event():
    """应用启动"""
    global _app_logger
    # 必须在 initialize_trading 之前：策略里的 logger.info 才会被收集
    _app_logger = _setup_logging()
    _app_logger.info("=" * 60)
    _app_logger.info("[START] BTC 马丁策略 Web 服务启动")
    _app_logger.info("=" * 60)
    initialize_trading()
    _app_logger.info("[READY] 初始化完成，等待登录与指令")
    # 启动后台策略循环（live + simulation 都启用）。这是实盘"价格到了自动加仓"的真正驱动。
    await _start_strategy_loop()


@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭时保存状态"""
    await _stop_strategy_loop()
    if _app_logger:
        _app_logger.info("[STOP] 服务关闭，保存状态")
    save_state()


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """首页 - 交易状态"""
    return render_template("index.html", request,
                           title="交易状态",
                           authed=is_authenticated(request))


@app.get("/trades", response_class=HTMLResponse)
async def trades_page(request: Request):
    """交易记录页面"""
    return render_template("trades.html", request,
                           title="交易记录",
                           authed=is_authenticated(request))


@app.get("/config", response_class=HTMLResponse, dependencies=[Depends(require_login)])
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
                if _app_logger:
                    _app_logger.warning("[STATUS] _state.market is None, returning price=0")
                current_price = 0.0
                price_source = 'uninitialized'
        else:
            try:
                px = _state.client.get_current_price(symbol)
                if px and px > 0:
                    current_price = px
                    price_source = 'okx'
                else:
                    current_price = 0.0
                    price_source = 'unavailable'
            except Exception as e:
                print(f"获取 OKX 价格失败: {e}")
                current_price = 0.0
                price_source = 'error'
    else:
        current_price = 0.0
        price_source = 'unknown'

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
        leverage = _state.strategy.config.leverage
        pnl_rate = price_diff / pos.avg_price * leverage * 100

    # ---------- 保证金 / 名义价值 / 杠杆倍数 ----------
    pos_margin = pos.margin if not pos.is_empty() else 0
    pos_notional = 0.0
    pos_leverage_ratio = 0.0
    if not pos.is_empty() and current_price > 0:
        # 名义价值 = 张数 × ctVal × 当前价格
        pos_notional = pos.total_size * ct_val * current_price
        if pos_margin > 0:
            pos_leverage_ratio = pos_notional / pos_margin
    # 尝试从 OKX 持仓接口拉取真实 MMR（live 模式）
    okx_mmr_rate = 0.0
    okx_margin_from_api = 0.0
    if api_ok and not pos.is_empty():
        try:
            okx_positions = _state.client.get_position(_state.strategy.config.symbol)
            for p in okx_positions:
                if float(p.get('pos', '0') or 0) != 0:
                    okx_margin_from_api = float(p.get('margin', '0') or 0)
                    raw_mmr = float(p.get('maintMarginRatio', '0') or 0)
                    if raw_mmr > 0:
                        okx_mmr_rate = raw_mmr * 100
                    elif okx_margin_from_api > 0 and pos_notional > 0:
                        # maintMarginRatio 未返回时，用 margin/notional 反推杠杆比
                        okx_mmr_rate = okx_margin_from_api / pos_notional * 100
                    if okx_margin_from_api > 0:
                        pos_margin = okx_margin_from_api
                    break
        except Exception:
            pass

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
            "first_entry_time": pos.first_entry_time,
            "margin": pos_margin,
            "notional": pos_notional,
            "mmr_rate": okx_mmr_rate if okx_mmr_rate > 0 else pos_leverage_ratio,
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

    # 优先从内存获取（实时、包含最新统计数据）
    if _state.strategy:
        trades = _state.strategy.recorder.trades
        stats = _state.strategy.stats
        summary = {
            'total_trades': stats.get('total_cycles', 0),
            'profit_trades': stats.get('profit_cycles', 0),
            'loss_trades': stats.get('loss_cycles', 0),
            'total_profit': stats.get('total_profit', 0.0),
            'total_loss': stats.get('total_loss', 0.0),
            'net_profit': stats.get('total_profit', 0.0) - stats.get('total_loss', 0.0),
        }
    else:
        trades = db.get_trades()
        summary = db.get_trades_summary()

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


@app.post("/api/config", dependencies=[Depends(require_login)])
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

    # CHECK_INTERVAL 变了，重启后台循环以应用新间隔
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(_start_strategy_loop())
    except RuntimeError:
        pass

    if _app_logger:
        _app_logger.info(
            f"[CONFIG] [{mode}] 更新配置: 杠杆={cfg['LEVERAGE']}x "
            f"首单={cfg['FIRST_ORDER_SIZE']}U 方向={cfg['DIRECTION']} "
            f"加仓x{cfg['MULTIPLIER']} 间隔={cfg['PRICE_INTERVAL']*100:.1f}% "
            f"最大加仓={cfg['MAX_DCA_COUNT']} 止盈={cfg['TAKE_PROFIT']*100:.1f}% "
            f"止损={cfg['MAX_LOSS_RATE']*100:.0f}% AUTO_LOOP={cfg['AUTO_LOOP']}"
        )

    return {"success": True, "message": f"[{mode}] 配置已更新", "mode": mode}


@app.post("/api/mode/switch", dependencies=[Depends(require_login)])
async def switch_mode(request: Request):
    """切换 模拟盘 / 实盘"""
    body = await request.json()
    mode = body.get('mode', 'simulation')
    if mode not in ('simulation', 'live'):
        return {"success": False, "message": "mode 必须是 simulation 或 live"}

    prev = get_active_mode()
    db = get_db()
    db.set_active_mode(mode)

    # 重新加载该模式下的配置
    cfg = load_mode_config(mode)
    _state.config = cfg

    # 重新初始化策略/客户端
    initialize_trading()

    # 重启后台策略循环：旧 client/strategy 已经被换掉了，跑下去会拿错对象
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(_start_strategy_loop())
    except RuntimeError:
        # 没有运行中的 loop（同步场景），不重启循环也无影响
        pass

    if _app_logger:
        _app_logger.info(f"[MODE] 切换交易模式: {prev} -> {mode}")

    return {"success": True, "message": f"已切换到{mode}", "mode": mode, "config": cfg}


@app.get("/api/mode")
async def get_mode():
    """获取当前激活模式"""
    mode = get_active_mode()
    return {"success": True, "mode": mode}


@app.post("/api/action/open", dependencies=[Depends(require_login)])
async def action_open():
    """开仓"""
    if not _state.strategy:
        return {"success": False, "message": "策略未初始化"}

    if not _state.strategy.position.is_empty():
        if _app_logger:
            _app_logger.warning("[OPEN] 拒绝：已有持仓")
        return {"success": False, "message": "已有持仓"}

    symbol = _state.strategy.config.symbol
    price, source = _resolve_current_price(symbol)
    if price <= 0:
        return {"success": False, "message": "当前不可用价格（OKX + 模拟盘都拉不到）"}

    # 与后台循环互斥：手动操作期间阻塞 _strategy_loop 跑 run_one_cycle，
    # 避免同时改 last_price / 同时下 place_order
    if _state.strategy_task_lock is None:
        _state.strategy_task_lock = asyncio.Lock()
    async with _state.strategy_task_lock:
        _state.strategy.last_price = price
        if _app_logger:
            _app_logger.info(
                f"[OPEN-REQ] 收到开仓请求: 方向={_state.strategy.config.direction} "
                f"首单={_state.strategy.config.first_order_size}U 价格={price:.2f} (source={source})"
            )
        success = _state.strategy.check_and_open()

    # 保存状态
    save_state()

    msg = "开仓成功" if success else "开仓失败"
    if not success and getattr(_state.strategy, 'last_error', None):
        msg = _state.strategy.last_error
    if _app_logger:
        level_fn = _app_logger.info if success else _app_logger.warning
        level_fn(f"[OPEN-RES] {msg} @ {price:.2f}")
    return {
        "success": success,
        "message": msg
    }


@app.post("/api/action/close", dependencies=[Depends(require_login)])
async def action_close():
    """平仓"""
    if not _state.strategy:
        return {"success": False, "message": "策略未初始化"}

    if _state.strategy.position.is_empty():
        if _app_logger:
            _app_logger.warning("[CLOSE] 拒绝：无持仓")
        return {"success": False, "message": "无持仓"}

    symbol = _state.strategy.config.symbol
    price, source = _resolve_current_price(symbol)
    if price <= 0:
        return {"success": False, "message": "当前不可用价格（OKX + 模拟盘都拉不到）"}
    pos_before = _state.strategy.position
    size_before = pos_before.total_size
    dca_before = pos_before.dca_count
    avg_before = pos_before.avg_price

    if _state.strategy_task_lock is None:
        _state.strategy_task_lock = asyncio.Lock()
    async with _state.strategy_task_lock:
        _state.strategy.last_price = price
        if _app_logger:
            _app_logger.info(
                f"[CLOSE-REQ] 手动平仓请求: 方向={pos_before.side.value} "
                f"张数={size_before} 均价={avg_before:.2f} 加仓={dca_before} 价格={price:.2f} (source={source})"
            )
        success = _state.strategy._close_position()

    # 保存状态
    save_state()

    if _app_logger:
        level_fn = _app_logger.info if success else _app_logger.warning
        level_fn(f"[CLOSE-RES] {'成功' if success else '失败'} @ {price:.2f}")
    return {
        "success": success,
        "message": "平仓成功" if success else "平仓失败"
    }


@app.post("/api/action/add", dependencies=[Depends(require_login)])
async def action_add():
    """加仓（手动）。注意：自动加仓由后台 _strategy_loop 驱动，
    大多数情况下你不需要点这个按钮。
    """
    if not _state.strategy:
        return {"success": False, "message": "策略未初始化"}

    if _state.strategy.position.is_empty():
        if _app_logger:
            _app_logger.warning("[ADD] 拒绝：无持仓")
        return {"success": False, "message": "无持仓"}

    symbol = _state.strategy.config.symbol
    price, source = _resolve_current_price(symbol)
    if price <= 0:
        return {"success": False, "message": "当前不可用价格（OKX + 模拟盘都拉不到）"}
    pos_before = _state.strategy.position
    next_dca_price = getattr(_state.strategy, 'next_dca_price', 0) or 0

    if _state.strategy_task_lock is None:
        _state.strategy_task_lock = asyncio.Lock()
    async with _state.strategy_task_lock:
        _state.strategy.last_price = price
        if _app_logger:
            max_dca = _state.strategy.config.max_dca_count
            _app_logger.info(
                f"[ADD-REQ] 手动加仓请求: 当前加仓={pos_before.dca_count}/{max_dca} "
                f"均价={pos_before.avg_price:.2f} 触发价={next_dca_price:.2f} 价格={price:.2f} (source={source})"
            )
        success = _state.strategy.check_and_add_position()

    # 保存状态
    save_state()

    if _app_logger:
        if success:
            pos_after = _state.strategy.position
            _app_logger.info(
                f"[ADD-RES] 成功 #{pos_after.dca_count} 张数={pos_after.total_size:.4f} "
                f"均价={pos_after.avg_price:.2f} @ {price:.2f}"
            )
        else:
            # 把"为什么不满足"也讲清楚，避免用户再来一次问同样的问题
            last_dca = _state.strategy.last_dca_price
            if pos_before.dca_count >= _state.strategy.config.max_dca_count:
                why = f"已达最大加仓次数 {pos_before.dca_count}/{_state.strategy.config.max_dca_count}"
            elif last_dca > 0:
                interval_pct = (last_dca - price) / last_dca * 100 if pos_before.side.value == 'long' else (price - last_dca) / last_dca * 100
                if interval_pct < 0:
                    why = f"价格朝不利方向变动（{interval_pct:+.2f}%），未触发加仓"
                else:
                    why = (f"距上次加仓仅 {interval_pct:.2f}%，未达触发阈值 "
                           f"{_state.strategy.config.price_interval*100:.2f}% "
                           f"（last_dca={last_dca:.2f} 当前={price:.2f}）")
            else:
                why = "上次加仓价异常（last_dca_price<=0）"
            _app_logger.info(f"[ADD-RES] 加仓条件未满足：{why} @ {price:.2f}")
    return {
        "success": success,
        "message": "加仓成功" if success else "加仓条件未满足"
    }


@app.post("/api/position/sync", dependencies=[Depends(require_login)])
async def action_sync_position():
    """用 OKX 真实持仓覆盖本地策略的持仓状态。

    用于以下场景：手动在 OKX 下单 / 策略用本地 SimulatedMarket 价格开仓后
    与 OKX 真实成交价不一致。同步后风控、止盈、止损都按真实均价重新计算。
    """
    if not _state.strategy or not _state.client:
        return {"success": False, "message": "策略或客户端未初始化"}

    mode = get_active_mode()
    if mode != 'live':
        return {"success": False, "message": "仅 live 模式需要从 OKX 同步；simulation 模式请直接重置"}

    symbol = _state.strategy.config.symbol
    try:
        positions = _state.client.get_position(symbol)
    except Exception as e:
        if _app_logger:
            _app_logger.error(f"[SYNC] 拉 OKX 持仓失败：{e}")
        return {"success": False, "message": f"拉 OKX 持仓失败：{e}"}

    pos = _state.strategy.position
    target = None
    for p in positions:
        if float(p.get('pos', '0') or 0) != 0:
            target = p
            break

    if not target:
        # OKX 上没持仓，但本地有：清掉本地持仓
        avg_before = pos.avg_price
        first_before = pos.first_entry_price
        if _state.strategy_task_lock is None:
            _state.strategy_task_lock = asyncio.Lock()
        async with _state.strategy_task_lock:
            _state.strategy._reset_position()
        save_state()
        if _app_logger:
            _app_logger.warning(
                f"[SYNC] OKX 上无持仓，但本地有 (均价={avg_before:.2f} 首单={first_before:.2f})，已清空"
            )
        return {"success": True, "message": "OKX 无持仓，本地已清空", "okx_empty": True}

    # 把 OKX 持仓对拷到本地
    okx_pos = float(target.get('pos', '0') or 0)
    okx_avg = float(target.get('avgPx', '0') or 0)
    okx_side_raw = target.get('posSide', '').lower()   # 'long'/'short'/'net'

    # 均价异常保护：OKX 在某些成交失败/刚下单瞬间可能回空或负值
    if okx_pos <= 0 or okx_avg <= 0:
        if _app_logger:
            _app_logger.warning(
                f"[SYNC] OKX 回值异常：pos={okx_pos}, avgPx={okx_avg}，拒绝同步以免污染本地"
            )
        return {
            "success": False,
            "message": f"OKX 回值异常：pos={okx_pos}, avgPx={okx_avg}（可能刚下单未成交），拒绝同步",
        }

    if _state.strategy_task_lock is None:
        _state.strategy_task_lock = asyncio.Lock()
    async with _state.strategy_task_lock:
        # 自适应合约张数：OKX 给的 pos 是张数，已经对齐 lotSz
        from martingale import PositionSide
        side = _state.strategy._get_position_side() if hasattr(_state.strategy, '_get_position_side') else pos.side
        if okx_side_raw == 'short':
            side = PositionSide.SHORT
        elif okx_side_raw == 'long':
            side = PositionSide.LONG

        pos.total_size = abs(okx_pos)
        pos.avg_price = okx_avg
        pos.first_entry_price = okx_avg
        pos.first_entry_time = pos.first_entry_time or time.strftime('%Y-%m-%d %H:%M:%S')

        # dca_count：从 entry_prices 推算；如果列表为空（典型首单）则按 0 处理
        # 同时用 OKX 创建时间辅助判断：entry_prices 元素数 - 1 表示已加仓的次数
        entry_prices = getattr(pos, 'entry_prices', None)
        if entry_prices and len(entry_prices) > 0:
            pos.dca_count = max(0, len(entry_prices) - 1)
        else:
            pos.dca_count = 0

        pos.side = side
        _state.strategy.base_price = okx_avg
        _state.strategy.last_dca_price = okx_avg

    save_state()
    if _app_logger:
        _app_logger.warning(
            f"[SYNC] 用 OKX 真实持仓覆盖本地: 方向={side.value} 张数={pos.total_size:.4f} "
            f"均价={okx_avg:.2f} dca_count={pos.dca_count} (只对齐均价/张数/方向)"
        )
    return {
        "success": True,
        "message": f"已对齐 OKX 真实持仓：{side.value} 张数={pos.total_size:.4f} 均价={okx_avg:.2f}",
        "okx_position": {
            "side": side.value,
            "size": pos.total_size,
            "avg_price": okx_avg,
            "dca_count": pos.dca_count,
        }
    }


@app.post("/api/position/clear", dependencies=[Depends(require_login)])
async def action_clear_position():
    """只清掉本地持仓（不改变余额和统计）。

    用于：OKX 上手动平仓了，想让本地也保持同步清空。
    不影响余额、统计数据、交易记录。
    """
    if not _state.strategy:
        return {"success": False, "message": "策略未初始化"}

    pos = _state.strategy.position
    if pos.is_empty():
        return {"success": True, "message": "本地已无持仓，无需清理"}

    avg = pos.avg_price
    size = pos.total_size
    side = pos.side.value if hasattr(pos.side, 'value') else pos.side

    if _state.strategy_task_lock is None:
        _state.strategy_task_lock = asyncio.Lock()
    async with _state.strategy_task_lock:
        _state.strategy._reset_position()

    save_state()

    if _app_logger:
        _app_logger.warning(
            f"[CLEAR] 手动清掉本地持仓: 方向={side} 张数={size:.4f} 均价={avg:.2f}"
        )

    return {"success": True, "message": f"已清掉本地持仓（方向={side} 张数={size:.4f} 均价={avg:.2f}）"}


@app.post("/api/action/reset", dependencies=[Depends(require_login)])
async def action_reset():
    """重置余额"""
    if not _state.client:
        return {"success": False, "message": "客户端未初始化"}

    initial = _state.config.get('TOTAL_CAPITAL', 2000)
    if _app_logger:
        _app_logger.warning(
            f"[RESET] 重置余额为 {initial} USDT，并清空统计/交易记录（持仓={None if _state.strategy is None else ('空' if _state.strategy.position.is_empty() else '有')}）"
        )
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

    if _app_logger:
        _app_logger.info(f"[RESET] 完成，余额={initial} USDT")
    return {"success": True, "message": f"余额已重置为 {initial} USDT"}


@app.post("/api/simulate", dependencies=[Depends(require_login)])
async def simulate_price(action: str = Form(...)):
    """模拟价格变动（仅 simulation 模式可用）

    live 模式下调它会让本地 SimulatedMarket 价格漂移，
    但 OKX 真实行情不会变，会误导决策，所以这里直接 400 拒绝。
    """
    if not _state.market:
        return {"success": False, "message": "市场未初始化"}

    mode = get_active_mode()
    if mode == 'live':
        if _app_logger:
            _app_logger.warning(f"[SIM] 拒绝：live 模式下调模拟价格动作：{action}")
        return {
            "success": False,
            "message": "实盘模式禁用模拟价格变动（真实价格由 OKX 驱动，不能手动推动）",
        }

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

    if _app_logger:
        _app_logger.info(f"[SIM] 模拟价格动作: {action} -> 价格={new_price:.2f}")

    # 与后台策略循环互斥，避免 run_one_cycle 并发改 last_price
    if _state.strategy_task_lock is None:
        _state.strategy_task_lock = asyncio.Lock()
    async with _state.strategy_task_lock:
        # 运行策略逻辑（external_price 让 run_one_cycle 直接用传入的模拟价格）
        _state.strategy.run_one_cycle(external_price=new_price)

    # 保存状态
    save_state()

    return {
        "success": True,
        "price": new_price
    }


# ============ 登录 / 登出 ============

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    """登录页：未登录访问受保护路由会自动跳到这里"""
    # 已登录直接跳转
    if is_authenticated(request):
        return RedirectResponse(url=next or "/", status_code=302)
    return render_template("login.html", request,
                           error=request.query_params.get("error"),
                           next=next or "/")


@app.post("/login")
async def login_submit(request: Request, username: str = Form(...),
                       password: str = Form(...), next: str = Form(default="/")):
    """校验写死的凭据，校验通过种 session"""
    if username == AUTH_USERNAME and password == AUTH_PASSWORD:
        request.session["user"] = AUTH_USERNAME
        # 阻止 open redirect：只允许同源相对路径
        target = next if next.startswith("/") and not next.startswith("//") else "/"
        if _app_logger:
            client = request.client.host if request.client else "?"
            _app_logger.info(f"[AUTH] 登录成功: user={username} from={client} -> {target}")
        return RedirectResponse(url=target, status_code=302)
    if _app_logger:
        _app_logger.warning(f"[AUTH] 登录失败: user={username}")
    return render_template("login.html", request,
                           error="用户名或密码错误",
                           next=next or "/")


@app.post("/logout")
async def logout(request: Request):
    user = request.session.get("user")
    request.session.clear()
    if _app_logger:
        _app_logger.info(f"[AUTH] 登出: user={user}")
    return RedirectResponse(url="/login", status_code=302)


# 便捷 GET 版登出（点链接也能退出）
@app.get("/logout")
async def logout_get(request: Request):
    user = request.session.get("user")
    request.session.clear()
    if _app_logger:
        _app_logger.info(f"[AUTH] 登出: user={user}")
    return RedirectResponse(url="/login", status_code=302)


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
                    full = await get_status(refresh_price=True, refresh_balance=True)
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


# ============ 后台策略循环 ============
# 之前 Web 版是纯被动模式：只有手动 / 手动点"加仓"按钮才会触发 run_one_cycle。
# 实盘 (live) 下这意味着 OKX 价格已经跌穿触发价，但策略根本不知道，也不会加仓。
# 这里加一个 asyncio 后台任务，每 CHECK_INTERVAL 秒拉一次 OKX 真实价格 → 跑一次
# run_one_cycle（包含开仓 / 加仓 / 止盈 / 止损 全套逻辑）。
#
# 模拟盘 (simulation) 也启用，价格来源是本地 SimulatedMarket，行为与 live 一致。
# 这条路径完全独立于 /api/simulate（那个只更新本地市场价做手动试价）。
#
# 关键设计：
# - 单实例：通过 _state.strategy_task + cancel + await 实现"切换 mode / 更新配置时无缝重启"
# - 失败自愈：单次循环抛异常只记日志，下次 sleep 后继续跑，不让协程死掉
# - 互斥：initialize_trading 切换 client/strategy 时拿锁，避免一边重启一边还在跑旧对象


async def _strategy_loop():
    """后台策略循环：每 CHECK_INTERVAL 秒跑一次 run_one_cycle()

    与 main.py 的 while self.running 同源，迁移到 asyncio 适配 FastAPI。
    """
    # 循环退出标志：app 关闭时会 cancel 这个 task，外部不需要再翻 _state.running
    log = logging.getLogger("StrategyLoop")
    log.info("[LOOP] 后台策略循环启动")

    while True:
        try:
            # 检查 _state.strategy 是不是已经被切掉了（initialize_trading 重启）
            if not _state.strategy or not _state.client:
                await asyncio.sleep(2)
                continue

            # 处理止盈后的自动循环等待（_close_position 标记，由本循环异步等待，避免阻塞）
            if _state.strategy._pending_auto_loop_wait:
                _state.strategy._pending_auto_loop_wait = False
                await asyncio.sleep(5)

            # 加锁，避免跟 initialize_trading / switch_mode 抢资源
            if _state.strategy_task_lock is not None:
                async with _state.strategy_task_lock:
                    if not _state.strategy or not _state.client:
                        continue
                    symbol = _state.strategy.config.symbol
                    price, _ = _resolve_current_price(symbol)
                    if price > 0:
                        _state.strategy.last_price = price
                    _state.strategy.run_one_cycle()
            else:
                symbol = _state.strategy.config.symbol
                price, _ = _resolve_current_price(symbol)
                if price > 0:
                    _state.strategy.last_price = price
                _state.strategy.run_one_cycle()

            interval = _state.config.get('CHECK_INTERVAL', 2) or 2
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            log.info("[LOOP] 后台策略循环收到取消信号，退出")
            raise
        except Exception as e:
            # 单次失败不能让循环死掉；用 logging 防止 _app_logger 未初始化时报 NameError
            log.exception(f"[LOOP] 单次循环异常（将继续下一轮）: {e}")
            await asyncio.sleep(2)


async def _start_strategy_loop():
    """启动后台策略循环。重复调用会先停旧的再起新的，天然支持热重启。"""
    if _state.strategy_task_lock is None:
        _state.strategy_task_lock = asyncio.Lock()

    if _state.strategy_task and not _state.strategy_task.done():
        _state.strategy_task.cancel()
        try:
            await _state.strategy_task
        except (asyncio.CancelledError, Exception):
            pass

    _state.strategy_task = asyncio.create_task(_strategy_loop())
    if _app_logger:
        interval = _state.config.get('CHECK_INTERVAL', 2) or 2
        _app_logger.info(f"[LOOP] 后台策略循环已启动，interval={interval}s")


async def _stop_strategy_loop():
    """停止后台策略循环（app shutdown 时调用）"""
    if _state.strategy_task and not _state.strategy_task.done():
        _state.strategy_task.cancel()
        try:
            await _state.strategy_task
        except (asyncio.CancelledError, Exception):
            pass
        _state.strategy_task = None
    if _app_logger:
        _app_logger.info("[LOOP] 后台策略循环已停止")


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
