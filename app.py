import json
import time
import random
import requests
import os
import re
from flask import Flask, render_template, jsonify, request
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import wraps
from flask import Flask, render_template, jsonify, request, Response # 增加 Response

app = Flask(__name__)
CONFIG_FILE = 'funds.json'
MAX_WORKERS = 20


# --- 核心：多源数据抓取 & 强制硬核计算 ---

def get_random_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "http://finance.sina.com.cn/"
    }


def fetch_from_sina(code):
    """
    【数据源 A：新浪财经】
    特点：最适合周末/盘后。
    核心逻辑：强制用 (最新确权净值 - 昨日净值) 计算涨跌，无视接口可能返回的0。
    """
    try:
        url = f"http://hq.sinajs.cn/list=f_{code}"
        res = requests.get(url, headers=get_random_headers(), timeout=1.5)
        try:
            content = res.content.decode('gbk')
        except:
            content = res.text

        match = re.search(r'="(.*?)"', content)
        if match:
            data = match.group(1).split(',')
            if len(data) > 4:
                # data[1]: 最新确权净值 (例如周五的净值)
                # data[3]: 上次确权净值 (例如周四的净值)
                current_price = float(data[1])
                prev_price = float(data[3])

                # 强制计算涨跌幅
                if prev_price > 0:
                    calc_gszzl = (current_price - prev_price) / prev_price * 100
                else:
                    calc_gszzl = 0

                return {
                    "source": "SINA_OFFICIAL",
                    "name": data[0],
                    "gsz": current_price,  # 当前价
                    "dwjz": prev_price,  # 昨收价
                    "gszzl": calc_gszzl,  # 手动算出来的涨幅
                    "date": data[4],
                    "status": "closed"
                }
    except:
        pass
    return None


def fetch_l2_market(code):
    """
    【数据源 B：L2 实时行情】
    特点：最适合盘中。
    核心逻辑：强制用 (f43现价 - f60昨收) 计算。
    """
    if not re.match(r'^(15|16|50|51|56|58)', str(code)):
        return None

    prefix = "1." if str(code).startswith('5') else "0."

    try:
        url = "http://push2.eastmoney.com/api/qt/stock/get"
        params = {
            "secid": f"{prefix}{code}",
            "fields": "f43,f60,f170",  # f43现价, f60昨收, f170官方涨幅
            "invt": "2",
            "_": int(time.time() * 1000)
        }
        res = requests.get(url, params=params, timeout=1).json()
        if res and res.get('data') and res['data']['f43'] != '-':
            data = res['data']
            current_price = float(data['f43'])
            prev_price = float(data['f60'])

            # 优先用官方涨幅，如果官方是0但价格不一致，手动算
            api_rate = float(data['f170'])
            if api_rate == 0 and current_price != prev_price and prev_price > 0:
                api_rate = (current_price - prev_price) / prev_price * 100

            return {
                "source": "LEVEL2_MARKET",
                "name": "",
                "gsz": current_price,
                "dwjz": prev_price,
                "gszzl": api_rate,
                "status": "trading"
            }
    except:
        pass
    return None


def fetch_eastmoney_estimate(code):
    """
    【数据源 C：天天基金估算】
    特点：盘中估值参考。
    """
    try:
        ts = int(time.time() * 1000)
        url = f"http://fundgz.1234567.com.cn/js/{code}.js?rt={ts}"
        res = requests.get(url, timeout=1)
        match = re.search(r'jsonpgz\((.*?)\);', res.text)
        if match:
            data = json.loads(match.group(1))
            return {
                "source": "EASTMONEY_EST",
                "name": data['name'],
                "gsz": float(data['gsz']),  # 实时估值
                "dwjz": float(data['dwjz']),  # 昨日净值
                "gszzl": float(data['gszzl']),
                "date": data['gztime'][:10],
                "status": "trading"
            }
    except:
        pass
    return None


def get_best_data(code):
    """智能决策：周末优先信新浪，盘中优先信L2/天天"""

    # 1. 尝试 L2
    l2 = fetch_l2_market(code)

    # 2. 尝试 新浪 和 天天
    sina = fetch_from_sina(code)
    east = fetch_eastmoney_estimate(code)

    # 补全名字逻辑
    name = f"基金{code}"
    if sina and sina.get('name'):
        name = sina['name']
    elif east and east.get('name'):
        name = east['name']

    if l2:
        l2['name'] = name
        return l2

    # 3. 决策：新浪 vs 天天
    current_hour = time.localtime().tm_hour
    is_trading_time = 9 <= current_hour <= 15
    is_weekend = time.localtime().tm_wday >= 5  # 5=Sat, 6=Sun

    if is_weekend or (not is_trading_time):
        if sina: return sina
        if east: return east
    else:
        # 盘中
        if east: return east
        if sina: return sina

    return None


