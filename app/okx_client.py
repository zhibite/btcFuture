"""
OKX 交易所 API 客户端模块
用于与 OKX 永续合约 API 进行交互
"""

import time
import hmac
import hashlib
import base64
import json
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
import requests


logger = logging.getLogger("OKXClient")


class OKXClient:
    """OKX API 客户端"""

    # API 端点
    BASE_URL_TEST = "https://www.okx.com"
    BASE_URL_PROD = "https://www.okx.com"

    # 合约规格默认值（与 BTC-USDT-SWAP 当前实际一致；启动时会用 /instruments 校准）
    DEFAULT_CTVAL = 0.01       # 1 张 = 0.01 BTC
    DEFAULT_LOT_SZ = 0.01      # 步长 0.01 张
    DEFAULT_MIN_SZ = 0.01      # 最小单 0.01 张

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

        # 实例日志器（便于在 app 层用 self.client.logger.xxx 调试）
        self.logger = logging.getLogger("OKXClient")

        # 模拟账户余额
        self.sim_balance = 2000.0

        self.session = requests.Session()
        self.session.headers.update({
            'Content-Type': 'application/json',
            'x-simulated-trading': '1' if simulation else '0'
        })

        # 合约规格缓存：symbol -> {ctVal, lotSz, minSz, ctValCcy}
        self._instrument_specs: Dict[str, Dict[str, float]] = {}

        # 账户持仓模式缓存：'net_mode' | 'long_short_mode'
        # 默认按 'net_mode' 兜底，启动后会被 get_account_config 校准
        self._pos_mode: str = 'net_mode'

    def _get_timestamp(self) -> str:
        """获取 ISO 格式时间戳（必须是 UTC，否则 OKX 返回 50112）"""
        return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'

    def _sign(self, timestamp: str, method: str, path: str,
              query_string: str = "", body: str = "") -> str:
        """
        生成签名

        Args:
            timestamp: 时间戳
            method: HTTP方法
            path: 请求路径
            query_string: 排序后的 query string (e.g. "ccy=USDT")
            body: 请求体

        Returns:
            签名字符串
        """
        request_path = path + ('?' + query_string if query_string else '')
        message = timestamp + method + request_path + body
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

        # 构造 query string 并按 key 排序（OKX 签名要求）
        query_string = ''
        if params:
            sorted_items = sorted(params.items())
            query_string = '&'.join([f"{k}={v}" for k, v in sorted_items])

        # 构建请求体
        body_str = json.dumps(body) if body else ""
        sign = self._sign(timestamp, method, path, query_string, body_str)

        headers = {
            'OK-ACCESS-KEY': self.api_key,
            'OK-ACCESS-SIGN': sign,
            'OK-ACCESS-TIMESTAMP': timestamp,
            'OK-ACCESS-PASSPHRASE': self.passphrase,
        }

        try:
            if method == 'GET':
                full_url = url + ('?' + query_string if query_string else '')
                response = self.session.get(full_url, headers=headers)
            elif method == 'POST':
                response = self.session.post(url, json=body, headers=headers)
            elif method == 'DELETE':
                response = self.session.delete(url, json=body, headers=headers)
            else:
                raise ValueError(f"Unsupported method: {method}")

            response.raise_for_status()
            return response.json()

        except requests.exceptions.HTTPError as e:
            # 把 OKX 返回的真实错误体（如 {"code":"401","msg":"Invalid OK-ACCESS-KEY"}）一并带上
            body = ''
            try:
                body = e.response.text if e.response is not None else ''
            except Exception:
                pass
            return {'code': str(e.response.status_code) if e.response is not None else '-1',
                    'msg': f"{e} | body={body}"}
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

    def get_instrument_spec(self, symbol: str, force_refresh: bool = False) -> Dict[str, float]:
        """
        拉取并缓存合约规格（ctVal / lotSz / minSz / tickSz）。

        Args:
            symbol: 交易对，如 BTC-USDT-SWAP
            force_refresh: 忽略缓存重新拉取

        Returns:
            dict with keys: ctVal, lotSz, minSz, tickSz, ctValCcy
        """
        if not force_refresh and symbol in self._instrument_specs:
            return self._instrument_specs[symbol]

        # 模拟盘也用同一套默认值（已经和 OKX 现行 BTC-USDT-SWAP 规格一致）
        if self.simulation:
            spec = {
                'ctVal': self.DEFAULT_CTVAL,
                'lotSz': self.DEFAULT_LOT_SZ,
                'minSz': self.DEFAULT_MIN_SZ,
                'tickSz': 0.1,
                'ctValCcy': 'BTC',
            }
            self._instrument_specs[symbol] = spec
            return spec

        path = "/api/v5/public/instruments"
        params = {"instType": "SWAP", "instId": symbol}
        result = self._request('GET', path, params=params)

        if result.get('code') != '0' or not result.get('data'):
            err = f"code={result.get('code')} msg={result.get('msg')}"
            logger.warning(f"获取合约规格失败 ({symbol})：{err}，回退到默认值")
            spec = {
                'ctVal': self.DEFAULT_CTVAL,
                'lotSz': self.DEFAULT_LOT_SZ,
                'minSz': self.DEFAULT_MIN_SZ,
                'tickSz': 0.1,
                'ctValCcy': 'BTC',
            }
        else:
            d = result['data'][0]
            spec = {
                'ctVal': float(d.get('ctVal', self.DEFAULT_CTVAL)),
                'lotSz': float(d.get('lotSz', self.DEFAULT_LOT_SZ)),
                'minSz': float(d.get('minSz', self.DEFAULT_MIN_SZ)),
                'tickSz': float(d.get('tickSz', 0.1)),
                'ctValCcy': d.get('ctValCcy', 'BTC'),
            }

        self._instrument_specs[symbol] = spec
        logger.info(f"合约规格 {symbol}: ctVal={spec['ctVal']} lotSz={spec['lotSz']} "
                    f"minSz={spec['minSz']} ctValCcy={spec['ctValCcy']}")
        return spec

    def get_account_config(self, force_refresh: bool = False) -> Dict[str, Any]:
        """
        拉取并缓存账户级配置（重点是 posMode）。

        OKX 永续有两种持仓模式：
          - net_mode (单向持仓): 所有订单 posSide 必须 = "net"
          - long_short_mode (双向持仓): posSide 必须 = "long" 或 "short"

        Returns:
            dict with keys: posMode, acctLv, posModeRaw, ...
        """
        if not force_refresh and getattr(self, '_account_config', None):
            return self._account_config

        # 模拟盘默认 net_mode（与大多数默认账户一致）；启动校准后会被覆盖
        if self.simulation:
            cfg = {
                'posMode': self._pos_mode,
                'acctLv': '2',
                '_source': 'simulation_default',
            }
            self._account_config = cfg
            self._pos_mode = cfg['posMode']
            return cfg

        path = "/api/v5/account/config"
        result = self._request('GET', path)

        if result.get('code') != '0' or not result.get('data'):
            err = f"code={result.get('code')} msg={result.get('msg')}"
            self.logger.warning(f"获取账户配置失败：{err}，回退到 {self._pos_mode}")
            cfg = {'posMode': self._pos_mode, 'acctLv': '', '_source': 'fallback'}
        else:
            d = result['data'][0]
            raw_pos_mode = d.get('posMode', 'net_mode')
            cfg = {
                'posMode': raw_pos_mode,
                'acctLv': d.get('acctLv', ''),
                'autoLoan': d.get('autoLoan', ''),
                'greeksType': d.get('greeksType', ''),
                'level': d.get('level', ''),
                'liquidationGear': d.get('liquidationGear', ''),
                'ctIsoMode': d.get('ctIsoMode', ''),
                '_source': 'okx_live',
            }

        self._account_config = cfg
        self._pos_mode = cfg['posMode']
        self.logger.info(
            f"账户配置: posMode={cfg['posMode']} acctLv={cfg['acctLv']} "
            f"level={cfg.get('level', '')}"
        )
        return cfg

    def _resolve_pos_side(self, symbol: str, explicit: Optional[str] = None,
                          direction: Optional[str] = None) -> str:
        """
        根据账户 posMode + 策略方向，解析出本次订单的 posSide。

        Args:
            symbol: 交易对（暂未用，留作未来按 symbol 单独配置）
            explicit: 调用方显式传入的 posSide（最高优先级）
            direction: 'long' | 'short'（净模式下被忽略）

        Returns:
            'net' 或 'long' 或 'short'
        """
        if explicit in ('net', 'long', 'short'):
            return explicit

        if self._pos_mode == 'long_short_mode':
            if direction == 'short':
                return 'short'
            # default + long 都映射为 long
            return 'long'

        # net_mode 或未知值都兜底为 net
        return 'net'

    def align_sz(self, symbol: str, raw_sz: float) -> float:
        """
        把任意 sz 数量按 lotSz 步长取整，并保证 >= minSz。
        返回调整后的 sz（OKX sz 的单位是合约张数）。
        """
        spec = self.get_instrument_spec(symbol)
        lot_sz = spec['lotSz']
        min_sz = spec['minSz']

        # 步长对齐：round(raw / lot) * lot
        if lot_sz > 0:
            lots = round(raw_sz / lot_sz)
            aligned = lots * lot_sz
        else:
            aligned = raw_sz

        # 最小下单量保护
        if aligned < min_sz:
            logger.warning(
                f"对齐后的 sz={aligned} 低于 minSz={min_sz} ({symbol})，"
                f"原始={raw_sz:.6f} → 自动调整为 {min_sz}"
            )
            aligned = min_sz

        # 浮点尾巴清理（避免 0.030000000000000002 之类的表示误差）
        aligned = float(f"{aligned:.10f}".rstrip('0').rstrip('.'))
        return aligned

    def size_margin_to_lots(self, symbol: str, margin_usdt: float,
                            price: float, leverage: int) -> float:
        """
        把"保证金 USDT"换算成 OKX sz（合约张数）。

        公式：
            名义价值 USDT   = margin_usdt × leverage
            标的货币数量 BTC = 名义价值 / price
            合约张数        = 标的货币数量 / ctVal

        返回 raw（未对齐步长）的张数。调用方应再走 align_sz() 落盘到 lotSz 倍数。
        """
        spec = self.get_instrument_spec(symbol)
        ct_val = spec['ctVal']
        if price <= 0 or ct_val <= 0 or leverage <= 0:
            return 0.0
        notional_usdt = margin_usdt * leverage
        coin_qty = notional_usdt / price
        lots = coin_qty / ct_val
        return lots

    def get_balance(self) -> Dict[str, Any]:
        """获取账户余额（统一调用 OKX API，模拟盘也走 demo 接口）"""
        # OKX balance 接口：GET 请求不需要 body，ccy 作为 query param
        # 模拟盘和实盘都走同一个 API，区别在 session header 中的 x-simulated-trading
        path = "/api/v5/account/balance"
        params = {"ccy": "USDT"}
        return self._request('GET', path, params=params)

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
                    reduce_only: bool = False,
                    pos_side: Optional[str] = None,
                    direction: Optional[str] = None) -> Optional[str]:
        """
        下单

        Args:
            symbol: 交易对
            side: 买卖方向 (buy/sell)
            order_type: 订单类型 (market/limit)
            size: 数量
            price: 价格 (限价单需要)
            reduce_only: 是否只平仓
            pos_side: 持仓方向 ('net' | 'long' | 'short')，按账户 posMode 自动推导
            direction: 策略方向 ('long' | 'short')，仅双向持仓模式有用

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

        resolved_pos_side = self._resolve_pos_side(
            symbol, explicit=pos_side, direction=direction
        )

        path = "/api/v5/trade/order"
        body = {
            "instId": symbol,
            "tdMode": "cross",
            "side": side,
            "ordType": order_type,
            "sz": str(size),
            "posSide": resolved_pos_side,
            "reduceOnly": str(reduce_only).lower()
        }

        if order_type == 'limit' and price:
            body["px"] = str(price)

        result = self._request('POST', path, body=body)

        if result.get('code') == '0' and result.get('data'):
            self.last_error = None
            ord_id = result['data'][0].get('ordId')
            if not ord_id:
                # 成功提交但子级 sCode 出错
                inner = result['data'][0] if result['data'] else {}
                self.last_error = f"sCode={inner.get('sCode')} sMsg={inner.get('sMsg')} (size={size}, posSide={resolved_pos_side})"
                return None
            return ord_id
        # 把 OKX 真实错误记录下来供上层诊断
        self.last_error = f"code={result.get('code')} msg={result.get('msg')} data={result.get('data')} (size={size}, posSide={resolved_pos_side})"
        return None

    def close_position(self, symbol: str, direction: Optional[str] = None) -> bool:
        """
        平掉所有持仓

        Args:
            symbol: 交易对
            direction: 策略方向 'long'/'short'，long_short_mode 下需要

        Returns:
            是否成功
        """
        if self.simulation:
            return True

        resolved_pos_side = self._resolve_pos_side(symbol, direction=direction)

        path = "/api/v5/trade/close-position"
        body = {
            "instId": symbol,
            "mgnMode": "cross",
            "posSide": resolved_pos_side
        }
        result = self._request('POST', path, body=body)
        if result.get('code') == '0':
            self.last_error = None
            return True
        self.last_error = f"code={result.get('code')} msg={result.get('msg')} data={result.get('data')}"
        return False

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

    def load_balance_from_db(self, db) -> float:
        """从数据库加载余额"""
        saved_balance = db.get_balance()
        if saved_balance is not None:
            self.sim_balance = saved_balance
        return self.sim_balance

    def save_balance_to_db(self, db):
        """保存余额到数据库"""
        db.set_balance(self.sim_balance)
