"""
OKX 期货马丁格尔策略 - 主程序入口
支持模拟交易和实盘交易
"""

import sys
import time
import logging
import signal
import yaml
from typing import Optional
from datetime import datetime

from okx_client import OKXClient
from risk_manager import RiskManager, RiskConfig, RiskLevel
from martingale import MartingaleStrategy, MartingaleConfig, SimulatedMarket


class TradingBot:
    """交易机器人主类"""

    def __init__(self, config_path: str = "config.yaml"):
        """
        初始化交易机器人

        Args:
            config_path: 配置文件路径
        """
        self.config = self._load_config(config_path)
        self.running = False
        self.strategy: Optional[MartingaleStrategy] = None
        self.client: Optional[OKXClient] = None
        self.risk_manager: Optional[RiskManager] = None

        # 设置日志
        self._setup_logging()

        # 注册信号处理
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _load_config(self, config_path: str) -> dict:
        """加载配置文件"""
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        except FileNotFoundError:
            logging.warning(f"配置文件 {config_path} 不存在，使用默认配置")
            return self._get_default_config()

    def _get_default_config(self) -> dict:
        """获取默认配置"""
        return {
            'API_KEY': '',
            'SECRET_KEY': '',
            'PASSPHRASE': '',
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
            'CHECK_INTERVAL': 2,
            'AUTO_LOOP': False,
            'LOG_LEVEL': 'INFO'
        }

    def _setup_logging(self):
        """设置日志"""
        log_level = self.config.get('LOG_LEVEL', 'INFO')
        logging.basicConfig(
            level=getattr(logging, log_level),
            format='%(asctime)s [%(levelname)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        self.logger = logging.getLogger("TradingBot")

    def _signal_handler(self, signum, frame):
        """信号处理器"""
        self.logger.info("\n收到停止信号，正在关闭...")
        self.stop()

    def initialize(self) -> bool:
        """
        初始化所有组件

        Returns:
            是否初始化成功
        """
        self.logger.info("=" * 60)
        self.logger.info("[START] 初始化交易机器人")
        self.logger.info("=" * 60)

        # 创建 OKX 客户端
        self.client = OKXClient(
            api_key=self.config.get('API_KEY', ''),
            secret_key=self.config.get('SECRET_KEY', ''),
            passphrase=self.config.get('PASSPHRASE', ''),
            simulation=self.config.get('SIMULATION', True)
        )

        if self.config.get('SIMULATION', True):
            self.client.reset_sim_balance(self.config.get('TOTAL_CAPITAL', 2000))
            self.logger.info("🔧 运行模式: 模拟交易")
        else:
            self.logger.info("🔴 运行模式: 实盘交易")

        # 创建风控配置
        risk_config = RiskConfig(
            max_loss_rate=self.config.get('MAX_LOSS_RATE', 0.25),
            max_drawdown=0.30,
            trend_pause_rate=self.config.get('TREND_PAUSE_RATE', 0.05),
            min_balance=self.config.get('TOTAL_CAPITAL', 2000) * 0.2
        )

        # 创建风控管理器
        self.risk_manager = RiskManager(
            config=risk_config,
            initial_balance=self.config.get('TOTAL_CAPITAL', 2000)
        )

        # 创建策略配置
        strategy_config = MartingaleConfig(
            leverage=self.config.get('LEVERAGE', 2),
            first_order_size=self.config.get('FIRST_ORDER_SIZE', 10),
            multiplier=self.config.get('MULTIPLIER', 1.5),
            price_interval=self.config.get('PRICE_INTERVAL', 0.008),
            max_dca_count=self.config.get('MAX_DCA_COUNT', 7),
            take_profit=self.config.get('TAKE_PROFIT', 0.02),
            symbol=self.config.get('SYMBOL', 'BTC-USDT-SWAP'),
            auto_loop=self.config.get('AUTO_LOOP', False)
        )

        # 创建策略
        self.strategy = MartingaleStrategy(
            client=self.client,
            risk_manager=self.risk_manager,
            config=strategy_config
        )

        # 初始化策略
        if not self.strategy.initialize():
            self.logger.error("策略初始化失败")
            return False

        self.logger.info("[OK] 初始化完成")
        return True

    def run(self):
        """运行交易机器人"""
        if not self.initialize():
            self.logger.error("初始化失败，程序退出")
            return

        self.running = True
        check_interval = self.config.get('CHECK_INTERVAL', 2)

        self.logger.info("=" * 60)
        self.logger.info("[RUN] 开始运行交易机器人")
        self.logger.info(f"检查间隔: {check_interval}秒")
        self.logger.info("按 Ctrl+C 停止")
        self.logger.info("=" * 60)

        last_status_time = time.time()

        try:
            while self.running:
                # 更新当前价格
                current_price = self.client.get_current_price(self.strategy.config.symbol)
                self.strategy.last_price = current_price

                # 执行策略
                self.strategy.run_one_cycle()

                # 检查风控
                balance = self.client.get_sim_balance()
                risk_status = self.risk_manager.check_loss_risk(balance)

                if risk_status.emergency_exit:
                    self.logger.warning(f"[WARN] 风控触发: {risk_status.message}")
                    self.strategy._close_position(stop_loss=True)
                    if self.strategy.config.auto_loop:
                        self.risk_manager.reset(balance)
                        self.strategy.stats['start_time'] = time.time()

                # 每30秒打印状态
                if time.time() - last_status_time >= 30:
                    self._print_status(current_price, balance, risk_status)
                    last_status_time = time.time()

                time.sleep(check_interval)

        except Exception as e:
            self.logger.error(f"运行错误: {e}")
        finally:
            self.stop()

    def _print_status(self, price: float, balance: float, risk_status):
        """打印状态信息"""
        pos = self.strategy.position
        self.logger.info("-" * 50)
        self.logger.info(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.logger.info(f"BTC价格: ${price:.2f}")
        self.logger.info(f"账户余额: {balance:.2f} USDT")

        if not pos.is_empty():
            pnl_rate = (price - pos.avg_price) / pos.avg_price * 100
            self.logger.info(f"持仓: 多单 {pos.total_size:.4f} @ ${pos.avg_price:.2f}")
            self.logger.info(f"浮盈: {pnl_rate:.2f}%")
            self.logger.info(f"加仓: {pos.dca_count}/{self.strategy.config.max_dca_count}")
            self.logger.info(f"下次加仓价: ${self.strategy.get_next_dca_price():.2f}")
            self.logger.info(f"目标止盈价: ${self.strategy.get_target_profit_price():.2f}")
        else:
            self.logger.info("状态: 空仓等待")

        self.logger.info(f"风控: {risk_status.message}")
        self.logger.info("-" * 50)

    def stop(self):
        """停止交易机器人"""
        self.running = False
        self.logger.info("正在停止交易机器人...")

        # 如果有持仓，平仓
        if self.strategy and not self.strategy.position.is_empty():
            self.logger.info("平掉所有持仓...")
            self.strategy._close_position()

        # 打印最终统计
        if self.strategy:
            self.logger.info("\n" + self.strategy.get_status_report())

        self.logger.info("交易机器人已停止")

    def run_simulation(self, duration: int = 60, simulate_price: float = 65000):
        """
        运行模拟交易

        Args:
            duration: 模拟时长(秒)
            simulate_price: 初始模拟价格
        """
        if not self.initialize():
            return

        self.logger.info(f"开始模拟交易，持续 {duration} 秒...")

        market = SimulatedMarket(initial_price=simulate_price, volatility=0.002)
        check_interval = self.config.get('CHECK_INTERVAL', 1)
        start_time = time.time()

        try:
            while time.time() - start_time < duration and self.running:
                # 模拟市场tick
                new_price = market.tick()
                self.strategy.last_price = new_price

                # 模拟偶尔的价格波动
                if len(self.strategy.stats) > 0 and self.strategy.position.is_empty():
                    # 空仓时偶尔模拟下跌触发开仓后反弹
                    if new_price < simulate_price * 0.98:
                        market.simulate_rally(0.02)

                # 运行策略
                self.strategy.run_one_cycle()

                # 检查风控
                balance = self.client.get_sim_balance()
                risk_status = self.risk_manager.check_loss_risk(balance)

                if risk_status.emergency_exit:
                    self.strategy._close_position(stop_loss=True)

                # 打印状态
                if self.strategy.position.dca_count > 0 or self.strategy.stats['total_cycles'] > 0:
                    self._print_status(new_price, balance, risk_status)

                time.sleep(check_interval)

        except Exception as e:
            self.logger.error(f"模拟错误: {e}")
        finally:
            self.stop()

    def run_interactive(self):
        """运行交互模式"""
        if not self.initialize():
            return

        self.logger.info("=" * 60)
        self.logger.info("[MODE] 交互模式")
        self.logger.info("=" * 60)
        self.logger.info("可用命令:")
        self.logger.info("  status  - 显示状态")
        self.logger.info("  open    - 开仓")
        self.logger.info("  close   - 平仓")
        self.logger.info("  add     - 加仓")
        self.logger.info("  profit  - 设置止盈")
        self.logger.info("  info    - 显示策略信息")
        self.logger.info("  sim     - 模拟价格变动")
        self.logger.info("  help    - 显示帮助")
        self.logger.info("  quit    - 退出")
        self.logger.info("=" * 60)

        market = SimulatedMarket(initial_price=65000.0, volatility=0.001)

        while True:
            try:
                cmd = input("\n> ").strip().lower()

                if cmd == 'quit':
                    break
                elif cmd == 'help':
                    self._print_help()
                elif cmd == 'status':
                    print(self.strategy.get_status_report())
                elif cmd == 'info':
                    self._print_strategy_info()
                elif cmd == 'open':
                    if self.strategy.position.is_empty():
                        self.strategy.last_price = market.get_price()
                        if self.strategy.check_and_open():
                            print("[OK] 开仓成功")
                        else:
                            print("[FAIL] 开仓失败")
                    else:
                        print("[WARN] 已有持仓")
                elif cmd == 'close':
                    if not self.strategy.position.is_empty():
                        self.strategy.last_price = market.get_price()
                        if self.strategy._close_position():
                            print("[OK] 平仓成功")
                        else:
                            print("[FAIL] 平仓失败")
                    else:
                        print("[WARN] 无持仓")
                elif cmd == 'add':
                    if not self.strategy.position.is_empty():
                        self.strategy.last_price = market.get_price()
                        if self.strategy.check_and_add_position():
                            print("[OK] 加仓成功")
                        else:
                            print("[FAIL] 加仓条件未满足")
                    else:
                        print("[WARN] 无持仓")
                elif cmd == 'sim':
                    self._handle_sim_command(market)
                elif cmd == 'balance':
                    print(f"余额: {self.client.get_sim_balance():.2f} USDT")
                else:
                    print(f"未知命令: {cmd}")

            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"错误: {e}")

        self.stop()

    def _print_help(self):
        """打印帮助信息"""
        help_text = """
马丁格尔策略帮助:
-----------------
1. 策略原理:
   - 在价格下跌时分批买入做多
   - 每次下跌0.8%加仓一次
   - 价格上涨2%时止盈
   - 亏损超过25%时止损

2. 仓位计算:
   - 首单: 10 USDT
   - 加仓: 10 * 1.5^n (n=加仓次数)
   - 7次加仓后总保证金约493 USDT

3. 风险控制:
   - 杠杆: 2倍
   - 最大加仓: 7次
   - 硬止损: 25%
   - 趋势暂停: 上涨5%不开新仓
"""
        print(help_text)

    def _print_strategy_info(self):
        """打印策略信息"""
        config = self.strategy.config
        self.logger.info("策略配置:")
        self.logger.info(f"  交易对: {config.symbol}")
        self.logger.info(f"  杠杆: {config.leverage}x")
        self.logger.info(f"  首单: {config.first_order_size} USDT")
        self.logger.info(f"  加仓倍数: {config.multiplier}")
        self.logger.info(f"  价格间隔: {config.price_interval * 100}%")
        self.logger.info(f"  最大加仓: {config.max_dca_count}次")
        self.logger.info(f"  止盈目标: {config.take_profit * 100}%")

        # 资金估算
        estimate = self.strategy.estimate_required_capital()
        self.logger.info("资金需求:")
        self.logger.info(f"  单次最大保证金: {estimate['total_required_margin']:.2f} USDT")
        self.logger.info(f"  建议保留资金: {estimate['recommended_capital']:.2f} USDT")

    def _handle_sim_command(self, market: SimulatedMarket):
        """处理模拟命令"""
        print("模拟价格操作:")
        print("  1. 小跌 (0.5%)")
        print("  2. 中跌 (1%)")
        print("  3. 大跌 (2%)")
        print("  4. 小涨 (0.5%)")
        print("  5. 中涨 (1%)")
        print("  6. 大涨 (2%)")
        print("  7. 随机波动")

        try:
            choice = input("选择: ").strip()
            if choice == '1':
                market.simulate_drop(0.005)
            elif choice == '2':
                market.simulate_drop(0.01)
            elif choice == '3':
                market.simulate_drop(0.02)
            elif choice == '4':
                market.simulate_rally(0.005)
            elif choice == '5':
                market.simulate_rally(0.01)
            elif choice == '6':
                market.simulate_rally(0.02)
            elif choice == '7':
                import random
                if random.random() < 0.5:
                    market.tick()
                else:
                    market.simulate_drop(random.uniform(0.005, 0.02))

            print(f"新价格: ${market.get_price():.2f}")
            self.strategy.last_price = market.get_price()
        except Exception as e:
            print(f"错误: {e}")


def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description='OKX期货马丁格尔策略')
    parser.add_argument('-c', '--config', default='config.yaml',
                       help='配置文件路径')
    parser.add_argument('-m', '--mode', choices=['live', 'sim', 'backtest', 'interactive'],
                       default='interactive',
                       help='运行模式: live(实盘), sim(模拟), backtest(回测), interactive(交互)')
    parser.add_argument('-s', '--symbol', default='BTC-USDT-SWAP',
                       help='交易对')
    parser.add_argument('--duration', type=int, default=60,
                       help='模拟运行时间(秒)')

    args = parser.parse_args()

    bot = TradingBot(args.config)

    # 更新配置
    if args.symbol:
        bot.config['SYMBOL'] = args.symbol

    if args.mode == 'live':
        bot.run()
    elif args.mode == 'sim':
        bot.run_simulation(duration=args.duration)
    elif args.mode == 'backtest':
        from martingale import run_backtest, MartingaleConfig
        import random

        # 默认回测配置
        config = MartingaleConfig(
            symbol=args.symbol,
            leverage=2,
            first_order_size=10,
            multiplier=1.5,
            price_interval=0.008,
            max_dca_count=7,
            take_profit=0.02
        )

        print("开始回测...")
        results = run_backtest(config=config, days=30, initial_capital=2000)

        print("\n" + "=" * 50)
        print("[BACKTEST] 回测结果")
        print("=" * 50)
        print(f"初始资金: 2000 USDT")
        print(f"最终资金: {results['final_balance']:.2f} USDT")
        print(f"收益率: {results['profit_rate']*100:.2f}%")
        print(f"总交易轮次: {results['total_trades']}")
        print(f"盈利轮次: {results['profit_trades']}")
        print(f"亏损轮次: {results['loss_trades']}")
        print(f"最大回撤: {results['max_drawdown']*100:.2f}%")
        print(f"累计盈利: {results['total_profit']:.2f}")
        print(f"累计亏损: {results['total_loss']:.2f}")
        print("=" * 50)

    else:  # interactive
        bot.run_interactive()


if __name__ == "__main__":
    main()
