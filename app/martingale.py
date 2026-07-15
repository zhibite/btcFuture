"""
马丁格尔策略核心引擎
实现完整的期货马丁格尔交易策略
"""

import time
import logging
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime

from okx_client import OKXClient
from risk_manager import RiskManager, RiskConfig, RiskStatus, RiskLevel, TradeRecorder


class PositionSide(Enum):
    """持仓方向"""
    LONG = "long"
    SHORT = "short"
    NONE = "none"


class CycleState(Enum):
    """交易周期状态"""
    IDLE = "idle"           # 空闲，等待开仓
    POSITION_OPEN = "open"   # 已开仓
    DCA_IN_PROGRESS = "dca" # 加仓中
    TAKING_PROFIT = "profit" # 止盈中
    STOP_LOSS = "stop"       # 止损中
    CLOSED = "closed"        # 已平仓


@dataclass
class Position:
    """持仓信息"""
    side: PositionSide = PositionSide.NONE
    total_size: float = 0.0           # 总持仓量
    avg_price: float = 0.0            # 平均入场价
    entry_prices: List[float] = field(default_factory=list)  # 各层级入场价
    sizes: List[float] = field(default_factory=list)  # 各层级仓位大小
    dca_count: int = 0                # 加仓次数
    first_entry_price: float = 0.0     # 首次入场价
    first_entry_time: str = ""         # 首次入场时间
    unrealized_pnl: float = 0.0       # 未实现盈亏
    entry_order_ids: List[str] = field(default_factory=list)  # 入场订单ID列表

    def is_empty(self) -> bool:
        return self.side == PositionSide.NONE or self.total_size == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'side': self.side.value,
            'total_size': self.total_size,
            'avg_price': self.avg_price,
            'entry_prices': self.entry_prices,
            'sizes': self.sizes,
            'dca_count': self.dca_count,
            'first_entry_price': self.first_entry_price,
            'first_entry_time': self.first_entry_time,
            'unrealized_pnl': self.unrealized_pnl
        }


@dataclass
class MartingaleConfig:
    """马丁策略配置"""
    leverage: int = 2                # 杠杆倍数
    first_order_size: float = 10.0   # 首单保证金(USDT)
    multiplier: float = 1.5           # 加仓倍数
    price_interval: float = 0.008     # 价格间隔 (0.8%)
    max_dca_count: int = 7            # 最大加仓次数
    take_profit: float = 0.02         # 止盈目标 (2%)
    symbol: str = "BTC-USDT-SWAP"     # 交易对
    auto_loop: bool = False           # 自动循环
    direction: str = "long"            # 交易方向: long=做多, short=做空


