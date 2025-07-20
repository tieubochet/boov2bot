import os
import json
import requests
from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import pytz # Cần cài đặt: pip install pytz

# --- CẤU HÌNH ---
# Danh sách mạng quét tự động
AUTO_SEARCH_NETWORKS = ['bsc', 'eth', 'polygon', 'arbitrum', 'base']
# Đường dẫn file lưu trạng thái người dùng (bật/tắt bot)
STATE_FILE_PATH = '/tmp/bot_user_states.json'
# Đường dẫn file lưu trữ các công việc
TASKS_FILE_PATH = '/tmp/bot_tasks.json'
# Múi giờ UTC+7 cho tính năng nhắc nhở
TIMEZONE = pytz.timezone('Asia/Ho_Chi_Minh')
# Khoảng thời gian nhắc nhở trước (tính bằng phút)
REMINDER_THRESHOLD_MINUTES = 20
# Secret key để bảo vệ endpoint của cron job. NÊN đặt làm biến môi trường.
CRON_SECRET = os.getenv("CRON_SECRET", "default-secret-please-change-me")


# --- LOGIC QUẢN LÝ TRẠNG THÁI NGƯỜI DÙNG (BẬT/TẮT BOT) ---

def load_user_states():
    if not os.path.exists(STATE_FILE_PATH): return {}
    try:
        with open(STATE_FILE_PATH, 'r') as f: return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError): return {}

def save_user_states(states):
    os.makedirs(os.path.dirname(STATE_FILE_PATH), exist_ok=True)
    with open(STATE_FILE_PATH, 'w') as f: json.dump(states, f)

def set_user_state(chat_id, is_active: bool):
    states = load_user_states()
    states[str(chat_id)] = is_active
    save_user_states(states)

def is_user_active(chat_id):
    # Mặc định là TẮT nếu chưa từng có cài đặt
    return load_user_states().get(str(chat_id), False)


# --- LOGIC QUẢN LÝ CÔNG VIỆC (TASK MANAGEMENT) ---

def load_tasks():
    """Tải danh sách công việc từ file JSON."""
    if not os.path.exists(TASKS_FILE_PATH): return {}
    try:
        with open(TASKS_FILE_PATH, 'r') as f: return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError): return {}

def save_tasks(tasks):
    """Lưu danh sách công việc vào file JSON."""
    os.makedirs(os.path.dirname(TASKS_FILE_PATH), exist_ok=True)
    with open(TASKS_FILE_PATH, 'w') as f: json.dump(tasks, f, indent=2)

def parse_user_time(time_str: str) -> datetime | None:
    """
    Phân tích cú pháp thời gian người dùng nhập (HH:mm hoặc DD/MM HH:mm)
    và chuyển thành đối tượng datetime có múi giờ UTC+7.
    """
    now = datetime.now(TIMEZONE)
    formats_to_try = ['%d/%m %H:%M', '%H:%M']
    
    for fmt in formats_to_try:
        try:
            dt_naive = datetime.strptime(time_str, fmt)
            if fmt == '%H:%M': # Chỉ có giờ:phút, giả định là hôm nay
                dt_aware = now.replace(hour=dt_naive.hour, minute=dt_naive.minute, second=0, microsecond=0)
            else: # Có ngày/tháng, giả định là năm nay
                dt_aware = now.replace(month=dt_naive.month, day=dt_naive.day, hour=dt_naive.hour, minute=dt_naive.minute, second=0, microsecond=0)
            return dt_aware
        except ValueError:
            continue
    return None

def add_task(chat_id, task_string: str) -> str:
    """Thêm một công việc mới cho người dùng."""
    try:
        time_str, task_name = task_string.split(':', 1)
        time_str, task_name = time_str.strip(), task_name.strip()
    except ValueError:
        return "❌ Cú pháp sai. Dùng: `<thời gian>:<tên công việc>`."

    task_dt = parse_user_time(time_str)
    if not task_dt:
        return "❌ Định dạng thời gian sai. Dùng `HH:mm` hoặc `DD/MM HH:mm`."

    if task_dt < datetime.now(TIMEZONE):
        return "❌ Không thể đặt lịch cho quá khứ."

    tasks = load_tasks()
    chat_id_str = str(chat_id)
    if chat_id_str not in tasks: tasks[chat_id_str] = []

    new_task = {"time_iso": task_dt.isoformat(), "name": task_name, "reminded": False}
    tasks[chat_id_str].append(new_task)
    tasks[chat_id_str].sort(key=lambda x: x['time_iso'])
    save_tasks(tasks)
    
    return f"✅ Đã thêm lịch: *{task_name}* lúc *{task_dt.strftime('%H:%M %d/%m/%Y')}*."

