import os
import json
import requests
import hashlib
import hmac
from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import pytz
from redis import Redis

# --- CẤU HÌNH ---
AUTO_SEARCH_NETWORKS = ['bsc', 'eth', 'tron', 'polygon', 'arbitrum', 'base']
TIMEZONE = pytz.timezone('Asia/Ho_Chi_Minh')
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
CRON_SECRET = os.getenv("CRON_SECRET")
REMINDER_THRESHOLD_MINUTES = 30
SYMBOL_TO_ID_MAP = {'btc': 'bitcoin', 'eth': 'ethereum', 'bnb': 'binancecoin', 'sol': 'solana'}
# Biến môi trường cho Alchemy
ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY")
ALCHEMY_AUTH_TOKEN = os.getenv("ALCHEMY_AUTH_TOKEN")

# --- KẾT NỐI CƠ SỞ DỮ LIỆU (VERCEL KV - REDIS) ---
try:
    kv_url = os.getenv("teeboov2_REDIS_URL")
    if not kv_url: raise ValueError("teeboov2_REDIS_URL is not set.")
    kv = Redis.from_url(kv_url, decode_responses=True)
except Exception as e:
    print(f"FATAL: Could not connect to Redis. Error: {e}"); kv = None

# --- LOGIC QUẢN LÝ CÔNG VIỆC (Không thay đổi) ---
def parse_task_from_string(task_string: str) -> tuple[datetime | None, str | None]:
    try:
        time_part, name_part = task_string.split(' - ', 1)
        name_part = name_part.strip()
        if not name_part: return None, None
        now = datetime.now(TIMEZONE)
        dt_naive = datetime.strptime(time_part.strip(), '%d/%m %H:%M')
        return now.replace(month=dt_naive.month, day=dt_naive.day, hour=dt_naive.hour, minute=dt_naive.minute, second=0, microsecond=0), name_part
    except ValueError: return None, None
def add_task(chat_id, task_string: str) -> str:
    if not kv: return "Lỗi: Chức năng lịch hẹn không khả dụng do không kết nối được DB."
    task_dt, name_part = parse_task_from_string(task_string)
    if not task_dt or not name_part: return "❌ Cú pháp sai. Dùng: `DD/MM HH:mm - Tên công việc`."
    if task_dt < datetime.now(TIMEZONE): return "❌ Không thể đặt lịch cho quá khứ."
    tasks = json.loads(kv.get(f"tasks:{chat_id}") or '[]')
    tasks.append({"time_iso": task_dt.isoformat(), "name": name_part, "reminded": False})
    tasks.sort(key=lambda x: x['time_iso'])
    kv.set(f"tasks:{chat_id}", json.dumps(tasks))
    return f"✅ Đã thêm lịch: *{name_part}* lúc *{task_dt.strftime('%H:%M %d/%m/%Y')}*."
