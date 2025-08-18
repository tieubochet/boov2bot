import os
import json
import requests
import hashlib
import hmac
from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import pytz
from redis import Redis
import google.generativeai as genai

# --- CẤU HÌNH ---
AUTO_SEARCH_NETWORKS = ['bsc', 'eth', 'tron', 'polygon', 'arbitrum', 'base']
TIMEZONE = pytz.timezone('Asia/Ho_Chi_Minh')
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
CRON_SECRET = os.getenv("CRON_SECRET")
REMINDER_THRESHOLD_MINUTES = 5
SYMBOL_TO_ID_MAP = {
    'btc': 'bitcoin', 'eth': 'ethereum', 'bnb': 'binancecoin', 'sol': 'solana',
    'xrp': 'ripple', 'doge': 'dogecoin', 'shib': 'shiba-inu', 'degen': 'degen-base',
    'sui': 'sui', 'dev': 'scout-protocol-token', 'hype':'hyperliquid', 'link': 'chainlink',
    'ondo':'ondo-finance', 'virtual':'virtual-protocol', 'trx':'tron', 'towns':'towns',
    'in': 'infinit', 'yala': 'yala', 'vra':'verasity', 'tipn':'tipn'
}
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

if GOOGLE_API_KEY:
    try:
        genai.configure(api_key=GOOGLE_API_KEY)
    except Exception as e:
        print(f"Error configuring Google Gemini: {e}")
        GOOGLE_API_KEY = None

# --- KẾT NỐI CƠ SỞ DỮ LIỆU ---
try:
    kv_url = os.getenv("teeboov2_REDIS_URL")
    if not kv_url: raise ValueError("teeboov2_REDIS_URL is not set.")
    kv = Redis.from_url(kv_url, decode_responses=True)
except Exception as e:
    print(f"FATAL: Could not connect to Redis. Error: {e}"); kv = None
# --- LOGIC QUẢN LÝ CÔNG VIỆC ---
def parse_task_from_string(task_string: str) -> tuple[datetime | None, str | None]:
    try:
        time_part, name_part = task_string.split(' - ', 1)
        name_part = name_part.strip()
        if not name_part: return None, None
        now = datetime.now(TIMEZONE)
        dt_naive = datetime.strptime(time_part.strip(), '%d/%m %H:%M')
        return now.replace(month=dt_naive.month, day=dt_naive.day, hour=dt_naive.hour, minute=dt_naive.minute, second=0, microsecond=0), name_part
    except ValueError: return None, None

def add_task(chat_id, task_string: str) -> tuple[bool, str]:
    if not kv: return False, "Lỗi: Chức năng lịch hẹn không khả dụng do không kết nối được DB."
    task_dt, name_part = parse_task_from_string(task_string)
    if not task_dt or not name_part: return False, "❌ Cú pháp sai. Dùng: `DD/MM HH:mm - Tên công việc`."
    if task_dt < datetime.now(TIMEZONE): return False, "❌ Không thể đặt lịch cho quá khứ."
    tasks = json.loads(kv.get(f"tasks:{chat_id}") or '[]')
    # Thêm type: 'simple' để phân biệt
    tasks.append({"type": "simple", "time_iso": task_dt.isoformat(), "name": name_part})
    tasks.sort(key=lambda x: x['time_iso'])
    kv.set(f"tasks:{chat_id}", json.dumps(tasks))
    return True, f"✅ Đã thêm lịch: *{name_part}*."
def add_alpha_task(chat_id, task_string: str) -> tuple[bool, str]:
    if not kv: return False, "Lỗi: Chức năng lịch hẹn không khả dụng do không kết nối được DB."
    
    try:
        parts = task_string.split(' - ', 2)
        if len(parts) != 3: raise ValueError
        
        time_part, event_name, token_info_part = parts[0], parts[1].strip(), parts[2].strip()

        task_dt, _ = parse_task_from_string(f"{time_part} - {event_name}")
        if not task_dt or not event_name: raise ValueError

        token_info = token_info_part.strip("'").split()
        if len(token_info) != 2: raise ValueError
        amount_str, contract = token_info
        amount = float(amount_str)
        
        if not is_crypto_address(contract):
            return False, f"❌ Địa chỉ contract không hợp lệ: `{contract}`"
            
        # Kiểm tra sự tồn tại của token ngay khi đặt lịch bằng GeckoTerminal
        token_details = get_token_details_by_contract(contract)
        if not token_details:
            return False, f"❌ Không tìm thấy token với contract `{contract[:10]}...` trên các mạng được hỗ trợ."
            
    except (ValueError, IndexError):
        return False, "❌ Cú pháp sai. Dùng: `/alpha DD/MM HH:mm - Tên sự kiện - 'số lượng' 'contract'`."

    if task_dt < datetime.now(TIMEZONE): return False, "❌ Không thể đặt lịch cho quá khứ."

    tasks = json.loads(kv.get(f"tasks:{chat_id}") or '[]')
    tasks.append({
        "type": "alpha",
        "time_iso": task_dt.isoformat(),
        "name": event_name,
        "amount": amount,
        "contract": contract
    })
    tasks.sort(key=lambda x: x['time_iso'])
    kv.set(f"tasks:{chat_id}", json.dumps(tasks))
    return True, f"✅ Đã thêm lịch Alpha: *{event_name}*."
