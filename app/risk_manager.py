"""
风险管理系统
负责监控和管理交易风险
"""

import time
from typing import Optional, Dict, Any
from dataclasses import dataclass, field
from enum import Enum


class RiskLevel(Enum):
    """风险等级"""
    SAFE = "safe"
    WARNING = "warning"
    DANGER = "danger"
    CRITICAL = "critical"


@dataclass
class RiskConfig:
    """风控配置"""
    max_loss_rate: float = 0.25  # 最大亏损比例 (25%)
    max_drawdown: float = 0.30   # 最大回撤 (30%)
    trend_pause_rate: float = 0.05  # 趋势暂停阈值 (5%)
    min_balance: float = 500.0   # 最小保留余额
    emergency_exit: bool = False  # 紧急退出标志


@dataclass
class RiskStatus:
    """风险状态"""
    level: RiskLevel = RiskLevel.SAFE
    current_loss_rate: float = 0.0
    current_drawdown: float = 0.0
    is_trend_blocked: bool = False
    total_loss: float = 0.0
    peak_balance: float = 2000.0
    current_balance: float = 2000.0
    message: str = ""
    emergency_exit: bool = False


class RiskManager:
    """
    风险管理系统

    职责：
    1. 监控账户整体亏损
    2. 检测趋势风险
    3. 管理紧急止损
    4. 保护本金安全
    """

    def __init__(self, config: RiskConfig, initial_balance: float = 2000.0):
        """
        初始化风控系统

        Args:
            config: 风控配置
            initial_balance: 初始账户余额
        """
        self.config = config
        self.initial_balance = initial_balance
        self.peak_balance = initial_balance
        self.entry_price: Optional[float] = None  # 入场价格
        self.last_check_time = time.time()

    def set_entry_price(self, price: float):
        """设置入场价格"""
        self.entry_price = price

    def check_trend_risk(self, current_price: float,
                         base_price: float,
                         direction: str = "long") -> bool:
        """
        检查趋势风险

        Args:
            current_price: 当前价格
            base_price: 参考基准价格
            direction: 策略方向 'long' / 'short'。做空策略下，价格大幅下跌才算趋势风险

        Returns:
            True 如果趋势危险，需要暂停开仓
        """
        if base_price <= 0:
            return False

        # 做空策略下，价格下跌才是"危险的反向趋势"，与做多相反
        if direction == "short":
            price_change = (base_price - current_price) / base_price
        else:
            price_change = (current_price - base_price) / base_price

        # 如果反向变动超过阈值，标记为趋势风险
        if price_change >= self.config.trend_pause_rate:
            return True

        return False

    def check_loss_risk(self, current_balance: float,
                       unrealized_pnl: float = 0.0) -> RiskStatus:
        """
        检查亏损风险

        Args:
            current_balance: 当前账户余额
            unrealized_pnl: 未实现盈亏

        Returns:
            风险状态
        """
        status = RiskStatus()
        status.current_balance = current_balance
        self.peak_balance = max(self.peak_balance, current_balance)

        # 计算总资产（含未实现盈亏）
        total_assets = current_balance + unrealized_pnl

        # 计算亏损率
        status.current_loss_rate = max(0,
            (self.initial_balance - total_assets) / self.initial_balance)

        # 计算回撤
        if self.peak_balance > 0:
            status.current_drawdown = max(0,
                (self.peak_balance - total_assets) / self.peak_balance)

        status.total_loss = self.initial_balance - total_assets

        # 确定风险等级
        if status.current_loss_rate >= self.config.max_loss_rate:
            status.level = RiskLevel.CRITICAL
            status.emergency_exit = True
            status.message = f"达到硬止损线！亏损 {status.current_loss_rate*100:.2f}%，触发强制平仓"
        elif status.current_loss_rate >= self.config.max_loss_rate * 0.8:
            status.level = RiskLevel.DANGER
            status.message = f"亏损严重 {status.current_loss_rate*100:.2f}%，注意风险"
        elif status.current_loss_rate >= self.config.max_loss_rate * 0.5:
            status.level = RiskLevel.WARNING
            status.message = f"亏损 {status.current_loss_rate*100:.2f}%，保持警惕"
        else:
            status.level = RiskLevel.SAFE
            status.message = f"账户正常，亏损 {status.current_loss_rate*100:.2f}%"

        # 后面三项 CRITICAL 触发条件：只在当前等级还不够严重时升级并覆盖 message，
        # 否则保留前一段更具体的信息（如"硬止损线"）。
        def _upgrade_to_critical(level, msg):
            # 用枚举值的"严重度"做比较：SAFE < WARNING < DANGER < CRITICAL
            order = {RiskLevel.SAFE: 0, RiskLevel.WARNING: 1, RiskLevel.DANGER: 2, RiskLevel.CRITICAL: 3}
            if order[level] < order[RiskLevel.CRITICAL]:
                status.level = RiskLevel.CRITICAL
                status.emergency_exit = True
                status.message = msg

        # 检查回撤风险
        if status.current_drawdown >= self.config.max_drawdown:
            _upgrade_to_critical(
                status.level,
                f"回撤达到 {status.current_drawdown*100:.2f}%，触发强制平仓"
            )

        # 检查余额是否低于最低要求
        if current_balance < self.config.min_balance:
            _upgrade_to_critical(
                status.level,
                f"余额 {current_balance:.2f} 低于最低要求 {self.config.min_balance}"
            )

        status.emergency_exit = status.emergency_exit or self.config.emergency_exit

        return status

    def can_open_position(self, current_price: float,
                          reference_price: float,
                          direction: str = "long") -> tuple[bool, str]:
        """
        检查是否可以开仓

        Args:
            current_price: 当前价格
            reference_price: 参考价格
            direction: 策略方向 'long' / 'short'

        Returns:
            (是否可以开仓, 原因)
        """
        # 检查趋势风险
        if self.check_trend_risk(current_price, reference_price, direction):
            return False, f"价格反向变动超过 {self.config.trend_pause_rate*100}%，趋势风险暂停开仓"

        # 检查紧急退出标志
        if self.config.emergency_exit:
            return False, "系统处于紧急退出状态，禁止开仓"

        return True, "可以开仓"

    def should_take_profit(self, current_price: float,
                           avg_price: float,
                           direction: str,
                           profit_target: float) -> bool:
        """
        检查是否应该止盈

        Args:
            current_price: 当前价格
            avg_price: 平均持仓价格
            direction: 持仓方向 (long/short)
            profit_target: 止盈目标比例

        Returns:
            是否应该止盈
        """
        if avg_price <= 0:
            return False

        if direction == "long":
            # 做多：价格上涨超过目标则止盈
            profit_rate = (current_price - avg_price) / avg_price
            return profit_rate >= profit_target
        else:
            # 做空：价格下跌超过目标则止盈
            profit_rate = (avg_price - current_price) / avg_price
            return profit_rate >= profit_target

    def should_add_position(self, current_price: float,
                            last_entry_price: float,
                            direction: str,
                            interval: float) -> bool:
        """
        检查是否应该加仓

        Args:
            current_price: 当前价格
            last_entry_price: 上次入场价格
            direction: 持仓方向
            interval: 补仓间隔

        Returns:
            是否应该加仓
        """
        if last_entry_price <= 0:
            return False

        if direction == "long":
            # 做多：价格下跌超过间隔则加仓
            drop_rate = (last_entry_price - current_price) / last_entry_price
            return drop_rate >= interval
        else:
            # 做空：价格上涨超过间隔则加仓
            rise_rate = (current_price - last_entry_price) / last_entry_price
            return rise_rate >= interval

    def calculate_position_size(self, base_size: float,
                                multiplier: float,
                                dca_count: int) -> float:
        """
        计算加仓仓位大小

        Args:
            base_size: 基础仓位
            multiplier: 加仓倍数
            dca_count: 当前加仓次数

        Returns:
            新的仓位大小
        """
        return base_size * (multiplier ** dca_count)

    def estimate_total_cost(self, base_size: float,
                            multiplier: float,
                            max_dca: int,
                            current_price: float,
                            leverage: float) -> Dict[str, float]:
        """
        估算最大总投入

        Args:
            base_size: 基础仓位
            multiplier: 加仓倍数
            max_dca: 最大加仓次数
            current_price: 当前价格
            leverage: 杠杆倍数

        Returns:
            估算数据字典
        """
        total_cost = 0.0
        costs_by_level = []

        for i in range(max_dca + 1):
            size = base_size * (multiplier ** i)
            cost = size / leverage  # 保证金 = 仓位 / 杠杆
            total_cost += cost
            costs_by_level.append({
                'level': i,
                'size': size,
                'margin': cost,
                'cumulative': total_cost
            })

        return {
            'total_margin': total_cost,
            'costs_by_level': costs_by_level,
            'available_balance': self.initial_balance - total_cost,
            'utilization_rate': total_cost / self.initial_balance
        }

    def reset(self, new_balance: float = None):
        """
        重置风控状态

        Args:
            new_balance: 新初始余额
        """
        if new_balance:
            self.initial_balance = new_balance
            self.peak_balance = new_balance
        self.entry_price = None
        self.config.emergency_exit = False

    def get_risk_report(self, status: RiskStatus) -> str:
        """
        生成风险报告

        Args:
            status: 风险状态

        Returns:
            格式化的风险报告
        """
        report_lines = [
            "=" * 50,
            "[RISK] 风险状态报告",
            "=" * 50,
            f"风险等级: {status.level.value.upper()}",
            f"当前余额: {status.current_balance:.2f} USDT",
            f"初始余额: {self.initial_balance:.2f} USDT",
            f"峰值余额: {status.peak_balance:.2f} USDT",
            f"当前亏损: {status.total_loss:.2f} USDT ({status.current_loss_rate*100:.2f}%)",
            f"最大回撤: {status.current_drawdown*100:.2f}%",
            f"趋势状态: {'暂停开仓' if status.is_trend_blocked else '正常'}",
            f"紧急退出: {'是' if status.emergency_exit else '否'}",
            "-" * 50,
            f"提示: {status.message}",
            "=" * 50
        ]
        return "\n".join(report_lines)


