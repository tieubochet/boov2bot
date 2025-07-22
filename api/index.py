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
REMINDER_THRESHOLD_MINUTES = 30
SYMBOL_TO_ID_MAP = {'btc': 'bitcoin', 'eth': 'ethereum', 'bnb': 'binancecoin', 'sol': 'solana'}
# Biến môi trường mới cho Google Gemini
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

# Cấu hình thư viện Google Gemini một cách an toàn
if GOOGLE_API_KEY:
    try:
        genai.configure(api_key=GOOGLE_API_KEY)
    except Exception as e:
        print(f"Error configuring Google Gemini: {e}")
        GOOGLE_API_KEY = None # Vô hiệu hóa nếu cấu hình lỗi

# --- KẾT NỐI CƠ SỞ DỮ LIỆU ---
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
    try: task_index = int(task_index_str) - 1; assert task_index >= 0
    except (ValueError, AssertionError): return "❌ Số thứ tự không hợp lệ."
    user_tasks = json.loads(kv.get(f"tasks:{chat_id}") or '[]')
    active_tasks = [t for t in user_tasks if datetime.fromisoformat(t['time_iso']) > datetime.now(TIMEZONE)]
    if task_index >= len(active_tasks): return "❌ Số thứ tự không hợp lệ."
    task_to_delete = active_tasks.pop(task_index)
    updated_tasks = [t for t in user_tasks if t['time_iso'] != task_to_delete['time_iso']]
    kv.set(f"tasks:{chat_id}", json.dumps(updated_tasks))
    return f"✅ Đã xóa lịch hẹn: *{task_to_delete['name']}*"

# --- LOGIC CRYPTO & TIỆN ÍCH BOT ---
def get_price_by_symbol(symbol: str) -> float | None:
    coin_id = SYMBOL_TO_ID_MAP.get(symbol.lower(), symbol.lower())
    url = "https://api.coingecko.com/api/v3/simple/price"; params = {'ids': coin_id, 'vs_currencies': 'usd'}
    try:
        res = requests.get(url, params=params, timeout=10)
        return res.json().get(coin_id, {}).get('usd') if res.status_code == 200 else None
    except requests.RequestException: return None

def get_crypto_explanation(query: str) -> str:
    """Lấy giải thích về thuật ngữ crypto từ Google Gemini."""
    if not GOOGLE_API_KEY:
        return "❌ Lỗi cấu hình: Thiếu `GOOGLE_API_KEY`. Vui lòng liên hệ admin để thiết lập."
    
    try:
        model = genai.GenerativeModel('gemini-2.5-pro')
        full_prompt = (
            "Bạn là một trợ lý chuyên gia về tiền điện tử. Hãy trả lời câu hỏi sau một cách "
            "ngắn gọn, súc tích, và dễ hiểu bằng tiếng Việt cho người mới bắt đầu. "
            "Tập trung vào các khía cạnh quan trọng nhất.\n\n"
            f"Câu hỏi: {query}"
        )
        response = model.generate_content(full_prompt)
        # Kiểm tra xem có nội dung trả về không
        if response.parts:
            return response.text
        else:
            # Điều này xảy ra nếu nội dung bị bộ lọc an toàn chặn
            return "❌ Không thể tạo câu trả lời cho câu hỏi này. Có thể nội dung đã vi phạm chính sách an toàn."
            
    except Exception as e:
        # In lỗi chi tiết ra log của Vercel để debug
        print(f"Google Gemini API Error: {e}")
        # Trả về thông báo lỗi chung cho người dùng
        return f"❌ Đã xảy ra lỗi khi kết nối với dịch vụ giải thích. Vui lòng thử lại sau."

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
            res = requests.get(url, headers={"accept": "application/json"}, timeout=10)
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
            res = requests.get(url, headers={"accept": "application/json"}, timeout=10)
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
            start_message = ("Gòi, cần gì fen?\n\n"
                             "**Chức năng Lịch hẹn:**\n"
                             "`/add DD/MM HH:mm - Tên`\n"
                             "`/list`, `/del <số>`, `/edit <số> ...`\n\n"
                             "**Chức năng Crypto:**\n"
                             "`/gia <ký hiệu>`\n"
                             "`/gt <thuật ngữ>` - Giải thích (vd: /gt airdrop là gì)\n\n"
                             "1️⃣ *Tra cứu Token theo Contract*\nChỉ cần gửi địa chỉ contract.\n"
                             "2️⃣ *Tính Portfolio*\nGửi danh sách theo cú pháp:\n`[số lượng] [địa chỉ] [mạng]`")
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
        elif cmd == '/gt':
            if len(parts) < 2:
                send_telegram_message(chat_id, text="Cú pháp: `/gt <câu hỏi>`\nVí dụ: `/gt airdrop là gì?`", reply_to_message_id=msg_id)
            else:
                query = " ".join(parts[1:])
                temp_msg_id = send_telegram_message(chat_id, text="🤔 Đang tìm hiểu, vui lòng chờ...", reply_to_message_id=msg_id)
                if temp_msg_id:
                    explanation = get_crypto_explanation(query)
                    edit_telegram_message(chat_id, temp_msg_id, text=explanation)
        return jsonify(success=True)
    if len(parts) == 1 and is_crypto_address(parts[0]):
        send_telegram_message(chat_id, text=find_token_across_networks(parts[0]), reply_to_message_id=msg_id, disable_web_page_preview=True)
    else:
        portfolio_result = process_portfolio_text(text)
        if portfolio_result:
            refresh_btn = {'inline_keyboard': [[{'text': '🔄 Refresh', 'callback_data': 'refresh_portfolio'}]]}
            send_telegram_message(chat_id, text=portfolio_result, reply_to_message_id=msg_id, reply_markup=json.dumps(refresh_btn))
        #else: send_telegram_message(chat_id, text="🤔 Cú pháp không hợp lệ. Gửi /start để xem hướng dẫn.", reply_to_message_id=msg_id)
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