def edit_task(chat_id, index_str: str, new_task_string: str) -> tuple[bool, str]:
    if not kv: return False, "Lỗi: Chức năng lịch hẹn không khả dụng do không kết nối được DB."
    
    try:
        task_index = int(index_str) - 1
        if task_index < 0: raise ValueError
    except (ValueError, AssertionError):
        return False, "❌ Số thứ tự không hợp lệ."

    user_tasks = json.loads(kv.get(f"tasks:{chat_id}") or '[]')
    now = datetime.now(TIMEZONE)
    active_tasks = [t for t in user_tasks if datetime.fromisoformat(t['time_iso']) > now]

    if task_index >= len(active_tasks):
        return False, "❌ Số thứ tự không hợp lệ."

    # Xác định công việc cần sửa và loại của nó
    task_to_edit_ref = active_tasks[task_index]
    task_type = task_to_edit_ref.get("type", "simple")

    # Xóa công việc cũ khỏi danh sách đầy đủ
    user_tasks = [t for t in user_tasks if t['time_iso'] != task_to_edit_ref['time_iso']]

    # Xử lý và tạo công việc mới dựa trên loại
    if task_type == "alpha":
        try:
            parts = new_task_string.split(' - ', 2)
            if len(parts) != 3: raise ValueError
            time_part, event_name, token_info_part = parts[0], parts[1].strip(), parts[2].strip()

            new_task_dt, _ = parse_task_from_string(f"{time_part} - {event_name}")
            if not new_task_dt or not event_name: raise ValueError

            token_info = token_info_part.strip("'").split()
            if len(token_info) != 2: raise ValueError
            amount_str, contract = token_info
            amount = float(amount_str)
            
            if not is_evm_address(contract):
                return False, f"❌ Địa chỉ contract BSC không hợp lệ: `{contract}`"
            
            initial_price = get_bsc_price_by_contract(contract)
            if initial_price is None:
                return False, f"❌ Không tìm thấy token với contract `{contract[:10]}...` trên mạng BSC."

            # Thêm lại công việc alpha đã được cập nhật
            user_tasks.append({
                "type": "alpha",
                "time_iso": new_task_dt.isoformat(),
                "name": event_name,
                "amount": amount,
                "contract": contract
            })
            
        except (ValueError, IndexError):
            return False, "❌ Cú pháp sai. Dùng: `DD/MM HH:mm - Tên sự kiện - 'số lượng' 'contract'`."
    
    else: # Xử lý cho công việc 'simple' (mặc định)
        new_task_dt, new_name_part = parse_task_from_string(new_task_string)
        if not new_task_dt or not new_name_part:
            return False, "❌ Cú pháp sai. Dùng: `DD/MM HH:mm - Tên công việc`."
        
        # Thêm lại công việc simple đã được cập nhật
        user_tasks.append({
            "type": "simple",
            "time_iso": new_task_dt.isoformat(),
            "name": new_name_part
        })

    # Sắp xếp lại danh sách và lưu vào Redis
    user_tasks.sort(key=lambda x: x['time_iso'])
    kv.set(f"tasks:{chat_id}", json.dumps(user_tasks))
    
    return True, f"✅ Đã sửa công việc số *{task_index + 1}*."
def list_tasks(chat_id) -> str:
    if not kv: return "Lỗi: Chức năng lịch hẹn không khả dụng do không kết nối được DB."
    user_tasks = json.loads(kv.get(f"tasks:{chat_id}") or '[]')
    active_tasks = [t for t in user_tasks if datetime.fromisoformat(t['time_iso']) > datetime.now(TIMEZONE)]
    if len(active_tasks) < len(user_tasks): kv.set(f"tasks:{chat_id}", json.dumps(active_tasks))
    if not active_tasks: return "Bạn không có lịch hẹn nào sắp tới."
    result_lines = ["*🗓️ Danh sách lịch hẹn của bạn:*"]
    for i, task in enumerate(active_tasks):
        result_lines.append(f"*{i+1}.* `{datetime.fromisoformat(task['time_iso']).strftime('%H:%M %d/%m')}` - {task['name']}")
    return "\n".join(result_lines)