# --- 业务逻辑 ---

def process_single_fund(item):
    """处理单只基金"""
    try:
        code = item['code']
        data = get_best_data(code)

        name = item.get('name', '未知')
        gsz = item['cost']
        dwjz = item['cost']
        gszzl = 0.0
        time_str = "--"
        src_tag = "OFFLINE"

        if data:
            gsz = data['gsz']
            dwjz = data['dwjz']
            gszzl = data['gszzl']

            if data.get('name'):
                name = data['name']

            if data['source'] == 'SINA_OFFICIAL':
                time_str = data['date']
                src_tag = "官方净值"
            elif data['source'] == 'LEVEL2_MARKET':
                time_str = "实时"
                src_tag = "L2行情"
            else:
                time_str = "估算"
                src_tag = "实时估算"

        # --- 核心计算公式 ---
        shares = item['shares']

        # 1. 持有市值
        market_value = shares * gsz

        # 2. 今日盈亏
        day_profit = (gsz - dwjz) * shares

        # 3. 累计盈亏
        total_profit = (gsz - item['cost']) * shares

        return {
            "code": code,
            "name": name,
            "gsz": gsz,
            "gszzl": gszzl,
            "market_value": round(market_value, 2),
            "day_profit": round(day_profit, 2),
            "total_profit": round(total_profit, 2),
            "update_time": time_str[-8:] if len(time_str) > 8 else time_str,
            "status": "online" if data else "failed",
            "src_tag": src_tag
        }
    except Exception as e:
        print(f"Error {item['code']}: {e}")
        return {
            "code": item['code'], "name": item.get('name', 'Err'),
            "gsz": 0, "gszzl": 0, "market_value": 0, "day_profit": 0, "total_profit": 0,
            "update_time": "--", "status": "failed", "src_tag": "Err"
        }


# --- 基础接口 ---

def load_holdings():
    if not os.path.exists(CONFIG_FILE):
        return []
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return []


def save_holdings(data):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/add_fund', methods=['POST'])
def add_fund():
    try:
        data = request.json
        code = str(data.get('code'))
        amount = float(data.get('amount'))
        profit = float(data.get('profit'))

        info = get_best_data(code)

        current_price = 1.0
        name = f"基金{code}"

        if info:
            current_price = info['gsz']
            if info.get('name'):
                name = info['name']

        if current_price <= 0:
            current_price = 1.0

        # 份额 = 当前金额 / 当前价格
        shares = amount / current_price

        # 成本
        principal = amount - profit
        cost = principal / shares if shares > 0 else 0

        holdings = load_holdings()
        found = False
        for item in holdings:
            if item['code'] == code:
                item['name'] = name
                item['shares'] = round(shares, 2)
                item['cost'] = round(cost, 4)
                found = True
                break
        if not found:
            holdings.append({"code": code, "name": name, "shares": round(shares, 2), "cost": round(cost, 4)})

        save_holdings(holdings)
        return jsonify({"status": "success", "msg": f"校准成功! 当前参考价: {current_price}"})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500


@app.route('/api/delete_fund', methods=['POST'])
def delete_fund():
    try:
        code = str(request.json.get('code'))
        holdings = [h for h in load_holdings() if h['code'] != code]
        save_holdings(holdings)
        return jsonify({"status": "success"})
    except:
        return jsonify({"status": "error"}), 500


@app.route('/api/valuations')
def get_valuations():
    holdings = load_holdings()
    results = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_fund = {executor.submit(process_single_fund, item): item for item in holdings}
        for future in as_completed(future_to_fund):
            try:
                results.append(future.result())
            except:
                pass

    results.sort(key=lambda x: x['code'])

    t_day = sum(r['day_profit'] for r in results)
    t_hold = sum(r['total_profit'] for r in results)
    t_market = sum(r['market_value'] for r in results)

    return jsonify({
        "data": results,
        "summary": {
            "total_day_profit": round(t_day, 2),
            "total_hold_profit": round(t_hold, 2),
            "total_market_value": round(t_market, 2)
        }
    })


if __name__ == '__main__':
    app.run(debug=True, port=5000)