def edit_task(chat_id, index_str: str, new_task_string: str) -> str:
    if not kv: return "Lỗi: Chức năng lịch hẹn không khả dụng do không kết nối được DB."
    try: task_index = int(index_str) - 1; assert task_index >= 0
    except (ValueError, AssertionError): return "❌ Số thứ tự không hợp lệ."
    new_task_dt, new_name_part = parse_task_from_string(new_task_string)
    if not new_task_dt or not new_name_part: return "❌ Cú pháp công việc mới không hợp lệ. Dùng: `DD/MM HH:mm - Tên công việc`."
    user_tasks = json.loads(kv.get(f"tasks:{chat_id}") or '[]')
    active_tasks = [t for t in user_tasks if datetime.fromisoformat(t['time_iso']) > datetime.now(TIMEZONE)]
    if task_index >= len(active_tasks): return "❌ Số thứ tự không hợp lệ."
    task_to_edit_iso = active_tasks[task_index]['time_iso']
    for task in user_tasks:
        if task['time_iso'] == task_to_edit_iso:
            task['time_iso'] = new_task_dt.isoformat(); task['name'] = new_name_part; task['reminded'] = False; break
    user_tasks.sort(key=lambda x: x['time_iso'])
    kv.set(f"tasks:{chat_id}", json.dumps(user_tasks))
    return f"✅ Đã sửa công việc số *{task_index + 1}* thành: *{new_name_part}*."
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
def delete_task(chat_id, task_index_str: str) -> str:
    if not kv: return "Lỗi: Chức năng lịch hẹn không khả dụng do không kết nối được DB."
    try: task_index = int(index_str) - 1; assert task_index >= 0
    except (ValueError, AssertionError): return "❌ Số thứ tự không hợp lệ."
    user_tasks = json.loads(kv.get(f"tasks:{chat_id}") or '[]')
    active_tasks = [t for t in user_tasks if datetime.fromisoformat(t['time_iso']) > datetime.now(TIMEZONE)]
    if task_index >= len(active_tasks): return "❌ Số thứ tự không hợp lệ."
    task_to_delete = active_tasks.pop(task_index)
    updated_tasks = [t for t in user_tasks if t['time_iso'] != task_to_delete['time_iso']]
    kv.set(f"tasks:{chat_id}", json.dumps(updated_tasks))
    return f"✅ Đã xóa lịch hẹn: *{task_to_delete['name']}*"

# --- LOGIC TRACKING VÍ (Cập nhật với API mới) ---
def update_alchemy_addresses(addresses_to_add=None, addresses_to_remove=None) -> tuple[bool, str | None]:
    if not ALCHEMY_API_KEY or not ALCHEMY_AUTH_TOKEN:
        return False, "Lỗi cấu hình: Thiếu ALCHEMY_API_KEY hoặc ALCHEMY_AUTH_TOKEN."
    url = f"https://api.alchemy.com/v2/{ALCHEMY_API_KEY}"
    headers = {"X-Alchemy-Token": ALCHEMY_AUTH_TOKEN, "Content-Type": "application/json"}
    payload = {"jsonrpc": "2.0", "id": 1, "method": "alchemy_updateWebhookAddresses", "params": [addresses_to_add or [], addresses_to_remove or []]}
    try:
        ### <<< THAY ĐỔI: Tăng timeout và cải thiện log lỗi ###
        res = requests.post(url, headers=headers, json=payload, timeout=20)
        if res.status_code == 200 and 'result' in res.json():
            return True, None
        else:
            error_details = res.json().get('error', {}).get('message', res.text)
            print(f"Alchemy API Error: {error_details}")
            return False, f"Lỗi từ Alchemy: {error_details}"
    except requests.RequestException as e:
        print(f"Network request to Alchemy failed: {e}")
        return False, "Lỗi mạng khi cập nhật ví trên Alchemy."

def track_wallet(chat_id, address: str) -> str:
    if not kv: return "Lỗi: Chức năng theo dõi không khả dụng do không kết nối được DB."
    if not is_evm_address(address): return "❌ Địa chỉ ví BSC không hợp lệ."
    address_lower = address.lower()
    
    wallets = set(json.loads(kv.get(f"wallets:{chat_id}") or '[]'))
    if address_lower in wallets: return f"Ví `{address[:6]}...` đã được theo dõi."
    
    subscribers = set(json.loads(kv.get(f"subscribers:{address_lower}") or '[]'))
    if not subscribers:
        success, error = update_alchemy_addresses(addresses_to_add=[address_lower])
        if not success:
            return f"❌ {error}" if error else "❌ Lỗi không xác định khi thêm ví vào dịch vụ theo dõi."
            
    wallets.add(address_lower)
    subscribers.add(str(chat_id))
    kv.set(f"wallets:{chat_id}", json.dumps(list(wallets)))
    kv.set(f"subscribers:{address_lower}", json.dumps(list(subscribers)))
    return f"✅ Đã bắt đầu theo dõi ví:\n`{address}`"

