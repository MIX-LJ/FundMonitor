#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基金实时估值监控系统
基于Python Flask和jQuery开发的基金实时估值监控系统，采用"资产指挥舱"风格设计
提供基金实时估值、收益计算、多数据源切换等功能
"""

import json
import time
import random
import requests
import os
import re
from flask import Flask, render_template, jsonify, request
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import wraps
from flask import Flask, render_template, jsonify, request, Response

app = Flask(__name__)

CONFIG_FILE = 'funds.json'
MAX_WORKERS = 20


def get_random_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "http://finance.sina.com.cn/"
    }


def fetch_from_sina(code):
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
                current_price = float(data[1])
                prev_price = float(data[3])

                if prev_price > 0:
                    calc_gszzl = (current_price - prev_price) / prev_price * 100
                else:
                    calc_gszzl = 0

                return {
                    "source": "SINA_OFFICIAL",
                    "name": data[0],
                    "gsz": current_price,
                    "dwjz": prev_price,
                    "gszzl": calc_gszzl,
                    "date": data[4],
                    "status": "closed"
                }
    except Exception as e:
        print(f"新浪财经数据源错误: {e}")
        pass
    return None


def fetch_l2_market(code):
    if not re.match(r'^(15|16|50|51|56|58)', str(code)):
        return None

    prefix = "1." if str(code).startswith('5') else "0."

    try:
        url = "http://push2.eastmoney.com/api/qt/stock/get"
        params = {
            "secid": f"{prefix}{code}",
            "fields": "f43,f60,f170",
            "invt": "2",
            "_": int(time.time() * 1000)
        }
        res = requests.get(url, params=params, timeout=1).json()
        if res and res.get('data') and res['data']['f43'] != '-':
            data = res['data']
            current_price = float(data['f43']) / 1000
            prev_price = float(data['f60']) / 1000

            api_rate = float(data['f170']) / 100
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
    except Exception as e:
        print(f"L2行情数据源错误: {e}")
        pass
    return None


def fetch_eastmoney_estimate(code):
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
                "gsz": float(data['gsz']),
                "dwjz": float(data['dwjz']),
                "gszzl": float(data['gszzl']),
                "date": data['gztime'][:10],
                "status": "trading"
            }
    except Exception as e:
        print(f"天天基金数据源错误: {e}")
        pass
    return None


def get_best_data(code):
    l2 = fetch_l2_market(code)
    sina = fetch_from_sina(code)
    east = fetch_eastmoney_estimate(code)

    name = f"基金{code}"
    if sina and sina.get('name'):
        name = sina['name']
    elif east and east.get('name'):
        name = east['name']

    if l2:
        l2['name'] = name
        return l2

    current_hour = time.localtime().tm_hour
    is_trading_time = 9 <= current_hour <= 15
    is_weekend = time.localtime().tm_wday >= 5

    if is_weekend or (not is_trading_time):
        if sina: return sina
        if east: return east
    else:
        if east: return east
        if sina: return sina

    return None


def process_single_fund(item):
    try:
        code = item['code']
        data = get_best_data(code)
        
        sina = fetch_from_sina(code)
        east = fetch_eastmoney_estimate(code)

        name = item.get('name', '未知')
        gsz = item['cost']
        dwjz = item['cost']
        gszzl = 0.0
        time_str = "--"
        src_tag = "OFFLINE"

        if east and 'dwjz' in east:
            dwjz = east['dwjz']
        elif sina and 'dwjz' in sina:
            dwjz = sina['dwjz']

        if data:
            gsz = data['gsz']
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

        shares = item['shares']
        market_value = shares * gsz
        day_profit = (gsz - dwjz) * shares
        total_profit = (gsz - item['cost']) * shares

        return {
            "code": code,
            "name": name,
            "net_value": dwjz,
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
        print(f"处理基金 {item['code']} 时出错: {e}")
        return {
            "code": item['code'], "name": item.get('name', 'Err'),
            "net_value": 0, "gsz": 0, "gszzl": 0, "market_value": 0, "day_profit": 0, "total_profit": 0,
            "update_time": "--", "status": "failed", "src_tag": "Err"
        }


def load_holdings():
    if not os.path.exists(CONFIG_FILE):
        return []
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"加载持仓数据失败: {e}")
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

        shares = amount / current_price
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
        print(f"添加基金失败: {e}")
        return jsonify({"status": "error", "msg": str(e)}), 500


@app.route('/api/delete_fund', methods=['POST'])
def delete_fund():
    try:
        code = str(request.json.get('code'))
        holdings = [h for h in load_holdings() if h['code'] != code]
        save_holdings(holdings)
        return jsonify({"status": "success"})
    except Exception as e:
        print(f"删除基金失败: {e}")
        return jsonify({"status": "error"}), 500


@app.route('/api/valuations')
def get_valuations():
    holdings = load_holdings()

    print("debug pass")

    results = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_fund = {executor.submit(process_single_fund, item): item for item in holdings}
        for future in as_completed(future_to_fund):
            try:
                results.append(future.result())
            except Exception as e:
                print(f"处理基金结果失败: {e}")
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
    app.run(debug=True, port=5000, use_reloader=False, threaded=True)