class MartingaleStrategy:
    """
    马丁格尔交易策略

    策略逻辑:
    1. 在下跌过程中分批买入 (做多)
    2. 每次下跌一定比例后加仓
    3. 价格上涨到目标后一次性止盈
    4. 亏损达到阈值时止损
    """

    def __init__(self, client: OKXClient,
                 risk_manager: RiskManager,
                 config: MartingaleConfig):
        """
        初始化马丁策略

        Args:
            client: OKX客户端
            risk_manager: 风控管理器
            config: 策略配置
        """
        self.client = client
        self.risk_manager = risk_manager
        self.config = config
        self.recorder = TradeRecorder()
        self.logger = logging.getLogger("MartingaleStrategy")

        # 当前状态
        self.position = Position()
        self.cycle_state = CycleState.IDLE
        self.last_price = 0.0
        self.last_dca_price = 0.0
        self.base_price = 0.0  # 周期开始时的基准价格
        self.pending_orders: List[str] = []

        # 统计数据
        self.stats = {
            'total_cycles': 0,
            'profit_cycles': 0,
            'loss_cycles': 0,
            'total_profit': 0.0,
            'total_loss': 0.0,
            'start_time': time.time()
        }

    def initialize(self) -> bool:
        """
        初始化策略

        Returns:
            是否初始化成功
        """
        self.logger.info("=" * 50)
        self.logger.info("初始化马丁格尔策略...")
        self.logger.info(f"交易对: {self.config.symbol}")
        self.logger.info(f"杠杆: {self.config.leverage}x")
        self.logger.info(f"首单: {self.config.first_order_size} USDT")
        self.logger.info(f"加仓倍数: {self.config.multiplier}")
        self.logger.info(f"价格间隔: {self.config.price_interval * 100}%")
        self.logger.info(f"最大加仓: {self.config.max_dca_count}次")
        self.logger.info(f"止盈目标: {self.config.take_profit * 100}%")
        self.logger.info("=" * 50)

        # 设置杠杆
        result = self.client.set_leverage(self.config.symbol, self.config.leverage)
        if result.get('code') != '0':
            self.logger.warning(f"设置杠杆失败: {result.get('msg')}")

        # 获取当前价格
        self.last_price = self.client.get_current_price(self.config.symbol)
        if self.last_price <= 0:
            self.logger.error("获取价格失败")
            return False

        self.logger.info(f"当前价格: {self.last_price:.2f}")
        return True

    def check_and_open(self) -> bool:
        """
        检查并开仓

        Returns:
            是否成功开仓
        """
        if not self.position.is_empty():
            return False

        # 检查风控
        can_open, reason = self.risk_manager.can_open_position(
            self.last_price, self.base_price)

        if not can_open:
            self.logger.info(f"风控限制: {reason}")
            return False

        # 开仓做多
        return self._open_position()

    def _get_side(self) -> str:
        """获取下单方向"""
        return "buy" if self.config.direction == "long" else "sell"

    def _get_close_side(self) -> str:
        """获取平仓方向"""
        return "sell" if self.config.direction == "long" else "buy"

    def _get_position_side(self) -> PositionSide:
        """获取持仓方向枚举"""
        return PositionSide.LONG if self.config.direction == "long" else PositionSide.SHORT

    def _calculate_price_change(self, old_price: float, new_price: float) -> float:
        """计算价格变化率"""
        if self.config.direction == "long":
            # 做多：价格下跌为负，上涨为正
            return (new_price - old_price) / old_price
        else:
            # 做空：价格上涨为负，下跌为正
            return (old_price - new_price) / old_price

    def _check_dca_condition(self) -> bool:
        """检查加仓条件（根据方向）"""
        if self.position.is_empty():
            return False

        if self.position.dca_count >= self.config.max_dca_count:
            return False

        price_change = self._calculate_price_change(self.last_dca_price, self.last_price)
        return price_change >= self.config.price_interval

    def _check_take_profit_condition(self) -> float:
        """检查止盈条件，返回盈亏比例"""
        if self.position.is_empty():
            return 0.0

        if self.config.direction == "long":
            # 做多：价格上涨止盈
            return (self.last_price - self.position.avg_price) / self.position.avg_price
        else:
            # 做空：价格下跌止盈
            return (self.position.avg_price - self.last_price) / self.position.avg_price

    def _open_position(self) -> bool:
        """
        执行开仓

        Returns:
            是否成功
        """
        side_str = "做多" if self.config.direction == "long" else "做空"
        self.logger.info(f"[OPEN] 开仓{side_str} @ {self.last_price:.2f}")

        # 计算仓位大小
        margin_usdt = self._calculate_position_size(0)   # 保证金 USDT
        raw_lots = self.client.size_margin_to_lots(
            self.config.symbol, margin_usdt, self.last_price, self.config.leverage
        )
        contract_size = self.client.align_sz(self.config.symbol, raw_lots)

        # 下单
        order_id = self.client.place_order(
            symbol=self.config.symbol,
            side=self._get_side(),
            order_type="market",
            size=str(contract_size)
        )

        if order_id:
            self.position.side = self._get_position_side()
            self.position.total_size = contract_size
            self.position.avg_price = self.last_price
            self.position.first_entry_price = self.last_price
            self.position.first_entry_time = time.strftime('%Y-%m-%d %H:%M:%S')
            self.position.entry_prices.append(self.last_price)
            self.position.sizes.append(contract_size)
            self.position.dca_count = 0
            self.position.entry_order_ids.append(order_id)

            self.cycle_state = CycleState.POSITION_OPEN
            self.base_price = self.last_price
            self.last_dca_price = self.last_price

            self.risk_manager.set_entry_price(self.last_price)

            self.recorder.record_trade(
                trade_type='open',
                symbol=self.config.symbol,
                side=self._get_side(),
                size=contract_size,
                price=self.last_price
            )

            self.logger.info(f"开仓成功! 仓位: {contract_size:.4f}, 均价: {self.last_price:.2f}")
            self._log_position_info()
            return True

        # 下单失败：把 OKX 真实错误暴露出来
        last_err = getattr(self.client, 'last_error', None) or \
                   getattr(self.client, '_last_error', None) or \
                   f"size={contract_size:.6f}, price={self.last_price:.2f}"
        self.last_error = f"OKX 下单失败: {last_err}"
        self.logger.error(self.last_error)
        return False

    def check_and_add_position(self) -> bool:
        """
        检查并加仓

        Returns:
            是否成功加仓
        """
        if not self._check_dca_condition():
            return False

        # 执行加仓
        return self._add_position()

    def _add_position(self) -> bool:
        """
        执行加仓

        Returns:
            是否成功
        """
        side_str = "做多" if self.config.direction == "long" else "做空"
        self.logger.info(f"[ADD] 加仓 #{self.position.dca_count + 1} @ {self.last_price:.2f}")

        # 计算新仓位
        margin_usdt = self._calculate_position_size(self.position.dca_count + 1)
        raw_lots = self.client.size_margin_to_lots(
            self.config.symbol, margin_usdt, self.last_price, self.config.leverage
        )
        contract_size = self.client.align_sz(self.config.symbol, raw_lots)

        # 下单
        order_id = self.client.place_order(
            symbol=self.config.symbol,
            side=self._get_side(),
            order_type="market",
            size=str(contract_size)
        )

        if order_id:
            # 更新平均价格
            total_cost = (self.position.total_size * self.position.avg_price +
                         contract_size * self.last_price)
            self.position.total_size += contract_size
            self.position.avg_price = total_cost / self.position.total_size
            self.position.dca_count += 1
            self.position.entry_prices.append(self.last_price)
            self.position.sizes.append(contract_size)
            self.position.entry_order_ids.append(order_id)
            self.last_dca_price = self.last_price

            self.cycle_state = CycleState.DCA_IN_PROGRESS

            self.recorder.record_trade(
                trade_type='add',
                symbol=self.config.symbol,
                side=self._get_side(),
                size=contract_size,
                price=self.last_price
            )

            self.logger.info(f"加仓成功! 累计仓位: {self.position.total_size:.4f}")
            self._log_position_info()
            return True

        return False

    def check_take_profit(self) -> bool:
        """
        检查止盈条件

        Returns:
            是否执行止盈
        """
        if self.position.is_empty():
            return False

        profit_rate = self._check_take_profit_condition()

        if profit_rate >= self.config.take_profit:
            self.logger.info(f"触发止盈! 收益率: {profit_rate * 100:.2f}%")
            return self._close_position(pnl=profit_rate)

        return False

    def check_stop_loss(self) -> bool:
        """
        检查止损条件

        Returns:
            是否执行止损
        """
        if self.position.is_empty():
            return False

        # 检查风控止损
        balance = self.client.get_sim_balance()
        status = self.risk_manager.check_loss_risk(balance)

        if status.emergency_exit:
            self.logger.warning(f"风控触发止损: {status.message}")
            return self._close_position(stop_loss=True)

        # 检查最大加仓次数后的亏损
        if self.position.dca_count >= self.config.max_dca_count:
            loss_rate = self._check_take_profit_condition()
            if loss_rate <= -0.02:  # 累计亏损超过2%
                self.logger.warning(f"最大加仓后仍亏损 {abs(loss_rate) * 100:.2f}%，止损")
                return self._close_position(stop_loss=True)

        return False

    def _close_position(self, pnl: float = 0, stop_loss: bool = False) -> bool:
        """
        平仓

        Args:
            pnl: 盈亏比例
            stop_loss: 是否止损

        Returns:
            是否成功
        """
        if self.position.is_empty():
            return False

        self.logger.info(f"[CLOSE] 平仓 @ {self.last_price:.2f}")

        # self.position.total_size 已经是合约张数（_open_position / _add_position 写入时对齐过 lotSz）
        # 这里再做一次对齐保险，避免浮点漂移或恢复持仓时步长不匹配
        close_size = self.client.align_sz(self.config.symbol, self.position.total_size)

        # 下单平仓
        order_id = self.client.place_order(
            symbol=self.config.symbol,
            side=self._get_close_side(),
            order_type="market",
            size=str(close_size),
            reduce_only=True
        )

        if order_id:
            # 计算盈亏
            if self.position.side == PositionSide.LONG:
                profit = (self.last_price - self.position.avg_price) * self.position.total_size
            elif self.position.side == PositionSide.SHORT:
                profit = (self.position.avg_price - self.last_price) * self.position.total_size
            else:
                profit = 0

            # 扣除手续费 (约0.05%)
            fee = self.position.total_size * self.last_price * 0.0005
            net_profit = profit - fee

            self.recorder.record_trade(
                trade_type='stop_loss' if stop_loss else 'close',
                symbol=self.config.symbol,
                side=self._get_close_side(),
                size=self.position.total_size,
                price=self.last_price,
                pnl=profit,
                fee=fee
            )

            # 更新统计数据
            self.stats['total_cycles'] += 1
            if net_profit > 0:
                self.stats['profit_cycles'] += 1
                self.stats['total_profit'] += net_profit
            else:
                self.stats['loss_cycles'] += 1
                self.stats['total_loss'] += abs(net_profit)

            # 更新余额
            self.client.update_sim_balance(net_profit)

            self.logger.info(f"平仓完成! 盈亏: {net_profit:.2f} USDT")
            self.logger.info(f"累计盈利: {self.stats['total_profit']:.2f} | 累计亏损: {self.stats['total_loss']:.2f}")

            # 重置仓位
            self._reset_position()

            # 如果开启自动循环，延迟后重新开仓
            if self.config.auto_loop and not stop_loss:
                self.logger.info("自动循环模式，5秒后重新开仓...")
                time.sleep(5)

            return True

        return False

    def _calculate_position_size(self, level: int) -> float:
        """
        计算仓位大小

        Args:
            level: 加仓层级

        Returns:
            仓位大小(保证金USDT)
        """
        return self.config.first_order_size * (self.config.multiplier ** level)

    def _reset_position(self):
        """重置仓位状态"""
        self.position = Position()
        self.cycle_state = CycleState.IDLE
        self.last_dca_price = 0.0
        self.pending_orders.clear()

    def _log_position_info(self):
        """记录持仓信息"""
        self.logger.info("-" * 40)
        self.logger.info(f"持仓方向: {self.position.side.value}")
        self.logger.info(f"总仓位: {self.position.total_size:.4f}")
        self.logger.info(f"平均价格: {self.position.avg_price:.2f}")
        self.logger.info(f"加仓次数: {self.position.dca_count}/{self.config.max_dca_count}")
        self.logger.info(f"当前价格: {self.last_price:.2f}")
        if self.position.avg_price > 0:
            pnl_rate = (self.last_price - self.position.avg_price) / self.position.avg_price * 100
            self.logger.info(f"浮动盈亏: {pnl_rate:.2f}%")
        self.logger.info("-" * 40)

    def get_next_dca_price(self) -> float:
        """
        获取下次加仓价格

        Returns:
            加仓价格，0表示不需要加仓
        """
        if self.position.is_empty():
            return 0.0

        if self.position.dca_count >= self.config.max_dca_count:
            return 0.0

        interval = self.config.price_interval
        if self.config.direction == "long":
            # 做多：价格下跌时加仓
            return self.last_dca_price * (1 - interval)
        else:
            # 做空：价格上涨时加仓
            return self.last_dca_price * (1 + interval)

    def get_breakeven_price(self) -> float:
        """
        获取盈亏平衡价格

        Returns:
            盈亏平衡价格
        """
        if self.position.is_empty():
            return 0.0

        # 考虑手续费后的盈亏平衡价
        fee_rate = 0.0005 * 2  # 开仓+平仓
        return self.position.avg_price * (1 + fee_rate)

    def get_target_profit_price(self) -> float:
        """
        获取目标止盈价格

        Returns:
            止盈价格
        """
        if self.position.is_empty():
            return 0.0

        breakeven = self.get_breakeven_price()
        if self.config.direction == "long":
            # 做多：价格上涨止盈
            return breakeven * (1 + self.config.take_profit)
        else:
            # 做空：价格下跌止盈
            return breakeven * (1 - self.config.take_profit)

    def get_status_report(self) -> str:
        """
        获取状态报告

        Returns:
            格式化的状态报告
        """
        lines = [
            "=" * 50,
            "[STATUS] 马丁格尔策略状态",
            "=" * 50,
            f"交易对: {self.config.symbol}",
            f"周期状态: {self.cycle_state.value}",
            "-" * 50,
            f"持仓方向: {self.position.side.value}",
            f"总仓位: {self.position.total_size:.4f}",
            f"平均价格: {self.position.avg_price:.2f}",
            f"加仓次数: {self.position.dca_count}/{self.config.max_dca_count}",
            f"当前价格: {self.last_price:.2f}",
        ]

        if not self.position.is_empty():
            pnl_rate = (self.last_price - self.position.avg_price) / self.position.avg_price * 100
            lines.append(f"浮动盈亏: {pnl_rate:.2f}%")
            lines.append(f"下次加仓价: {self.get_next_dca_price():.2f}")
            lines.append(f"止盈价格: {self.get_target_profit_price():.2f}")
            lines.append(f"盈亏平衡价: {self.get_breakeven_price():.2f}")

        lines.extend([
            "-" * 50,
            f"总交易轮次: {self.stats['total_cycles']}",
            f"盈利轮次: {self.stats['profit_cycles']}",
            f"亏损轮次: {self.stats['loss_cycles']}",
            f"累计盈利: {self.stats['total_profit']:.2f} USDT",
            f"累计亏损: {self.stats['total_loss']:.2f} USDT",
            f"净盈亏: {self.stats['total_profit'] - self.stats['total_loss']:.2f} USDT",
            "=" * 50
        ])

        return "\n".join(lines)

    def run_one_cycle(self) -> bool:
        """
        执行一个完整的交易周期

        Returns:
            周期是否成功完成
        """
        # 更新价格
        self.last_price = self.client.get_current_price(self.config.symbol)

        # 空闲状态：尝试开仓
        if self.cycle_state == CycleState.IDLE:
            return self.check_and_open()

        # 持仓状态：检查止盈、加仓、止损
        if self.cycle_state in [CycleState.POSITION_OPEN, CycleState.DCA_IN_PROGRESS]:
            # 先检查止盈
            if self.check_take_profit():
                return True

            # 再检查加仓
            self.check_and_add_position()

            # 最后检查止损
            self.check_stop_loss()

        return False

    def estimate_required_capital(self) -> Dict[str, Any]:
        """
        估算所需资金（单位：USDT 保证金）。

        注意：_calculate_position_size 返回的就是本层"保证金 USDT"，
        所以累加本身就是首单 ~ 第 N 单需要的总保证金。无需再除以杠杆。
        """
        costs = []
        total_margin = 0.0

        for i in range(self.config.max_dca_count + 1):
            margin = self._calculate_position_size(i)   # 保证金 USDT
            total_margin += margin
            costs.append({
                'level': i,
                'margin': margin,
                'cumulative_margin': total_margin
            })

        return {
            'per_trade_margin': [c['margin'] for c in costs],
            'total_required_margin': total_margin,
            'recommended_capital': total_margin * 1.5,  # 保留50%缓冲
            'max_dca': self.config.max_dca_count,
            'details': costs
        }