def untrack_wallet(chat_id, address: str) -> str:
    if not kv: return "Lỗi: Chức năng theo dõi không khả dụng do không kết nối được DB."
    if not is_evm_address(address): return "❌ Địa chỉ ví BSC không hợp lệ."
    address_lower = address.lower()
    
    wallets = set(json.loads(kv.get(f"wallets:{chat_id}") or '[]'))
    if address_lower not in wallets: return f"Ví `{address[:6]}...` không có trong danh sách theo dõi."

    wallets.remove(address_lower)
    kv.set(f"wallets:{chat_id}", json.dumps(list(wallets)))
    
    subscribers = set(json.loads(kv.get(f"subscribers:{address_lower}") or '[]'))
    subscribers.discard(str(chat_id))
    kv.set(f"subscribers:{address_lower}", json.dumps(list(subscribers)))
    
    if not subscribers:
        success, error = update_alchemy_addresses(addresses_to_remove=[address_lower])
        if not success:
             return f"⚠️ Đã hủy theo dõi, nhưng có lỗi khi xóa ví khỏi dịch vụ: {error}"
        
    return f"✅ Đã hủy theo dõi ví:\n`{address}`"

def list_wallets(chat_id) -> str:
    if not kv: return "Lỗi: Chức năng theo dõi không khả dụng do không kết nối được DB."
    wallets = json.loads(kv.get(f"wallets:{chat_id}") or '[]')
    if not wallets: return "Bạn chưa theo dõi ví BSC nào."
    response = "*Danh sách các ví BSC đang theo dõi:*\n"
    for i, wallet in enumerate(wallets): response += f"`{i+1}. {wallet}`\n"
    return response