class TradeRecorder:
    """
    交易记录器
    记录所有交易操作和盈亏情况
    """

    def __init__(self):
        self.trades: list[Dict[str, Any]] = []
        self.daily_stats: Dict[str, Dict] = {}
        self.cycle_stats: Dict[str, Any] = {
            'cycle_count': 0,
            'total_profit': 0.0,
            'total_loss': 0.0,
            'win_count': 0,
            'loss_count': 0,
            'max_consecutive_loss': 0
        }

    def record_trade(self, trade_type: str, symbol: str,
                    side: str, size: float, price: float,
                    pnl: float = 0.0, fee: float = 0.0):
        """
        记录一笔交易（内存 + 数据库双写）

        Args:
            trade_type: 交易类型 (open, add, close, take_profit, stop_loss)
            symbol: 交易对
            side: 买卖方向
            size: 数量
            price: 价格
            pnl: 盈亏
            fee: 手续费
        """
        trade = {
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'type': trade_type,
            'symbol': symbol,
            'side': side,
            'size': size,
            'price': price,
            'pnl': pnl,
            'fee': fee,
            'net_pnl': pnl - fee
        }
        self.trades.append(trade)
        self._update_stats(trade)

        # 同时写入数据库（延迟导入避免循环依赖）
        try:
            from database import get_db
            db = get_db()
            db.add_trade(
                trade_type=trade_type,
                symbol=symbol,
                side=side,
                size=size,
                price=price,
                pnl=pnl,
                fee=fee
            )
        except Exception:
            # 数据库写入失败不影响主流程
            pass

    def _update_stats(self, trade: Dict):
        """更新统计数据"""
        if trade['type'] == 'close':
            self.cycle_stats['cycle_count'] += 1

            if trade['net_pnl'] > 0:
                self.cycle_stats['total_profit'] += trade['net_pnl']
                self.cycle_stats['win_count'] += 1
            else:
                self.cycle_stats['total_loss'] += abs(trade['net_pnl'])
                self.cycle_stats['loss_count'] += 1

        # 更新日统计
        date = time.strftime('%Y-%m-%d')
        if date not in self.daily_stats:
            self.daily_stats[date] = {
                'trade_count': 0,
                'profit': 0.0,
                'loss': 0.0,
                'fee': 0.0
            }

        stats = self.daily_stats[date]
        stats['trade_count'] += 1
        if trade['net_pnl'] > 0:
            stats['profit'] += trade['net_pnl']
        else:
            stats['loss'] += abs(trade['net_pnl'])
        stats['fee'] += trade['fee']

    def get_summary(self) -> Dict[str, Any]:
        """获取交易摘要"""
        total_trades = len(self.trades)
        closed_trades = sum(1 for t in self.trades if t['type'] == 'close')

        return {
            'total_trades': total_trades,
            'closed_trades': closed_trades,
            'win_rate': self.cycle_stats['win_count'] / closed_trades if closed_trades > 0 else 0,
            'total_profit': self.cycle_stats['total_profit'],
            'total_loss': self.cycle_stats['total_loss'],
            'net_profit': self.cycle_stats['total_profit'] - self.cycle_stats['total_loss'],
            'cycles': self.cycle_stats['cycle_count']
        }

    def get_daily_report(self, date: str = None) -> str:
        """获取日报"""
        if date is None:
            date = time.strftime('%Y-%m-%d')

        if date not in self.daily_stats:
            return f"{date} 无交易记录"

        stats = self.daily_stats[date]
        net = stats['profit'] - stats['loss'] - stats['fee']

        return f"""
{'='*40}
[DAILY] {date} 日报
{'='*40}
交易次数: {stats['trade_count']}
盈利: {stats['profit']:.2f} USDT
亏损: {stats['loss']:.2f} USDT
手续费: {stats['fee']:.2f} USDT
净盈亏: {net:.2f} USDT
{'='*40}
"""