class SimulatedMarket:
    """
    模拟市场 - 用于回测和模拟交易
    """

    def __init__(self, initial_price: float = 65000.0,
                 volatility: float = 0.002):
        """
        初始化模拟市场

        Args:
            initial_price: 初始价格
            volatility: 波动率
        """
        self.current_price = initial_price
        self.initial_price = initial_price
        self.volatility = volatility
        self.trend = 0.0  # 趋势偏移
        self.history = [initial_price]

    def set_trend(self, trend: float):
        """设置市场趋势"""
        self.trend = trend

    def tick(self) -> float:
        """
        市场tick

        Returns:
            新价格
        """
        import random
        # 随机波动
        change = random.gauss(self.trend, self.volatility)
        self.current_price *= (1 + change)
        self.current_price = max(self.current_price, 100)  # 防止负价格
        self.history.append(self.current_price)
        return self.current_price

    def get_price(self) -> float:
        """获取当前价格"""
        return self.current_price

    def reset(self, price: float = None):
        """重置市场"""
        self.current_price = price or self.initial_price
        self.history = [self.current_price]
        self.trend = 0.0

    def simulate_drop(self, drop_percent: float = 0.05):
        """
        模拟下跌

        Args:
            drop_percent: 下跌百分比
        """
        self.current_price *= (1 - drop_percent)

    def simulate_rally(self, rally_percent: float = 0.03):
        """
        模拟反弹

        Args:
            rally_percent: 反弹百分比
        """
        self.current_price *= (1 + rally_percent)