# --- LOGIC CRYPTO & TIỆN ÍCH BOT ---
def get_price_by_symbol(symbol: str) -> float | None:
    coin_id = SYMBOL_TO_ID_MAP.get(symbol.lower(), symbol.lower())
    url = "https://api.coingecko.com/api/v3/simple/price"; params = {'ids': coin_id, 'vs_currencies': 'usd'}
    try:
        res = requests.get(url, params=params, timeout=10) # Tăng timeout một chút
        return res.json().get(coin_id, {}).get('usd') if res.status_code == 200 else None
    except requests.RequestException: return None
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
    except requests.RequestException as e:
        print(f"Error sending message: {e}"); return None
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
def find_token_across_networks(address: str) -> str:
    for network in AUTO_SEARCH_NETWORKS:
        url = f"https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{address}?include=top_pools"
        try:
            res = requests.get(url, headers={"accept": "application/json"}, timeout=10) # Tăng timeout
            if res.status_code == 200:
                data = res.json(); token_attr = data.get('data', {}).get('attributes', {})
                price = float(token_attr.get('price_usd', 0)); change = float(token_attr.get('price_change_percentage', {}).get('h24', 0))
                return (f"✅ *Tìm thấy trên mạng {network.upper()}*\n"
                        f"*{token_attr.get('name', 'N/A')} ({token_attr.get('symbol', 'N/A')})*\n\n"
                        f"Giá: *${price:,.8f}*\n24h: *{'📈' if change >= 0 else '📉'} {change:+.2f}%*\n\n"
                        f"🔗 [Xem trên GeckoTerminal](https://www.geckoterminal.com/{network}/tokens/{address})\n\n`{address}`")
        except requests.RequestException: continue
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
            res = requests.get(url, headers={"accept": "application/json"}, timeout=10) # Tăng timeout
            if res.status_code == 200:
                attr = res.json().get('data', {}).get('attributes', {}); price = float(attr.get('price_usd', 0)); symbol = attr.get('symbol', 'N/A')
                value = amount * price; total_value += value
                result_lines.append(f"*{symbol}*: ${price:,.4f} x {amount} = *${value:,.2f}*")
            else: result_lines.append(f"❌ Không tìm thấy giá cho `{address[:10]}...` trên `{network}`")
        except requests.RequestException: result_lines.append(f"🔌 Lỗi mạng khi lấy giá cho `{address[:10]}...`")
    if valid_lines_count == 0: return None
    return "\n".join(result_lines) + f"\n--------------------\n*Tổng cộng: *${total_value:,.2f}**"

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
            start_message = ("Chào mừng! Bot đã sẵn sàng.\n\n"
                             "*Bot sẽ tự động PIN và THÔNG BÁO nhắc nhở cho cả nhóm.*\n"
                             "*(Lưu ý: Bot cần có quyền Admin để Pin tin nhắn)*\n\n"
                             "**Chức năng Lịch hẹn:**\n"
                             "`/add DD/MM HH:mm - Tên`\n"
                             "`/list`, `/del <số>`, `/edit <số> ...`\n\n"
                             "**Chức năng Tracking Ví BSC:**\n"
                             "`/track <địa chỉ ví>`\n"
                             "`/untrack <địa chỉ ví>`\n"
                             "`/wallets` - Xem danh sách ví\n\n"
                             "**Chức năng Crypto:**\n"
                             "`/gia <ký hiệu>`\n"
                             "Gửi contract để tra cứu token (hỗ trợ EVM & Tron).\n"
                             "Gửi portfolio để tính giá trị.")
            send_telegram_message(chat_id, text=start_message)
        elif cmd == '/add': send_telegram_message(chat_id, text=add_task(chat_id, " ".join(parts[1:])), reply_to_message_id=msg_id)
        elif cmd == '/list': send_telegram_message(chat_id, text=list_tasks(chat_id), reply_to_message_id=msg_id)
        elif cmd == '/del':
            if len(parts) > 1: send_telegram_message(chat_id, text=delete_task(chat_id, parts[1]), reply_to_message_id=msg_id)
            else: send_telegram_message(chat_id, text="Cú pháp: `/del <số>`", reply_to_message_id=msg_id)
        elif cmd == '/edit':
            if len(parts) < 3: send_telegram_message(chat_id, text="Cú pháp: `/edit <số> DD/MM HH:mm - Tên mới`", reply_to_message_id=msg_id)
            else: send_telegram_message(chat_id, text=edit_task(chat_id, parts[1], " ".join(parts[2:])), reply_to_message_id=msg_id)
        elif cmd == '/gia':
            if len(parts) < 2: send_telegram_message(chat_id, text="Cú pháp: `/gia <ký hiệu>`", reply_to_message_id=msg_id)
            else:
                price = get_price_by_symbol(parts[1])
                if price: send_telegram_message(chat_id, text=f"Giá của *{parts[1].upper()}* là: `${price:,.4f}`", reply_to_message_id=msg_id)
                else: send_telegram_message(chat_id, text=f"❌ Không tìm thấy giá cho `{parts[1]}`.", reply_to_message_id=msg_id)
        elif cmd == '/track':
            if len(parts) > 1: send_telegram_message(chat_id, text=track_wallet(chat_id, parts[1]), reply_to_message_id=msg_id)
            else: send_telegram_message(chat_id, text="Cú pháp: `/track <địa chỉ ví>`", reply_to_message_id=msg_id)
        elif cmd == '/untrack':
            if len(parts) > 1: send_telegram_message(chat_id, text=untrack_wallet(chat_id, parts[1]), reply_to_message_id=msg_id)
            else: send_telegram_message(chat_id, text="Cú pháp: `/untrack <địa chỉ ví>`", reply_to_message_id=msg_id)
        elif cmd == '/wallets':
            send_telegram_message(chat_id, text=list_wallets(chat_id), reply_to_message_id=msg_id)
        return jsonify(success=True)
    if len(parts) == 1 and is_crypto_address(parts[0]):
        send_telegram_message(chat_id, text=find_token_across_networks(parts[0]), reply_to_message_id=msg_id, disable_web_page_preview=True)
    else:
        portfolio_result = process_portfolio_text(text)
        if portfolio_result:
            refresh_btn = {'inline_keyboard': [[{'text': '🔄 Refresh', 'callback_data': 'refresh_portfolio'}]]}
            send_telegram_message(chat_id, text=portfolio_result, reply_to_message_id=msg_id, reply_markup=json.dumps(refresh_btn))
        else: send_telegram_message(chat_id, text="🤔 Cú pháp không hợp lệ. Gửi /start để xem hướng dẫn.", reply_to_message_id=msg_id)
    return jsonify(success=True)

