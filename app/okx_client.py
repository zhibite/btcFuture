"""
OKX 交易所 API 客户端模块
用于与 OKX 永续合约 API 进行交互
"""

import time
import hmac
import hashlib
import base64
import json
from typing import Optional, Dict, Any, List
from datetime import datetime
import requests


class OKXClient:
    """OKX API 客户端"""

    # API 端点
    BASE_URL_TEST = "https://www.okx.com"
    BASE_URL_PROD = "https://www.okx.com"

    def __init__(self, api_key: str, secret_key: str, passphrase: str,
                 simulation: bool = True):
        """
        初始化 OKX 客户端

        Args:
            api_key: API密钥
            secret_key: API密钥密码
            passphrase: API密钥短语
            simulation: 是否使用模拟盘
        """
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self.simulation = simulation
        self.base_url = self.BASE_URL_TEST

        # 模拟账户余额
        self.sim_balance = 2000.0

        self.session = requests.Session()
        self.session.headers.update({
            'Content-Type': 'application/json',
            'x-simulated-trading': '1' if simulation else '0'
        })

    def _get_timestamp(self) -> str:
        """获取 ISO 格式时间戳"""
        return datetime.utcnow().isoformat() + 'Z'

    def _sign(self, timestamp: str, method: str, path: str,
              body: str = "") -> str:
        """
        生成签名

        Args:
            timestamp: 时间戳
            method: HTTP方法
            path: 请求路径
            body: 请求体

        Returns:
            签名字符串
        """
        message = timestamp + method + path + body
        mac = hmac.new(
            self.secret_key.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        )
        return base64.b64encode(mac.digest()).decode('utf-8')

    def _request(self, method: str, path: str,
                 params: Dict = None, body: Dict = None) -> Dict[str, Any]:
        """
        发送 API 请求

        Args:
            method: HTTP方法
            path: 请求路径
            params: URL参数
            body: 请求体

        Returns:
            API响应
        """
        timestamp = self._get_timestamp()
        url = self.base_url + path

        # 构建请求体
        body_str = json.dumps(body) if body else ""
        sign = self._sign(timestamp, method, path, body_str)

        headers = {
            'OK-ACCESS-KEY': self.api_key,
            'OK-ACCESS-SIGN': sign,
            'OK-ACCESS-TIMESTAMP': timestamp,
            'OK-ACCESS-PASSPHRASE': self.passphrase,
        }

        try:
            if method == 'GET':
                response = self.session.get(url, params=params, headers=headers)
            elif method == 'POST':
                response = self.session.post(url, json=body, headers=headers)
            elif method == 'DELETE':
                response = self.session.delete(url, json=body, headers=headers)
            else:
                raise ValueError(f"Unsupported method: {method}")

            response.raise_for_status()
            return response.json()

        except requests.exceptions.RequestException as e:
            return {'code': '-1', 'msg': str(e)}

    def set_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        """
        设置合约杠杆倍数

        Args:
            symbol: 交易对，如 BTC-USDT-SWAP
            leverage: 杠杆倍数

        Returns:
            API响应
        """
        if self.simulation:
            return {'code': '0', 'msg': '', 'data': [{'lever': str(leverage)}]}

        path = "/api/v5/account/set-leverage"
        body = {
            "instId": symbol,
            "lever": str(leverage),
            "mgnMode": "cross"
        }
        return self._request('POST', path, body=body)

    def get_balance(self) -> Dict[str, Any]:
        """获取账户余额"""
        if self.simulation:
            return {
                'code': '0',
                'data': [{
                    'totalEq': str(self.sim_balance),
                    'upl': '0',
                    'eq': str(self.sim_balance)
                }]
            }

        path = "/api/v5/account/balance"
        body = {"ccy": "USDT"}
        return self._request('GET', path, body=body)

    def get_ticker(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取合约行情

        Args:
            symbol: 交易对

        Returns:
            行情数据
        """
        if self.simulation:
            return {
                'code': '0',
                'data': [{
                    'instId': symbol,
                    'last': '65000.0',  # 模拟价格
                    'bidPx': '64999.0',
                    'askPx': '65001.0',
                    'high24h': '66000.0',
                    'low24h': '64000.0'
                }]
            }

        path = "/api/v5/market/ticker"
        params = {"instId": symbol}
        result = self._request('GET', path, params=params)

        if result.get('code') == '0':
            return result['data'][0] if result['data'] else None
        return None

    def get_current_price(self, symbol: str) -> float:
        """获取当前市场价格"""
        ticker = self.get_ticker(symbol)
        if ticker:
            return float(ticker.get('last', '0'))
        return 0.0

    def get_position(self, symbol: str) -> List[Dict[str, Any]]:
        """
        获取当前持仓

        Args:
            symbol: 交易对

        Returns:
            持仓列表
        """
        if self.simulation:
            return []

        path = "/api/v5/account/positions"
        params = {"instId": symbol}
        result = self._request('GET', path, params=params)

        if result.get('code') == '0':
            return result.get('data', [])
        return []

    def get_open_orders(self, symbol: str) -> List[Dict[str, Any]]:
        """获取未成交订单"""
        if self.simulation:
            return []

        path = "/api/v5/trade/orders-pending"
        params = {"instId": symbol}
        result = self._request('GET', path, params=params)

        if result.get('code') == '0':
            return result.get('data', [])
        return []

    def place_order(self, symbol: str, side: str, order_type: str,
                    size: float, price: float = None,
                    reduce_only: bool = False) -> Optional[str]:
        """
        下单

        Args:
            symbol: 交易对
            side: 买卖方向 (buy/sell)
            order_type: 订单类型 (market/limit)
            size: 数量
            price: 价格 (限价单需要)
            reduce_only: 是否只平仓

        Returns:
            订单ID
        """
        if self.simulation:
            order_id = f"sim_{int(time.time() * 1000)}"
            # 模拟更新余额
            size_float = float(size) if isinstance(size, str) else size
            price_float = float(price) if isinstance(price, str) else price
            cost = size_float * price_float if price_float else size_float * self.get_current_price(symbol)
            if side == 'buy':
                self.sim_balance -= cost / 20  # 假设20倍保证金
            return order_id

        path = "/api/v5/trade/order"
        body = {
            "instId": symbol,
            "tdMode": "cross",
            "side": side,
            "ordType": order_type,
            "sz": str(size),
            "reduceOnly": str(reduce_only).lower()
        }

        if order_type == 'limit' and price:
            body["px"] = str(price)

        result = self._request('POST', path, body=body)

        if result.get('code') == '0' and result.get('data'):
            return result['data'][0].get('ordId')
        return None

    def close_position(self, symbol: str) -> bool:
        """
        平掉所有持仓

        Args:
            symbol: 交易对

        Returns:
            是否成功
        """
        if self.simulation:
            return True

        path = "/api/v5/trade/close-position"
        body = {
            "instId": symbol,
            "mgnMode": "cross"
        }
        result = self._request('POST', path, body=body)
        return result.get('code') == '0'

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        """取消订单"""
        if self.simulation:
            return True

        path = "/api/v5/trade/cancel-order"
        body = {
            "instId": symbol,
            "ordId": order_id
        }
        result = self._request('POST', path, body=body)
        return result.get('code') == '0'

    def get_order_info(self, symbol: str, order_id: str) -> Dict[str, Any]:
        """获取订单信息"""
        if self.simulation:
            return {'state': 'filled'}

        path = "/api/v5/trade/order"
        params = {"instId": symbol, "ordId": order_id}
        return self._request('GET', path, params=params)

    def get_trade_fee(self, symbol: str) -> Dict[str, Any]:
        """获取交易费率"""
        if self.simulation:
            return {
                'code': '0',
                'data': [{
                    'makerFee': '0.00020',  # 0.02%
                    'takerFee': '0.00050'   # 0.05%
                }]
            }

        path = "/api/v5/account/trade-fee"
        params = {"instId": symbol}
        return self._request('GET', path, params=params)

    def get_funding_rate(self, symbol: str) -> Optional[Dict[str, Any]]:
        """获取当前资金费率"""
        if self.simulation:
            return {
                'code': '0',
                'data': [{
                    'fundingRate': '0.000100',  # 0.01%
                    'nextFundingTime': '2026-07-16T00:00:00.000Z'
                }]
            }

        path = "/api/v5/public/funding-rate"
        params = {"instId": symbol}
        result = self._request('GET', path, params=params)

        if result.get('code') == '0' and result['data']:
            return result['data'][0]
        return None

    def get_candles(self, symbol: str, bar: str = "1H",
                    limit: int = 100) -> List[Dict[str, Any]]:
        """
        获取K线数据

        Args:
            symbol: 交易对
            bar: K线周期 (1m, 5m, 1H, 1D)
            limit: 数量

        Returns:
            K线数据列表
        """
        if self.simulation:
            # 生成模拟K线数据
            import random
            base_price = 65000
            candles = []
            now = int(time.time() * 1000)
            for i in range(limit, 0, -1):
                ts = now - i * 3600000
                open_p = base_price + random.uniform(-500, 500)
                high_p = open_p + random.uniform(0, 200)
                low_p = open_p - random.uniform(0, 200)
                close_p = open_p + random.uniform(-100, 100)
                candles.append({
                    'ts': str(ts),
                    'open': str(open_p),
                    'high': str(high_p),
                    'low': str(low_p),
                    'close': str(close_p)
                })
            return candles

        path = "/api/v5/market/candles"
        params = {"instId": symbol, "bar": bar, "limit": str(limit)}
        result = self._request('GET', path, params=params)

        if result.get('code') == '0':
            return result.get('data', [])
        return []

    def get_24h_price_change(self, symbol: str) -> float:
        """获取24小时价格变动百分比"""
        ticker = self.get_ticker(symbol)
        if ticker:
            last = float(ticker.get('last', '0'))
            high = float(ticker.get('high24h', '0'))
            low = float(ticker.get('low24h', '0'))
            if low > 0:
                return (last - low) / low
        return 0.0

    # ==================== 模拟交易相关 ====================

    def update_sim_balance(self, amount: float):
        """更新模拟账户余额"""
        self.sim_balance += amount

    def get_sim_balance(self) -> float:
        """获取模拟账户余额"""
        return self.sim_balance

    def reset_sim_balance(self, amount: float = 2000.0):
        """重置模拟账户余额"""
        self.sim_balance = amount
