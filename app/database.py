"""
数据库持久化模块
用于存储配置、交易记录和状态
"""

import sqlite3
import json
import os
from typing import Optional, Dict, Any, List
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager


class Database:
    """SQLite数据库管理器"""

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = os.getenv('DATABASE_PATH', '/data/btc_future.db')
        self.db_path = db_path
        self._ensure_dir()
        self._init_db()

    def _ensure_dir(self):
        """确保目录存在"""
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

    @contextmanager
    def _get_conn(self):
        """获取数据库连接"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        """初始化数据库表"""
        with self._get_conn() as conn:
            cursor = conn.cursor()

            # 配置表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            ''')

            # 交易记录表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_type TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    size REAL NOT NULL,
                    price REAL NOT NULL,
                    pnl REAL,
                    fee REAL,
                    created_at TEXT NOT NULL
                )
            ''')

            # 状态表（用于存储当前持仓等状态）
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            ''')

            # 余额表（模拟账户余额）
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS balance (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    balance REAL NOT NULL,
                    initial_balance REAL NOT NULL,
                    updated_at TEXT NOT NULL
                )
            ''')

    # ============ 配置管理 ============

    def get_config(self, key: str, default: Any = None) -> Any:
        """获取配置"""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT value FROM config WHERE key = ?', (key,))
            row = cursor.fetchone()
            if row:
                return json.loads(row['value'])
            return default

    def set_config(self, key: str, value: Any):
        """设置配置"""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO config (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?
            ''', (key, json.dumps(value), datetime.now().isoformat(),
                  json.dumps(value), datetime.now().isoformat()))

    def get_all_config(self) -> Dict[str, Any]:
        """获取所有配置"""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT key, value FROM config')
            return {row['key']: json.loads(row['value']) for row in cursor.fetchall()}

    def set_all_config(self, config: Dict[str, Any]):
        """批量设置配置"""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            now = datetime.now().isoformat()
            for key, value in config.items():
                cursor.execute('''
                    INSERT INTO config (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?
                ''', (key, json.dumps(value), now, json.dumps(value), now))

    # ============ 交易记录 ============

    def add_trade(self, trade_type: str, symbol: str, side: str,
                  size: float, price: float, pnl: float = None,
                  fee: float = None) -> int:
        """添加交易记录"""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO trades (trade_type, symbol, side, size, price, pnl, fee, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (trade_type, symbol, side, size, price, pnl, fee, datetime.now().isoformat()))
            return cursor.lastrowid

    def get_trades(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        """获取交易记录"""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM trades ORDER BY id DESC LIMIT ? OFFSET ?
            ''', (limit, offset))
            return [dict(row) for row in cursor.fetchall()]

    def get_trades_summary(self) -> Dict[str, Any]:
        """获取交易汇总"""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as profit_trades,
                    SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as loss_trades,
                    COALESCE(SUM(pnl), 0) as total_pnl,
                    COALESCE(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 0) as total_profit,
                    COALESCE(SUM(CASE WHEN pnl < 0 THEN ABS(pnl) ELSE 0 END), 0) as total_loss
                FROM trades
                WHERE pnl IS NOT NULL
            ''')
            row = cursor.fetchone()
            return dict(row) if row else {}

    def clear_trades(self):
        """清空交易记录"""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM trades')

    # ============ 状态管理 ============

    def get_state(self, key: str, default: Any = None) -> Any:
        """获取状态"""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT value FROM state WHERE key = ?', (key,))
            row = cursor.fetchone()
            if row:
                return json.loads(row['value'])
            return default

    def set_state(self, key: str, value: Any):
        """设置状态"""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?
            ''', (key, json.dumps(value), datetime.now().isoformat(),
                  json.dumps(value), datetime.now().isoformat()))

    # ============ 余额管理 ============

    def get_balance(self) -> Optional[float]:
        """获取余额"""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT balance FROM balance WHERE id = 1')
            row = cursor.fetchone()
            return row['balance'] if row else None

    def set_balance(self, balance: float, initial_balance: float = None):
        """设置余额"""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            if initial_balance is None:
                cursor.execute('SELECT initial_balance FROM balance WHERE id = 1')
                row = cursor.fetchone()
                initial_balance = row['initial_balance'] if row else 2000.0
            cursor.execute('''
                INSERT INTO balance (id, balance, initial_balance, updated_at)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET balance = ?, updated_at = ?
            ''', (balance, initial_balance, datetime.now().isoformat(),
                  balance, datetime.now().isoformat()))

    def get_initial_balance(self) -> float:
        """获取初始余额"""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT initial_balance FROM balance WHERE id = 1')
            row = cursor.fetchone()
            return row['initial_balance'] if row else 2000.0

    # ============ 模式管理 (模拟盘 / 实盘) ============

    def get_active_mode(self, default: str = 'simulation') -> str:
        """获取当前激活的模式: 'simulation' 或 'live'"""
        return self.get_config('active_mode', default)

    def set_active_mode(self, mode: str):
        """设置激活的模式"""
        if mode not in ('simulation', 'live'):
            raise ValueError(f"Invalid mode: {mode}")
        self.set_config('active_mode', mode)

    def get_mode_config(self, mode: str) -> Dict[str, Any]:
        """获取指定模式下的配置 (去除 key 前缀)"""
        prefix = f"{mode}_"
        all_cfg = self.get_all_config()
        result = {}
        for k, v in all_cfg.items():
            if k.startswith(prefix):
                result[k[len(prefix):]] = v
        return result

    def set_mode_config(self, mode: str, config: Dict[str, Any]):
        """设置指定模式下的配置 (自动加前缀)"""
        prefixed = {f"{mode}_{k}": v for k, v in config.items()}
        self.set_all_config(prefixed)


# 全局数据库实例
_db: Optional[Database] = None


def get_db() -> Database:
    """获取数据库实例"""
    global _db
    if _db is None:
        _db = Database()
    return _db