def list_tasks(chat_id) -> str:
    """Liệt kê các công việc chưa hoàn thành của người dùng."""
    tasks = load_tasks()
    chat_id_str = str(chat_id)
    user_tasks = tasks.get(chat_id_str, [])
    
    now = datetime.now(TIMEZONE)
    active_tasks = [t for t in user_tasks if datetime.fromisoformat(t['time_iso']) > now]
    if len(active_tasks) < len(user_tasks): # Nếu có task đã hết hạn
        tasks[chat_id_str] = active_tasks
        save_tasks(tasks)

    if not active_tasks:
        return "Bạn không có lịch hẹn nào sắp tới."

    result_lines = ["*🗓️ Danh sách lịch hẹn của bạn:*"]
    for i, task in enumerate(active_tasks):
        task_dt = datetime.fromisoformat(task['time_iso'])
        result_lines.append(f"*{i+1}.* `{task_dt.strftime('%H:%M %d/%m')}` - {task['name']}")
    return "\n".join(result_lines)

def delete_task(chat_id, task_index_str: str) -> str:
    """Xóa một công việc theo số thứ tự."""
    try:
        task_index = int(task_index_str) - 1
        if task_index < 0: raise ValueError
    except (ValueError, IndexError):
        return "❌ Số thứ tự không hợp lệ."

    tasks = load_tasks()
    chat_id_str = str(chat_id)
    now = datetime.now(TIMEZONE)
    active_tasks = [t for t in tasks.get(chat_id_str, []) if datetime.fromisoformat(t['time_iso']) > now]

    if task_index >= len(active_tasks):
        return "❌ Số thứ tự không hợp lệ."

    task_to_delete = active_tasks.pop(task_index)
    tasks[chat_id_str] = [t for t in tasks.get(chat_id_str, []) if t['time_iso'] != task_to_delete['time_iso']]
    save_tasks(tasks)
    return f"✅ Đã xóa lịch hẹn: *{task_to_delete['name']}*"


# --- LOGIC LẤY DỮ LIỆU CRYPTO ---

def get_full_token_info(network: str, token_address: str) -> dict | None:
    url = f"https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{token_address}?include=top_pools"
    try:
        response = requests.get(url, headers={"accept": "application/json"}, timeout=10)
        if response.status_code != 200: return None
        data = response.json()
        token_data = data.get('data', {}).get('attributes', {})
        if not token_data: return None
        
        top_dex_name = "N/A"
        if data.get('included'):
            included_map = {item['id']: item for item in data['included']}
            top_pools_data = data.get('data', {}).get('relationships', {}).get('top_pools', {}).get('data', [])
            if top_pools_data:
                pool_info = included_map.get(top_pools_data[0]['id'])
                if pool_info:
                    dex_id = pool_info.get('relationships', {}).get('dex', {}).get('data', {}).get('id')
                    dex_info = included_map.get(dex_id)
                    if dex_info: top_dex_name = dex_info.get('attributes', {}).get('name')

        return {
            "network": network, "name": token_data.get('name'), "symbol": token_data.get('symbol'),
            "price_usd": token_data.get('price_usd'),
            "price_change_24h": token_data.get('price_change_percentage', {}).get('h24'),
            "address": token_data.get('address'),
            "gecko_terminal_link": f"https://www.geckoterminal.com/{network}/tokens/{token_address}",
            "top_dex_name": top_dex_name
        }
    except requests.RequestException: return None

def find_token_across_networks(address: str) -> str:
    """Quét địa chỉ contract qua nhiều mạng và trả về kết quả đầu tiên."""
    for network in AUTO_SEARCH_NETWORKS:
        info = get_full_token_info(network, address.lower())
        if info:
            price = float(info['price_usd']) if info.get('price_usd') else 0
            price_change = float(info['price_change_24h']) if info.get('price_change_24h') else 0
            emoji = "📈" if price_change >= 0 else "📉"
            return (
                f"✅ *Tìm thấy trên mạng {info['network'].upper()}*\n"
                f"*{info.get('name', 'N/A')} ({info.get('symbol', 'N/A')})*\n\n"
                f"Giá: *${price:,.8f}*\n"
                f"24h: *{emoji} {price_change:+.2f}%*\n"
                f"Sàn DEX chính: `{info.get('top_dex_name', 'N/A')}`\n\n"
                f"🔗 [Xem trên GeckoTerminal]({info.get('gecko_terminal_link')})\n\n"
                f"`{info.get('address')}`"
            )
    return f"❌ Không tìm thấy token với địa chỉ `{address[:10]}...` trên các mạng đã quét."