@app.route('/check_reminders', methods=['POST'])
def cron_webhook():
    if not kv or not BOT_TOKEN or not CRON_SECRET: return jsonify(error="Server not configured"), 500
    secret = request.headers.get('X-Cron-Secret') or (request.is_json and request.get_json().get('secret'))
    if secret != CRON_SECRET: return jsonify(error="Unauthorized"), 403
    print(f"[{datetime.now()}] Running reminder check...")
    reminders_sent = 0
    for key in kv.scan_iter("tasks:*"):
        chat_id = key.split(':')[1]; user_tasks = json.loads(kv.get(key) or '[]')
        tasks_changed = False; now = datetime.now(TIMEZONE)
        for task in user_tasks:
            if not task.get("reminded", False):
                task_time = datetime.fromisoformat(task['time_iso'])
                time_until_due = task_time - now
                if timedelta(seconds=1) < time_until_due <= timedelta(minutes=REMINDER_THRESHOLD_MINUTES):
                    minutes_left = int(time_until_due.total_seconds() / 60)
                    reminder_text = f"‼️ *NHẮC NHỞ @all* ‼️\n\nSự kiện: *{task['name']}*\nSẽ diễn ra trong khoảng *{minutes_left} phút* nữa."
                    sent_message_id = send_telegram_message(chat_id, text=reminder_text)
                    if sent_message_id: pin_telegram_message(chat_id, sent_message_id)
                    task['reminded'] = True; tasks_changed = True; reminders_sent += 1
        if tasks_changed:
            kv.set(key, json.dumps(user_tasks))
    result = {"status": "success", "reminders_sent": reminders_sent}
    print(result)
    return jsonify(result)

@app.route('/alchemy-webhook', methods=['POST'])
def alchemy_webhook():
    if not kv: return jsonify(error="Server DB not configured"), 500
    if not ALCHEMY_AUTH_TOKEN: return jsonify(error="Alchemy Auth not configured"), 500
    
    signature = request.headers.get('X-Alchemy-Signature')
    body = request.data
    if not signature: return jsonify(error="Signature missing"), 401
    
    hmac_hash = hmac.new(ALCHEMY_AUTH_TOKEN.encode('utf-8'), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(hmac_hash, signature):
        print("Unauthorized Alchemy webhook attempt.")
        return jsonify(error="Unauthorized"), 401

    data = request.get_json()
    if data.get('type') == "ADDRESS_ACTIVITY":
        activity = data.get('event', {}).get('activity', [{}])[0]
        tx_hash = activity.get('hash'); from_address = activity.get('fromAddress'); to_address = activity.get('toAddress')
        value = float(activity.get('value', 0)); asset = activity.get('asset')
        
        addresses_in_tx = {from_address.lower(), to_address.lower()}
        notified_chats = set()
        
        for address in addresses_in_tx:
            subscribers = json.loads(kv.get(f"subscribers:{address}") or '[]')
            for chat_id in subscribers:
                if chat_id in notified_chats: continue
                direction = "➡️ *NHẬN*" if address == to_address.lower() else "⬅️ *GỬI*"
                message = (f"🚨 *Giao dịch mới trên ví {address[:6]}...{address[-4:]}*\n\n"
                           f"{direction} *{value:.4f} {asset}*\n\n"
                           f"Từ: `{from_address}`\n"
                           f"Tới: `{to_address}`\n\n"
                           f"🔗 [Xem trên BscScan](https://bscscan.com/tx/{tx_hash})")
                send_telegram_message(chat_id, text=message, disable_web_page_preview=True)
                notified_chats.add(chat_id)
    return jsonify(success=True)