def run_backtest(config: MartingaleConfig = None,
                 days: int = 30,
                 initial_capital: float = 2000.0) -> Dict[str, Any]:
    """
    回测策略

    Args:
        config: 策略配置
        days: 回测天数
        initial_capital: 初始资金

    Returns:
        回测结果
    """
    import random

    if config is None:
        config = MartingaleConfig()

    # 创建模拟组件
    client = OKXClient("", "", "", simulation=True)
    client.reset_sim_balance(initial_capital)

    risk_config = RiskConfig(max_loss_rate=0.3)
    risk_manager = RiskManager(risk_config, initial_capital)

    strategy = MartingaleStrategy(client, risk_manager, config)

    # 创建模拟市场
    market = SimulatedMarket(initial_price=65000.0, volatility=0.003)

    results = {
        'trades': [],
        'equity_curve': [],
        'final_balance': 0.0,
        'total_trades': 0,
        'profit_trades': 0,
        'loss_trades': 0,
        'max_drawdown': 0.0
    }

    strategy.initialize()

    # 模拟每天的走势
    ticks_per_day = 1440  # 每分钟tick
    total_ticks = days * ticks_per_day

    peak_balance = initial_capital

    for i in range(total_ticks):
        # 每小时模拟一次趋势变化
        if i % 60 == 0:
            hour = (i // 60) % 24
            # 模拟日内波动
            if 2 <= hour <= 6:  # 凌晨低波动
                market.set_trend(-0.0005)
            elif 18 <= hour <= 22:  # 晚间高波动
                market.set_trend(random.uniform(-0.001, 0.001))
            else:
                market.set_trend(random.uniform(-0.0002, 0.0002))

        # 更新价格
        new_price = market.tick()
        client._sim_last_price = new_price  # 注入模拟价格

        # 运行策略
        strategy.last_price = new_price
        strategy.run_one_cycle()

        # 记录权益
        balance = client.get_sim_balance()
        results['equity_curve'].append(balance)
        peak_balance = max(peak_balance, balance)

        # 计算最大回撤
        drawdown = (peak_balance - balance) / peak_balance
        results['max_drawdown'] = max(results['max_drawdown'], drawdown)

        # 每天模拟一次下跌+反弹
        if i > 0 and i % (ticks_per_day // 4) == 0:
            # 模拟小跌
            if random.random() < 0.3:
                market.simulate_drop(random.uniform(0.005, 0.015))
            # 模拟小涨
            if random.random() < 0.3:
                market.simulate_rally(random.uniform(0.005, 0.02))

    # 汇总结果
    summary = strategy.stats
    results.update({
        'final_balance': client.get_sim_balance(),
        'total_trades': summary['total_cycles'],
        'profit_trades': summary['profit_cycles'],
        'loss_trades': summary['loss_cycles'],
        'total_profit': summary['total_profit'],
        'total_loss': summary['total_loss'],
        'net_profit': summary['total_profit'] - summary['total_loss'],
        'profit_rate': (client.get_sim_balance() - initial_capital) / initial_capital
    })

    return results