def process_portfolio_text(message_text: str) -> str | None:
    """Xử lý tin nhắn tính toán portfolio."""
    lines = message_text.strip().split('\n')
    total_value, result_lines, valid_lines_count = 0.0, [], 0

    for line in lines:
        parts = line.strip().split()
        if len(parts) != 3: continue
        
        amount_str, address, network = parts
        try: amount = float(amount_str)
        except ValueError: continue
        
        valid_lines_count += 1
        url = f"https://api.geckoterminal.com/api/v2/networks/{network.lower()}/tokens/{address.lower()}"
        try:
            response = requests.get(url, headers={"accept": "application/json"}, timeout=5)
            if response.status_code == 200:
                data = response.json().get('data', {}).get('attributes', {})
                price = float(data.get('price_usd', 0))
                symbol = data.get('symbol', 'N/A')
                value = amount * price
                total_value += value
                result_lines.append(f"*{symbol}*: ${price:,.4f} x {amount_str} = *${value:,.2f}*")
            else:
                result_lines.append(f"❌ Không tìm thấy giá cho `{address[:10]}...` trên `{network}`.")
        except requests.RequestException:
            result_lines.append(f"🔌 Lỗi mạng khi lấy giá cho `{address[:10]}...`.")

    if valid_lines_count == 0: return None
    return "\n".join(result_lines) + f"\n--------------------\n*Tổng cộng: *${total_value:,.2f}**"


# --- CÁC HÀM TIỆN ÍCH BOT ---
def is_evm_address(s: str) -> bool:
    return isinstance(s, str) and s.startswith('0x') and len(s) == 42

def send_telegram_message(chat_id, text, token, **kwargs):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown', **kwargs}
    try:
        requests.post(url, json=payload, timeout=10)
    except requests.RequestException as e:
        print(f"Error sending message to {chat_id}: {e}")

def edit_telegram_message(chat_id, message_id, text, token, **kwargs):
    url = f"https://api.telegram.org/bot{token}/editMessageText"
    payload = {'chat_id': chat_id, 'message_id': message_id, 'text': text, 'parse_mode': 'Markdown', **kwargs}
    try:
        requests.post(url, json=payload, timeout=10)
    except requests.RequestException as e:
        print(f"Error editing message {message_id} in {chat_id}: {e}")

def answer_callback_query(callback_query_id, token):
    url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
    try:
        requests.post(url, json={'callback_query_id': callback_query_id}, timeout=5)
    except requests.RequestException as e:
        print(f"Error answering callback query {callback_query_id}: {e}")


# --- LOGIC QUÉT VÀ GỬI NHẮC NHỞ (CHO CRON JOB) ---
def check_and_send_reminders(bot_token: str):
    print(f"[{datetime.now()}] Running reminder check...")
    all_tasks = load_tasks()
    now = datetime.now(TIMEZONE)
    reminders_sent_count = 0
    tasks_changed = False

    for chat_id, user_tasks in all_tasks.items():
        for task in user_tasks:
            if task.get("reminded", False): continue
            
            task_time = datetime.fromisoformat(task['time_iso'])
            if task_time < now: continue

            time_until_due = task_time - now
            if timedelta(0) < time_until_due <= timedelta(minutes=REMINDER_THRESHOLD_MINUTES):
                minutes_left = int(time_until_due.total_seconds() / 60)
                reminder_text = (
                    f"‼️ *NHẮC NHỞ LỊCH HẸN* ‼️\n\n"
                    f"Sự kiện: *{task['name']}*\n"
                    f"Sẽ diễn ra trong khoảng *{minutes_left} phút* nữa (lúc {task_time.strftime('%H:%M')})."
                )
                send_telegram_message(chat_id, reminder_text, bot_token)
                task['reminded'] = True
                tasks_changed = True
                reminders_sent_count += 1

    if tasks_changed: save_tasks(all_tasks)
    print(f"Reminder check finished. Sent {reminders_sent_count} reminders.")
    return {"status": "success", "reminders_sent": reminders_sent_count}


# --- WEB SERVER VỚI FLASK ---
app = Flask(__name__)