# --- LOGIC CRYPTO & TIỆN ÍCH BOT ---
def get_bsc_price_by_contract(address: str) -> float | None:
    """Hàm chuyên biệt chỉ lấy giá của token trên mạng BSC."""
    network = 'bsc'
    url = f"https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{address}"
    try:
        res = requests.get(url, headers={"accept": "application/json"}, timeout=10)
        if res.status_code == 200:
            data = res.json().get('data', {}).get('attributes', {})
            price_str = data.get('price_usd')
            if price_str:
                return float(price_str)
    except requests.RequestException as e:
        print(f"Error getting BSC price for {address}: {e}")
    return None
def get_price_by_symbol(symbol: str) -> float | None:
    coin_id = SYMBOL_TO_ID_MAP.get(symbol.lower(), symbol.lower())
    url = "https://api.coingecko.com/api/v3/simple/price"; params = {'ids': coin_id, 'vs_currencies': 'usd'}
    try:
        res = requests.get(url, params=params, timeout=10)
        return res.json().get(coin_id, {}).get('usd') if res.status_code == 200 else None
    except requests.RequestException: return None
def get_crypto_explanation(query: str) -> str:
    if not GOOGLE_API_KEY: return "❌ Lỗi cấu hình: Thiếu `GOOGLE_API_KEY`."
    try:
        model = genai.GenerativeModel('gemini-2.5-pro')
        full_prompt = (f"Bạn là một trợ lý chuyên gia về tiền điện tử. Hãy trả lời câu hỏi sau một cách ngắn gọn, súc tích, và dễ hiểu bằng tiếng Việt cho người mới bắt đầu. Tập trung vào các khía cạnh quan trọng nhất. Trả lời luôn mà không cần nói gì thêm.\n\nCâu hỏi: {query}")
        response = model.generate_content(full_prompt)
        if response.parts: return response.text
        else: return "❌ Không thể tạo câu trả lời cho câu hỏi này."
    except Exception as e:
        print(f"Google Gemini API Error: {e}")
        return f"❌ Đã xảy ra lỗi khi kết nối với dịch vụ giải thích."
def calculate_value(parts: list) -> str:
    if len(parts) != 3: return "Cú pháp: `/calc <ký hiệu> <số lượng>`\nVí dụ: `/calc btc 0.5`"
    symbol, amount_str = parts[1], parts[2]
    try: amount = float(amount_str)
    except ValueError: return f"❌ Số lượng không hợp lệ: `{amount_str}`"
    price = get_price_by_symbol(symbol)
    if price is None: return f"❌ Không tìm thấy giá cho ký hiệu `{symbol}`."
    total_value = price * amount
    return f"*{symbol.upper()}*: `${price:,.2f}` x {amount_str} = *${total_value:,.2f}*"
def translate_crypto_text(text_to_translate: str) -> str:
    if not GOOGLE_API_KEY: return "❌ Lỗi cấu hình: Thiếu `GOOGLE_API_KEY`."
    try:
        model = genai.GenerativeModel('gemini-2.5-pro')
        prompt = (f"Act as an expert translator specializing in finance and cryptocurrency. Your task is to translate the following English text into Vietnamese. Use accurate and natural-sounding financial/crypto jargon appropriate for a savvy investment community. Preserve the original nuance and meaning. Only provide the final Vietnamese translation, without any additional explanation or preamble.\n\nText to translate:\n\"\"\"{text_to_translate}\"\"\"")
        response = model.generate_content(prompt)
        if response.parts: return response.text
        else: return "❌ Không thể dịch văn bản này."
    except Exception as e:
        print(f"Google Gemini API Error (Translation): {e}")
        return f"❌ Đã xảy ra lỗi khi kết nối với dịch vụ dịch thuật."
