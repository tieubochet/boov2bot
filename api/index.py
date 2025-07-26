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
SYMBOL_TO_ID_MAP = {
    'btc': 'bitcoin', 'eth': 'ethereum', 'bnb': 'binancecoin', 'sol': 'solana',
    'xrp': 'ripple', 'doge': 'dogecoin', 'shib': 'shiba-inu'
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

# --- CHECK RANK KAITO ---
def get_user_rank(username: str) -> str:
    """Lấy dữ liệu rank từ API, nhóm theo dự án và định dạng kết quả."""
    url = f"https://star7777.shop/Kaito/GetUserRank?id={username}"
    try:
        res = requests.get(url, timeout=15)
        if res.status_code != 200:
            return f"❌ Lỗi khi gọi API rank (Code: {res.status_code})."
        
        data = res.json()
        if not data:
            return f"❌ Không tìm thấy người dùng `{username}`."
        
        # --- BẮT ĐẦU LOGIC NHÓM DỮ LIỆU ---
        
        # Bước 1: Nhóm dữ liệu theo S_PROJECT_NAME
        projects = {}
        for rank_info in data:
            project_name = rank_info.get('S_PROJECT_NAME', 'N/A')
            if project_name not in projects:
                projects[project_name] = []
            projects[project_name].append(rank_info)

        # Bước 2: Xây dựng chuỗi kết quả từ dữ liệu đã nhóm
        final_message_parts = [f"🏆 *Rank của {username}*"]
        
        for project_name, ranks in projects.items():
            project_str = f"\n\n- - - - - - - - - -\n\n*{project_name}*"
            
            # Lặp qua các rank trong cùng một dự án để lấy thông tin
            for rank_info in ranks:
                duration = rank_info.get('S_DURATION', 'N/A')
                rank = rank_info.get('N_RANK', 'N/A')
                
                # Thêm dòng chi tiết cho mỗi duration
                project_str += f"\n`{duration}`: *{rank}*"
            
            final_message_parts.append(project_str)
            
        return "".join(final_message_parts)

    except requests.RequestException as e:
        print(f"Request exception for Rank API: {e}")
        return "❌ Lỗi mạng khi lấy dữ liệu rank."
    except (json.JSONDecodeError, IndexError):
        return f"❌ Dữ liệu trả về từ API không hợp lệ cho người dùng `{username}`."
# --- END RANK KAITO---
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
    # Không cần cờ 'reminded' nữa
    tasks.append({"time_iso": task_dt.isoformat(), "name": name_part})
    tasks.sort(key=lambda x: x['time_iso'])
    kv.set(f"tasks:{chat_id}", json.dumps(tasks))
    return True, f"✅ Đã thêm lịch: *{name_part}*."
def edit_task(chat_id, index_str: str, new_task_string: str) -> tuple[bool, str]:
    if not kv: return False, "Lỗi: Chức năng lịch hẹn không khả dụng do không kết nối được DB."
    try: task_index = int(index_str) - 1; assert task_index >= 0
    except (ValueError, AssertionError): return False, "❌ Số thứ tự không hợp lệ."
    new_task_dt, new_name_part = parse_task_from_string(new_task_string)
    if not new_task_dt or not new_name_part: return False, "❌ Cú pháp công việc mới không hợp lệ. Dùng: `DD/MM HH:mm - Tên công việc`."
    user_tasks = json.loads(kv.get(f"tasks:{chat_id}") or '[]')
    active_tasks = [t for t in user_tasks if datetime.fromisoformat(t['time_iso']) > datetime.now(TIMEZONE)]
    if task_index >= len(active_tasks): return False, "❌ Số thứ tự không hợp lệ."
    task_to_edit_iso = active_tasks[task_index]['time_iso']
    for task in user_tasks:
        if task['time_iso'] == task_to_edit_iso:
            task['time_iso'] = new_task_dt.isoformat(); task['name'] = new_name_part; break
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
def delete_task(chat_id, task_index_str: str) -> tuple[bool, str]:
    if not kv: return False, "Lỗi: Chức năng lịch hẹn không khả dụng do không kết nối được DB."
    try: task_index = int(index_str) - 1; assert task_index >= 0
    except (ValueError, AssertionError): return False, "❌ Số thứ tự không hợp lệ."
    user_tasks = json.loads(kv.get(f"tasks:{chat_id}") or '[]')
    active_tasks = [t for t in user_tasks if datetime.fromisoformat(t['time_iso']) > datetime.now(TIMEZONE)]
    if task_index >= len(active_tasks): return False, "❌ Số thứ tự không hợp lệ."
    task_to_delete = active_tasks.pop(task_index)
    updated_tasks = [t for t in user_tasks if t['time_iso'] != task_to_delete['time_iso']]
    kv.set(f"tasks:{chat_id}", json.dumps(updated_tasks))
    return True, f"✅ Đã xóa lịch hẹn: *{task_to_delete['name']}*"

# --- LOGIC CRYPTO & TIỆN ÍCH BOT (Không thay đổi) ---
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
        prompt = (
            "Act as an expert translator specializing in finance and cryptocurrency. "
            "Your task is to translate the following text into Vietnamese. "
            "Use accurate and natural-sounding financial/crypto jargon appropriate for a savvy investment community. "
            "Preserve the original nuance and meaning. Only provide the final Vietnamese translation, without any additional explanation or preamble.\n\n"
            "Text to translate:\n"
            f"\"\"\"{text_to_translate}\"\"\""
        )
        response = model.generate_content(prompt)
        if response.parts: return response.text
        else: return "❌ Không thể dịch văn bản này."
    except Exception as e:
        print(f"Google Gemini API Error (Translation): {e}")
        return f"❌ Đã xảy ra lỗi khi kết nối với dịch vụ dịch thuật."
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
                             "`/list`, `/del <số>`, `/edit <số> ...`\n\n"
                             "**Chức năng Crypto:**\n"
                             "`/gia <ký hiệu>`\n"
                             "`/calc <ký hiệu> <số lượng>`\n"
                             "`/gt <thuật ngữ>`\n"
                             "`/tr <nội dung>`\n"
                             "`/ktrank <username>`\n\n"
                             "1️⃣ *Tra cứu Token theo Contract*\nChỉ cần gửi địa chỉ contract.\n"
                             "2️⃣ *Tính Portfolio*\nGửi danh sách theo cú pháp:\n`[số lượng] [địa chỉ] [mạng]`")
            send_telegram_message(chat_id, text=start_message)
        elif cmd in ['/add', '/edit', '/del']:
            success = False; message = ""
            if cmd == '/add': success, message = add_task(chat_id, " ".join(parts[1:]))
            elif cmd == '/del':
                if len(parts) > 1: success, message = delete_task(chat_id, parts[1])
                else: message = "Cú pháp: `/del <số>`"
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
        elif cmd == '/ktrank':
            if len(parts) < 2:
                send_telegram_message(chat_id, text="Cú pháp: `/ktrank <username>`", reply_to_message_id=msg_id)
            else:
                username = parts[1]
                temp_msg_id = send_telegram_message(chat_id, text=f"🏆 Đang tìm rank cho *{username}*...", reply_to_message_id=msg_id)
                if temp_msg_id:
                    result = get_user_rank(username)
                    edit_telegram_message(chat_id, temp_msg_id, text=result)
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
    tasks_to_keep = {}

    for key in kv.scan_iter("tasks:*"):
        chat_id = key.split(':')[1]
        user_tasks = json.loads(kv.get(key) or '[]')
        now = datetime.now(TIMEZONE)
        
        # Lọc ra các công việc chưa hết hạn để lưu lại
        tasks_to_keep[chat_id] = [task for task in user_tasks if datetime.fromisoformat(task['time_iso']) > now]
        
        for task in user_tasks:
            task_time = datetime.fromisoformat(task['time_iso'])
            time_until_due = task_time - now
            
            if timedelta(seconds=1) < time_until_due <= timedelta(minutes=REMINDER_THRESHOLD_MINUTES):
                last_reminded_key = f"last_reminded:{chat_id}:{task['time_iso']}"
                last_reminded_ts_str = kv.get(last_reminded_key)
                last_reminded_ts = float(last_reminded_ts_str) if last_reminded_ts_str else 0
                
                # Chỉ nhắc lại nếu lần nhắc cuối đã hơn 9 phút trước (an toàn cho cron job 10 phút)
                if (datetime.now().timestamp() - last_reminded_ts) > 540:
                    minutes_left = int(time_until_due.total_seconds() / 60)
                    reminder_text = f"‼️ *ANH NHẮC EM* ‼️\n\nSự kiện: *{task['name']}*\nSẽ diễn ra trong khoảng *{minutes_left} phút* nữa."
                    sent_message_id = send_telegram_message(chat_id, text=reminder_text)
                    if sent_message_id:
                        pin_telegram_message(chat_id, sent_message_id)
                    
                    kv.set(last_reminded_key, datetime.now().timestamp())
                    kv.expire(last_reminded_key, 3600) # Tự xóa key sau 1 giờ
                    reminders_sent += 1

        # Cập nhật lại danh sách công việc sau khi đã lọc bỏ các task hết hạn
        if len(tasks_to_keep[chat_id]) < len(user_tasks):
            kv.set(key, json.dumps(tasks_to_keep[chat_id]))

    result = {"status": "success", "reminders_sent": reminders_sent}
    print(result)
    return jsonify(result)