@app.route('/', methods=['POST'])
def webhook():
    BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
    if not BOT_TOKEN: return "Bot token not configured", 500

    data = request.get_json()

    # Xử lý callback query (Nút Refresh)
    if "callback_query" in data:
        callback_query = data["callback_query"]
        answer_callback_query(callback_query["id"], BOT_TOKEN)
        if callback_query.get("data") == "refresh_portfolio":
            chat_id = callback_query["message"]["chat"]["id"]
            message_id_to_edit = callback_query["message"]["message_id"]
            if "reply_to_message" in callback_query["message"]:
                portfolio_result = process_portfolio_text(callback_query["message"]["reply_to_message"]["text"])
                if portfolio_result:
                    edit_telegram_message(chat_id, message_id_to_edit, portfolio_result, BOT_TOKEN, reply_markup=json.dumps({'inline_keyboard': [[{'text': '🔄 Refresh', 'callback_data': 'refresh_portfolio'}]]}))
            else:
                edit_telegram_message(chat_id, message_id_to_edit, "Lỗi: Không tìm thấy tin nhắn gốc.", BOT_TOKEN)
        return jsonify(success=True)

    # Xử lý tin nhắn văn bản
    if "message" in data and "text" in data["message"]:
        chat_id = data["message"]["chat"]["id"]
        message_id = data["message"]["message_id"]
        message_text = data["message"]["text"].strip()
        parts = message_text.split()
        command = parts[0].lower()
        
        # Lệnh nhắc nhở
        if command.startswith('/'):
            if command == '/add':
                result = add_task(chat_id, " ".join(parts[1:])) if len(parts) > 1 else "Cú pháp: `/add <thời gian>:<tên công việc>`"
                send_telegram_message(chat_id, result, BOT_TOKEN, reply_to_message_id=message_id)
            elif command == '/list':
                send_telegram_message(chat_id, list_tasks(chat_id), BOT_TOKEN, reply_to_message_id=message_id)
            elif command == '/del':
                result = delete_task(chat_id, parts[1]) if len(parts) > 1 else "Cú pháp: `/del <số thứ tự>`"
                send_telegram_message(chat_id, result, BOT_TOKEN, reply_to_message_id=message_id)
            elif command in ["/start", "/sta"]:
                set_user_state(chat_id, True)
                start_message = (
                    "✅ *Bot đã được bật.*\n\n"
                    "*Chức năng nhắc nhở:*\n"
                    "`/add HH:mm: Tên công việc`\n"
                    "`/list`\n"
                    "`/del <số>`\n\n"
                    "*Chức năng Crypto:*\n"
                    "- Gửi địa chỉ contract để tra cứu.\n"
                    "- Gửi portfolio để tính toán.\n\n"
                    "Gõ /sto để tạm dừng bot."
                )
                send_telegram_message(chat_id, start_message, BOT_TOKEN)
            elif command == '/sto':
                set_user_state(chat_id, False)
                send_telegram_message(chat_id, "☑️ *Bot đã được tắt.*", BOT_TOKEN)
            return jsonify(success=True)

        # Xử lý tin nhắn khác (khi bot đang bật)
        if is_user_active(chat_id):
            if len(parts) == 1 and is_evm_address(parts[0]):
                temp_msg = send_telegram_message(chat_id, f"🔍 Đang tìm kiếm `{parts[0][:10]}...`", BOT_TOKEN, reply_to_message_id=message_id)
                result_text = find_token_across_networks(parts[0])
                edit_telegram_message(chat_id, message_id + 1, result_text, BOT_TOKEN, disable_web_page_preview=True)
            else:
                portfolio_result = process_portfolio_text(message_text)
                if portfolio_result:
                    refresh_button = {'inline_keyboard': [[{'text': '🔄 Refresh', 'callback_data': 'refresh_portfolio'}]]}
                    send_telegram_message(chat_id, portfolio_result, BOT_TOKEN, reply_to_message_id=message_id, reply_markup=json.dumps(refresh_button))
                else:
                    error_message = "🤔 Cú pháp không hợp lệ. Gửi `/start` để xem hướng dẫn."
                    send_telegram_message(chat_id, error_message, BOT_TOKEN, reply_to_message_id=message_id)
        
    return jsonify(success=True)


@app.route('/check_reminders', methods=['POST'])
def cron_webhook():
    """Endpoint được gọi bởi dịch vụ Cron Job."""
    secret_from_header = request.headers.get('X-Cron-Secret')
    secret_from_body = request.get_json(silent=True).get('secret') if request.is_json else None
    
    if secret_from_header != CRON_SECRET and secret_from_body != CRON_SECRET:
        return jsonify(error="Unauthorized"), 403

    BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
    if not BOT_TOKEN: return jsonify(error="Bot token not configured"), 500
        
    result = check_and_send_reminders(BOT_TOKEN)
    return jsonify(result)

# Lệnh để chạy cục bộ (tùy chọn)
# if __name__ == '__main__':
#     # Đặt biến môi trường trước khi chạy
#     # export TELEGRAM_TOKEN="your_bot_token"
#     # export CRON_SECRET="your_secret"
#     app.run(debug=True, port=5001)