def find_perpetual_markets(symbol: str) -> str:
    """Tìm các sàn CEX và DEX có hợp đồng perpetual và hiển thị funding rate."""
    url = "https://api.coingecko.com/api/v3/derivatives"
    params = {'include_tickers': 'unexpired'}
    
    try:
        res = requests.get(url, params=params, timeout=25)
        if res.status_code != 200:
            return f"❌ Lỗi khi gọi API CoinGecko (Code: {res.status_code})."
        
        derivatives = res.json()
        if not derivatives:
            return "❌ Không thể lấy dữ liệu phái sinh từ CoinGecko."
        
        markets = []
        found = False
        search_symbol = symbol.upper()
        
        for contract in derivatives:
            contract_symbol = contract.get('symbol', '')
            
            if contract_symbol.startswith(search_symbol):
                found = True
                market_name = contract.get('market')
                
                # Sửa lỗi: Lấy trực tiếp funding rate và không nhân thêm
                # API của Coingecko đã trả về funding rate dưới dạng phần trăm
                funding_rate = contract.get('funding_rate')
                
                if market_name and funding_rate is not None:
                    markets.append({
                        'name': market_name,
                        'funding_rate': float(funding_rate)
                    })

        if not found or not markets:
            return f"ℹ️ Không tìm thấy thị trường Perpetual nào có dữ liệu funding rate cho *{symbol.upper()}*."

        # Sắp xếp các sàn theo funding rate từ cao đến thấp
        markets.sort(key=lambda x: x['funding_rate'], reverse=True)
        
        # Định dạng kết quả
        message_parts = [f"📊 *Funding Rate cho {symbol.upper()} (Perpetual):*"]
        
        for market in markets[:15]:
            rate = market['funding_rate']
            emoji = "🟢" if rate > 0 else "🔴" if rate < 0 else "⚪️"
            # Định dạng funding rate với 4 chữ số thập phân
            message_parts.append(f"{emoji} `{market['name']}`: `{rate:+.4f}%`")
            
        return "\n".join(message_parts)

    except requests.RequestException as e:
        print(f"Error in find_perpetual_markets: {e}")
        return "❌ Lỗi mạng khi lấy dữ liệu thị trường phái sinh."

def unalert_price(chat_id, address: str) -> str:
    """Xóa một cảnh báo giá đã đặt."""
    if not kv: return "Lỗi: Chức năng cảnh báo giá không khả dụng do không kết nối được DB."
    alert_key = f"{chat_id}:{address.lower()}"
    if kv.hexists("price_alerts", alert_key):
        kv.hdel("price_alerts", alert_key)
        return f"✅ Đã xóa cảnh báo giá cho token `{address[:6]}...{address[-4:]}`."
    else:
        return f"❌ Không tìm thấy cảnh báo nào cho token `{address[:6]}...{address[-4:]}`."
def set_price_alert(chat_id, address: str, percentage_str: str) -> str:
    """Thiết lập cảnh báo giá cho một token."""
    if not kv: return "Lỗi: Chức năng cảnh báo giá không khả dụng do không kết nối được DB."
    
    try:
        percentage = float(percentage_str)
        if percentage <= 0:
            return unalert_price(chat_id, address)
    except ValueError:
        return "❌ Phần trăm không hợp lệ. Vui lòng nhập một con số (ví dụ: `5`)."

    token_info = get_price_by_contract(address)
    if not token_info:
        return f"❌ Không thể tìm thấy thông tin cho token `{address[:10]}...` để đặt cảnh báo."
    
    current_price = token_info['price']
    
    alert_data = {
        "address": address.lower(),
        "network": token_info['network'],
        "symbol": token_info['symbol'], # Lưu lại symbol
        "name": token_info['name'],       # Lưu lại name
        "chat_id": chat_id,
        "threshold_percent": percentage,
        "reference_price": current_price
    }
    
    kv.hset("price_alerts", f"{chat_id}:{address.lower()}", json.dumps(alert_data))
    
    return (f"✅ Đã đặt cảnh báo cho *{token_info['name']} (${token_info['symbol']})*.\n"
            f"Bot sẽ thông báo mỗi khi giá thay đổi `±{percentage}%` so với giá tham chiếu hiện tại là `${current_price:,.4f}`.")
def list_price_alerts(chat_id) -> str:
    """Liệt kê tất cả các cảnh báo giá đang hoạt động cho một chat."""
    if not kv: return "Lỗi: Chức năng cảnh báo giá không khả dụng do không kết nối được DB."

    all_alerts_raw = kv.hgetall("price_alerts")
    user_alerts = []
    
    for key, alert_json in all_alerts_raw.items():
        if key.startswith(f"{chat_id}:"):
            try:
                alert = json.loads(alert_json)
                user_alerts.append(alert)
            except json.JSONDecodeError:
                continue
    
    if not user_alerts:
        return "Bạn chưa đặt cảnh báo giá nào."
        
    message_parts = ["*🔔 Danh sách cảnh báo giá đang hoạt động:*"]
    for alert in user_alerts:
        symbol = alert.get('symbol', 'N/A')
        name = alert.get('name', alert.get('address', 'N/A'))
        threshold = alert.get('threshold_percent', 'N/A')
        ref_price = alert.get('reference_price', 0)
        
        message_parts.append(
            f"\n- *{name} (${symbol})* | Ngưỡng: `±{threshold}%` | Giá tham chiếu: `${ref_price:,.4f}`"
        )
        
    return "\n".join(message_parts)
def get_token_details_by_contract(address: str) -> dict | None:
    """
    Hàm phụ trợ để lấy thông tin chi tiết của token (giá, mạng, symbol, name)
    từ địa chỉ contract bằng cách quét các mạng trên GeckoTerminal.
    """
    for network in AUTO_SEARCH_NETWORKS:
        url = f"https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{address}"
        try:
            res = requests.get(url, headers={"accept": "application/json"}, timeout=10)
            if res.status_code == 200:
                data = res.json().get('data', {}).get('attributes', {})
                price_str = data.get('price_usd')
                # Chỉ trả về dữ liệu nếu có giá
                if price_str:
                    return {
                        "price": float(price_str),
                        "network": network,
                        "symbol": data.get('symbol', 'N/A'),
                        "name": data.get('name', address[:10] + '...') # Tên mặc định là địa chỉ rút gọn
                    }
        except requests.RequestException:
            continue
    return None
def check_price_alerts():
    if not kv: print("Price Alert check skipped due to no DB connection."); return
    all_alerts_raw = kv.hgetall("price_alerts")
    for key, alert_json in all_alerts_raw.items():
        try:
            alert = json.loads(alert_json)
            address = alert['address']; network = alert['network']; chat_id = alert['chat_id']
            threshold = alert['threshold_percent']; ref_price = alert['reference_price']
            
            # Lấy giá hiện tại của token
            token_info = get_price_by_contract(address)
            if not token_info: continue
            
            current_price = token_info['price']
            
            price_change_pct = ((current_price - ref_price) / ref_price) * 100 if ref_price > 0 else 0
            
            if abs(price_change_pct) >= threshold:
                emoji = "📈" if price_change_pct > 0 else "📉"
                # Sử dụng tên và ký hiệu đã lưu
                name = alert.get('name', address)
                symbol = alert.get('symbol', 'Token')
                
                message = (f"🚨 *Cảnh báo giá cho {name} (${symbol})!*\n\n"
                           f"Mạng: *{network.upper()}*\n\n"
                           f"{emoji} Giá đã thay đổi *{price_change_pct:+.2f}%*\n"
                           f"Giá cũ: `${ref_price:,.4f}`\n"
                           f"Giá mới: *`${current_price:,.4f}`*")
                
                send_telegram_message(chat_id, text=message)
                
                alert['reference_price'] = current_price
                kv.hset("price_alerts", key, json.dumps(alert))

        except (json.JSONDecodeError, KeyError) as e:
            print(f"Error processing price alert for key {key}: {e}")
            continue
def is_evm_address(s: str) -> bool: return isinstance(s, str) and s.startswith('0x') and len(s) == 42
def is_tron_address(s: str) -> bool: return isinstance(s, str) and s.startswith('T') and len(s) == 34
def is_crypto_address(s: str) -> bool: return is_evm_address(s) or is_tron_address(s)
def send_telegram_message(chat_id, text, **kwargs) -> int | None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown', **kwargs}
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200 and response.json().get('ok'): return response.json().get('result', {}).get('message_id')
        print(f"Error sending message, response: {response.text}"); return None
    except requests.RequestException as e: print(f"Error sending message: {e}"); return None
def pin_telegram_message(chat_id, message_id):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/pinChatMessage"
    payload = {'chat_id': chat_id, 'message_id': message_id, 'disable_notification': False}
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200: print(f"Error pinning message: {response.text}")
    except requests.RequestException as e: print(f"Error pinning message: {e}")
def edit_telegram_message(chat_id, msg_id, text, **kwargs):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    payload = {'chat_id': chat_id, 'message_id': msg_id, 'text': text, 'parse_mode': 'Markdown', **kwargs}
    try: requests.post(url, json=payload, timeout=10)
    except requests.RequestException as e: print(f"Error editing message: {e}")
def answer_callback_query(cb_id):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
    try: requests.post(url, json={'callback_query_id': cb_id}, timeout=5)
    except requests.RequestException as e: print(f"Error answering callback: {e}")
def delete_telegram_message(chat_id, message_id):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage"
    payload = {'chat_id': chat_id, 'message_id': message_id}
    try: requests.post(url, json=payload, timeout=5)
    except requests.RequestException as e: print(f"Error deleting message: {e}")
def find_token_across_networks(address: str) -> str:
    for network in AUTO_SEARCH_NETWORKS:
        url = f"https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{address}?include=top_pools"
        try:
            res = requests.get(url, headers={"accept": "application/json"}, timeout=10)
            if res.status_code == 200:
                data = res.json()
                token_attr = data.get('data', {}).get('attributes', {})
                if not token_attr: continue

                # --- SỬA LỖI ---
                # Xử lý an toàn các giá trị có thể là None
                price_str = token_attr.get('price_usd')
                price = float(price_str) if price_str is not None else 0.0
                
                change_pct_str = token_attr.get('price_change_percentage', {}).get('h24')
                change = float(change_pct_str) if change_pct_str is not None else 0.0

                return (f"✅ *Tìm thấy trên mạng {network.upper()}*\n"
                        f"*{token_attr.get('name', 'N/A')} ({token_attr.get('symbol', 'N/A')})*\n\n"
                        f"Giá: *${price:,.8f}*\n24h: *{'📈' if change >= 0 else '📉'} {change:+.2f}%*\n\n"
                        f"🔗 [Xem trên GeckoTerminal](https://www.geckoterminal.com/{network}/tokens/{address})\n\n`{address}`")
        except requests.RequestException:
            continue
    return f"❌ Không tìm thấy token với địa chỉ `{address[:10]}...`."
def process_portfolio_text(message_text: str) -> str | None:
    lines = message_text.strip().split('\n'); total_value, result_lines, valid_lines_count = 0.0, [], 0
    for line in lines:
        parts = line.strip().split()
        if len(parts) != 3: continue
        try: amount = float(parts[0])
        except ValueError: continue
        address, network = parts[1], parts[2]
        if not is_crypto_address(address):
            result_lines.append(f"❌ Địa chỉ `{address[:10]}...` không hợp lệ."); continue
        valid_lines_count += 1
        url = f"https://api.geckoterminal.com/api/v2/networks/{network.lower()}/tokens/{address}"
        try:
            res = requests.get(url, headers={"accept": "application/json"}, timeout=10)
            if res.status_code == 200:
                attr = res.json().get('data', {}).get('attributes', {}); price = float(attr.get('price_usd', 0)); symbol = attr.get('symbol', 'N/A')
                value = amount * price; total_value += value
                result_lines.append(f"*{symbol}*: ${price:,.4f} x {amount} = *${value:,.2f}*")
            else: result_lines.append(f"❌ Không tìm thấy giá cho `{address[:10]}...` trên `{network}`")
        except requests.RequestException: result_lines.append(f"🔌 Lỗi mạng khi lấy giá cho `{address[:10]}...`")
    if valid_lines_count == 0: return None
    return "\n".join(result_lines) + f"\n--------------------\n*Húp nhẹ: *${total_value:,.2f}**"

# --- WEB SERVER (FLASK) ---
app = Flask(__name__)
@app.route('/', methods=['POST'])
def webhook():
    if not BOT_TOKEN: return "Server configuration error", 500
    data = request.get_json()
    if "callback_query" in data:
        cb = data["callback_query"]; answer_callback_query(cb["id"])
        if cb.get("data") == "refresh_portfolio" and "reply_to_message" in cb["message"]:
            result = process_portfolio_text(cb["message"]["reply_to_message"]["text"])
            if result: edit_telegram_message(cb["message"]["chat"]["id"], cb["message"]["message_id"], text=result, reply_markup=cb["message"]["reply_markup"])
        return jsonify(success=True)
    if "message" not in data or "text" not in data["message"]: return jsonify(success=True)
    chat_id = data["message"]["chat"]["id"]; msg_id = data["message"]["message_id"]
    text = data["message"]["text"].strip(); parts = text.split(); cmd = parts[0].lower()
    if cmd.startswith('/'):
        if cmd == "/start":
            start_message = ("Gòi, cần gì fen?\n\n"
                             "**Chức năng Lịch hẹn:**\n"
                             "`/add DD/MM HH:mm - Tên`\n"
                             "`/list`, `/edit <số> ...`\n\n"
                             "**Chức năng Crypto:**\n"
                             "`/alpha time - tên event - amount contract`\n"
                             "**Ví dụ: /alpha 20/08 22:00 - Alpha: GAME - 132 0x825459139c897d769339f295e962396c4f9e4a4d**\n"
                             "`/gia <ký hiệu>`\n"
                             "`/calc <ký hiệu> <số lượng>`\n"
                             "`/gt <thuật ngữ>`\n"
                             "`/tr <nội dung>`\n"
                             "`/perp <ký hiệu>`\n"
                             "`/alert <contract> <%>`\n"
                             "`/unalert <contract>`\n"
                             "`/alerts`\n\n"
                             "1️⃣ *Tra cứu Token theo Contract*\n"
                             "2️⃣ *Tính Portfolio*\n"
                             "Cú pháp: <số lượng> <contract> <chain>\n"
                             "Ví dụ: 20000 0x825459139c897d769339f295e962396c4f9e4a4d bsc")
            send_telegram_message(chat_id, text=start_message)
        elif cmd in ['/add', '/edit']:
            success = False; message = ""
            if cmd == '/add': success, message = add_task(chat_id, " ".join(parts[1:]))
            elif cmd == '/edit':
                if len(parts) < 3: message = "Cú pháp: `/edit <số> DD/MM HH:mm - Tên mới`"
                else: success, message = edit_task(chat_id, parts[1], " ".join(parts[2:]))
            if success:
                temp_msg_id = send_telegram_message(chat_id, text=message, reply_to_message_id=msg_id)
                send_telegram_message(chat_id, text=list_tasks(chat_id))
                if temp_msg_id: delete_telegram_message(chat_id, temp_msg_id)
            else: send_telegram_message(chat_id, text=message, reply_to_message_id=msg_id)
        elif cmd == '/list': send_telegram_message(chat_id, text=list_tasks(chat_id), reply_to_message_id=msg_id)
        elif cmd == '/gia':
            if len(parts) < 2: send_telegram_message(chat_id, text="Cú pháp: `/gia <ký hiệu>`", reply_to_message_id=msg_id)
            else:
                price = get_price_by_symbol(parts[1])
                if price: send_telegram_message(chat_id, text=f"Giá của *{parts[1].upper()}* là: `${price:,.4f}`", reply_to_message_id=msg_id)
                else: send_telegram_message(chat_id, text=f"❌ Không tìm thấy giá cho `{parts[1]}`.", reply_to_message_id=msg_id)
        elif cmd == '/gt':
            if len(parts) < 2: send_telegram_message(chat_id, text="Cú pháp: `/gt <câu hỏi>`", reply_to_message_id=msg_id)
            else:
                query = " ".join(parts[1:])
                temp_msg_id = send_telegram_message(chat_id, text="🤔 Đang mò, chờ chút fen...", reply_to_message_id=msg_id)
                if temp_msg_id: edit_telegram_message(chat_id, temp_msg_id, text=get_crypto_explanation(query))
        elif cmd == '/calc':
            send_telegram_message(chat_id, text=calculate_value(parts), reply_to_message_id=msg_id)
        elif cmd == '/tr':
            if len(parts) < 2: send_telegram_message(chat_id, text="Cú pháp: `/tr <nội dung>`", reply_to_message_id=msg_id)
            else:
                text_to_translate = " ".join(parts[1:])
                temp_msg_id = send_telegram_message(chat_id, text="⏳ Đang dịch, đợi tí fen...", reply_to_message_id=msg_id)
                if temp_msg_id: edit_telegram_message(chat_id, temp_msg_id, text=translate_crypto_text(text_to_translate))
        elif cmd == '/alpha':
            success, message = add_alpha_task(chat_id, " ".join(parts[1:]))
            if success:
                temp_msg_id = send_telegram_message(chat_id, text=message, reply_to_message_id=msg_id)
                send_telegram_message(chat_id, text=list_tasks(chat_id))
                if temp_msg_id: delete_telegram_message(chat_id, temp_msg_id)
            else:
                send_telegram_message(chat_id, text=message, reply_to_message_id=msg_id)
        elif cmd == '/perp':
            if len(parts) < 2: send_telegram_message(chat_id, text="Cú pháp: `/perp <ký hiệu>`", reply_to_message_id=msg_id)
            else:
                symbol = parts[1]
                temp_msg_id = send_telegram_message(chat_id, text=f"🔍 Đang tìm các sàn Futures cho *{symbol.upper()}*...", reply_to_message_id=msg_id)
                if temp_msg_id: edit_telegram_message(chat_id, temp_msg_id, text=find_perpetual_markets(symbol))
        elif cmd == '/alert':
            if len(parts) < 3:
                send_telegram_message(chat_id, text="Cú pháp: `/alert <contract> <%>`", reply_to_message_id=msg_id)
            else: send_telegram_message(chat_id, text=set_price_alert(chat_id, parts[1], parts[2]), reply_to_message_id=msg_id)
        elif cmd == '/unalert':
            if len(parts) < 2:
                send_telegram_message(chat_id, text="Cú pháp: `/unalert <địa chỉ contract>`", reply_to_message_id=msg_id)
            else:
                send_telegram_message(chat_id, text=unalert_price(chat_id, parts[1]), reply_to_message_id=msg_id)
        elif cmd == '/alerts':
            send_telegram_message(chat_id, text=list_price_alerts(chat_id), reply_to_message_id=msg_id)
        return jsonify(success=True)
    if len(parts) == 1 and is_crypto_address(parts[0]):
        send_telegram_message(chat_id, text=find_token_across_networks(parts[0]), reply_to_message_id=msg_id, disable_web_page_preview=True)
    else:
        portfolio_result = process_portfolio_text(text)
        if portfolio_result:
            refresh_btn = {'inline_keyboard': [[{'text': '🔄 Refresh', 'callback_data': 'refresh_portfolio'}]]}
            send_telegram_message(chat_id, text=portfolio_result, reply_to_message_id=msg_id, reply_markup=json.dumps(refresh_btn))
        #else:
            #send_telegram_message(chat_id, text="🤔 Cú pháp không hợp lệ. Gửi /start để xem hướng dẫn.", reply_to_message_id=msg_id)
    return jsonify(success=True)

@app.route('/check_reminders', methods=['POST'])
def cron_webhook():
    if not kv or not BOT_TOKEN or not CRON_SECRET: return jsonify(error="Server not configured"), 500
    secret = request.headers.get('X-Cron-Secret') or (request.is_json and request.get_json().get('secret'))
    if secret != CRON_SECRET: return jsonify(error="Unauthorized"), 403
    
    print(f"[{datetime.now()}] Running reminder check...")
    reminders_sent = 0
    
    for key in kv.scan_iter("tasks:*"):
        chat_id = key.split(':')[1]
        user_tasks = json.loads(kv.get(key) or '[]')
        now = datetime.now(TIMEZONE)
        active_tasks_after_check = []
        tasks_changed = False
        
        for task in user_tasks:
            task_time = datetime.fromisoformat(task['time_iso'])
            if task_time > now:
                active_tasks_after_check.append(task)
                time_until_due = task_time - now
                
                if timedelta(seconds=1) < time_until_due <= timedelta(minutes=REMINDER_THRESHOLD_MINUTES):
                    last_reminded_key = f"last_reminded:{chat_id}:{task['time_iso']}"
                    last_reminded_ts_str = kv.get(last_reminded_key)
                    last_reminded_ts = float(last_reminded_ts_str) if last_reminded_ts_str else 0
                    
                    if (datetime.now().timestamp() - last_reminded_ts) > 270:
                        minutes_left = int(time_until_due.total_seconds() / 60)
                        
                        reminder_text = f"‼️ *ANH NHẮC EM* ‼️\n\nSự kiện: *{task['name']}*\nSẽ diễn ra trong khoảng *{minutes_left} phút* nữa."

                        if task.get("type") == "alpha":
                            # <<< THAY ĐỔI: Gọi hàm lấy giá GeckoTerminal >>>
                            token_details = get_token_details_by_contract(task['contract'])
                            if token_details:
                                price = token_details['price']
                                value = price * task['amount']
                                reminder_text = (
                                    f"‼️ *ANH NHẮC EM* ‼️\n\n"
                                    f"Sự kiện: *{task['name']}*\nSẽ diễn ra trong khoảng *{minutes_left} phút* nữa.\n\n"
                                    f"Giá token: `${price:,.6f}`\n" # Thêm số thập phân
                                    f"Tổng ≈ `${value:,.2f}`"
                                )
                        
                        sent_message_id = send_telegram_message(chat_id, text=reminder_text)
                        if sent_message_id:
                            pin_telegram_message(chat_id, sent_message_id)
                        
                        kv.set(last_reminded_key, datetime.now().timestamp(), ex=3600)
                        reminders_sent += 1
            else:
                tasks_changed = True

        if tasks_changed:
            kv.set(key, json.dumps(active_tasks_after_check))

    result = {"status": "success", "reminders_sent": reminders_sent}
    print(result)
    return jsonify(result)
@app.route('/check_alerts', methods=['POST'])
def alert_cron_webhook():
    if not kv or not BOT_TOKEN or not CRON_SECRET: return jsonify(error="Server not configured"), 500
    secret = request.headers.get('X-Cron-Secret') or (request.is_json and request.get_json().get('secret'))
    if secret != CRON_SECRET: return jsonify(error="Unauthorized"), 403
    print(f"[{datetime.now()}] Running price alert check...")
    check_price_alerts()
    return jsonify(success=True)