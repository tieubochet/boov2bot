import os
import json
import requests
from flask import Flask, request, jsonify
from datetime import datetime
import pytz
from redis import Redis

# --- CẤU HÌNH ---
AUTO_SEARCH_NETWORKS = ['bsc', 'eth', 'polygon', 'arbitrum', 'base']
TIMEZONE = pytz.timezone('Asia/Ho_Chi_Minh')
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")

# --- KẾT NỐI CƠ SỞ DỮ LIỆU (VERCEL KV - REDIS) ---
try:
    kv_url = os.getenv("KV_URL")
    if not kv_url:
        raise ValueError("KV_URL is not set. Please connect a Vercel KV store to save tasks.")
    kv = Redis.from_url(kv_url, decode_responses=True)
except Exception as e:
    print(f"FATAL: Could not connect to Redis. Task features will be disabled. Error: {e}")
    kv = None

# --- LOGIC QUẢN LÝ CÔNG VIỆC ---

def parse_task_from_string(task_string: str) -> tuple[datetime | None, str | None]:
    """Phân tích cú pháp chuỗi 'DD/MM HH:mm - Tên' thành (datetime, name)."""
    try:
        time_part, name_part = task_string.split(' - ', 1)
        name_part = name_part.strip()
        if not name_part: return None, None
        
        now = datetime.now(TIMEZONE)
        dt_naive = datetime.strptime(time_part.strip(), '%d/%m %H:%M')
        dt_aware = now.replace(
            month=dt_naive.month, day=dt_naive.day,
            hour=dt_naive.hour, minute=dt_naive.minute,
            second=0, microsecond=0
        )
        return dt_aware, name_part
    except ValueError:
        return None, None

def add_task(chat_id, task_string: str) -> str:
    if not kv: return "Lỗi: Chức năng lịch hẹn không khả dụng do không kết nối được DB."

    task_dt, name_part = parse_task_from_string(task_string)
    if not task_dt or not name_part:
        return "❌ Cú pháp sai. Dùng: `DD/MM HH:mm - Tên công việc`."
    if task_dt < datetime.now(TIMEZONE): return "❌ Không thể đặt lịch cho quá khứ."

    tasks = json.loads(kv.get(f"tasks:{chat_id}") or '[]')
    tasks.append({"time_iso": task_dt.isoformat(), "name": name_part})
    tasks.sort(key=lambda x: x['time_iso'])
    kv.set(f"tasks:{chat_id}", json.dumps(tasks))
    
    return f"✅ Đã thêm lịch: *{name_part}* lúc *{task_dt.strftime('%H:%M %d/%m/%Y')}*."

### <<< THÊM MỚI: Chức năng sửa công việc ###
def edit_task(chat_id, index_str: str, new_task_string: str) -> str:
    """Sửa một công việc đã có."""
    if not kv: return "Lỗi: Chức năng lịch hẹn không khả dụng do không kết nối được DB."

    try:
        task_index = int(index_str) - 1
        if task_index < 0: raise ValueError
    except ValueError: return "❌ Số thứ tự không hợp lệ."

    new_task_dt, new_name_part = parse_task_from_string(new_task_string)
    if not new_task_dt or not new_name_part:
        return "❌ Cú pháp công việc mới không hợp lệ. Dùng: `DD/MM HH:mm - Tên công việc`."

    user_tasks = json.loads(kv.get(f"tasks:{chat_id}") or '[]')
    active_tasks = [t for t in user_tasks if datetime.fromisoformat(t['time_iso']) > datetime.now(TIMEZONE)]

    if task_index >= len(active_tasks): return "❌ Số thứ tự không hợp lệ."

    # Tìm task gốc trong danh sách đầy đủ để sửa
    task_to_edit_iso = active_tasks[task_index]['time_iso']
    
    for task in user_tasks:
        if task['time_iso'] == task_to_edit_iso:
            task['time_iso'] = new_task_dt.isoformat()
            task['name'] = new_name_part
            break
    
    user_tasks.sort(key=lambda x: x['time_iso'])
    kv.set(f"tasks:{chat_id}", json.dumps(user_tasks))
    
    return f"✅ Đã sửa công việc số *{task_index + 1}* thành: *{new_name_part}*."

def list_tasks(chat_id) -> str:
    # ... (Giữ nguyên không đổi)
    if not kv: return "Lỗi: Chức năng lịch hẹn không khả dụng do không kết nối được DB."
    user_tasks = json.loads(kv.get(f"tasks:{chat_id}") or '[]')
    now = datetime.now(TIMEZONE)
    active_tasks = [t for t in user_tasks if datetime.fromisoformat(t['time_iso']) > now]
    if len(active_tasks) < len(user_tasks): kv.set(f"tasks:{chat_id}", json.dumps(active_tasks))
    if not active_tasks: return "Bạn không có lịch hẹn nào sắp tới."
    result_lines = ["*🗓️ Danh sách lịch hẹn của bạn:*"]
    for i, task in enumerate(active_tasks):
        dt = datetime.fromisoformat(task['time_iso'])
        result_lines.append(f"*{i+1}.* `{dt.strftime('%H:%M %d/%m')}` - {task['name']}")
    return "\n".join(result_lines)

def delete_task(chat_id, task_index_str: str) -> str:
    # ... (Giữ nguyên không đổi)
    if not kv: return "Lỗi: Chức năng lịch hẹn không khả dụng do không kết nối được DB."
    try:
        task_index = int(task_index_str) - 1
        if task_index < 0: raise ValueError
    except ValueError: return "❌ Số thứ tự không hợp lệ."
    user_tasks = json.loads(kv.get(f"tasks:{chat_id}") or '[]')
    active_tasks = [t for t in user_tasks if datetime.fromisoformat(t['time_iso']) > datetime.now(TIMEZONE)]
    if task_index >= len(active_tasks): return "❌ Số thứ tự không hợp lệ."
    task_to_delete = active_tasks.pop(task_index)
    updated_tasks = [t for t in user_tasks if t['time_iso'] != task_to_delete['time_iso']]
    kv.set(f"tasks:{chat_id}", json.dumps(updated_tasks))
    return f"✅ Đã xóa lịch hẹn: *{task_to_delete['name']}*"

# --- LOGIC CRYPTO & TIỆN ÍCH BOT (Không thay đổi) ---
def is_evm_address(s: str) -> bool: return isinstance(s, str) and s.startswith('0x') and len(s) == 42
def send_telegram_message(chat_id, text, **kwargs):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown', **kwargs}
    try: requests.post(url, json=payload, timeout=10)
    except requests.RequestException as e: print(f"Error sending message: {e}")
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
            res = requests.get(url, headers={"accept": "application/json"}, timeout=5)
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
        valid_lines_count += 1
        address, network = parts[1], parts[2]
        url = f"https://api.geckoterminal.com/api/v2/networks/{network.lower()}/tokens/{address.lower()}"
        try:
            res = requests.get(url, headers={"accept": "application/json"}, timeout=5)
            if res.status_code == 200:
                attr = res.json().get('data', {}).get('attributes', {}); price = float(attr.get('price_usd', 0)); symbol = attr.get('symbol', 'N/A')
                value = amount * price; total_value += value
                result_lines.append(f"*{symbol}*: ${price:,.4f} x {amount} = *${value:,.2f}*")
            else: result_lines.append(f"❌ Không tìm thấy giá cho `{address[:10]}...`")
        except requests.RequestException: result_lines.append(f"🔌 Lỗi mạng khi lấy giá cho `{address[:10]}...`")
    if valid_lines_count == 0: return None
    return "\n".join(result_lines) + f"\n--------------------\n*Tổng cộng: *${total_value:,.2f}**"

# --- WEB SERVER (FLASK) ---
app = Flask(__name__)

@app.route('/', methods=['POST'])
def webhook():
    if not BOT_TOKEN:
        print("FATAL: TELEGRAM_TOKEN environment variable not set.")
        return "Server configuration error", 500

    data = request.get_json()
    
    if "callback_query" in data:
        cb = data["callback_query"]; answer_callback_query(cb["id"])
        if cb.get("data") == "refresh_portfolio" and "reply_to_message" in cb["message"]:
            result = process_portfolio_text(cb["message"]["reply_to_message"]["text"])
            if result:
                edit_telegram_message(cb["message"]["chat"]["id"], cb["message"]["message_id"], text=result, reply_markup=cb["message"]["reply_markup"])
        return jsonify(success=True)

    if "message" not in data or "text" not in data["message"]:
        return jsonify(success=True)

    chat_id = data["message"]["chat"]["id"]; msg_id = data["message"]["message_id"]
    text = data["message"]["text"].strip(); parts = text.split(); cmd = parts[0].lower()

    if cmd.startswith('/'):
        if cmd == "/start":
            ### <<< THAY ĐỔI: Cập nhật tin nhắn hướng dẫn
            send_telegram_message(chat_id, text=(
                "Gòi, cần tao giúp gì?\n\n"
                "**Chức năng Lịch hẹn:**\n"
                "`/add DD/MM HH:mm - Tên công việc`\n"
                "`/list` - Xem danh sách công việc\n"
                "`/del <số>` - Xóa công việc\n"
                "`/edit <số> DD/MM HH:mm - Tên mới`\n\n"
                "**Chức năng Crypto:**\n\n"
                "1️⃣ *Tra cứu Token*\n"
                "Chỉ cần gửi địa chỉ contract của token.\n"
                "_Ví dụ:_\n`0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c`\n\n"
                "2️⃣ *Tính Portfolio*\n"
                "Gửi danh sách theo cú pháp (mỗi token một dòng):\n"
                "`[số lượng] [địa chỉ] [mạng]`\n"
                "_Ví dụ:_\n"
                "```\n"
                "0.5 0x2260fac5e5542a773aa44fbcfedf7c193bc2c599 eth\n"
                "1000 0x55d398326f99059ff775485246999027b3197955 bsc\n"
                "```"
            ))
        elif cmd == '/add':
            send_telegram_message(chat_id, text=add_task(chat_id, " ".join(parts[1:])), reply_to_message_id=msg_id)
        elif cmd == '/list':
            send_telegram_message(chat_id, text=list_tasks(chat_id), reply_to_message_id=msg_id)
        elif cmd == '/del':
            send_telegram_message(chat_id, text=delete_task(chat_id, parts[1]) if len(parts) > 1 else "Cú pháp: `/del <số>`", reply_to_message_id=msg_id)
        ### <<< THAY ĐỔI: Thêm xử lý cho lệnh /edit
        elif cmd == '/edit':
            if len(parts) < 3:
                send_telegram_message(chat_id, text="Cú pháp: `/edit <số> DD/MM HH:mm - Tên mới`", reply_to_message_id=msg_id)
            else:
                index_str = parts[1]
                new_task_str = " ".join(parts[2:])
                send_telegram_message(chat_id, text=edit_task(chat_id, index_str, new_task_str), reply_to_message_id=msg_id)

        return jsonify(success=True)

    # Xử lý tin nhắn thường (không phải lệnh)
    if len(parts) == 1 and is_evm_address(parts[0]):
        send_telegram_message(chat_id, text=find_token_across_networks(parts[0]), reply_to_message_id=msg_id, disable_web_page_preview=True)
    else:
        portfolio_result = process_portfolio_text(text)
        if portfolio_result:
            refresh_btn = {'inline_keyboard': [[{'text': '🔄 Refresh', 'callback_data': 'refresh_portfolio'}]]}
            send_telegram_message(chat_id, text=portfolio_result, reply_to_message_id=msg_id, reply_markup=json.dumps(refresh_btn))
        else:
            send_telegram_message(chat_id, text="🤔 Cú pháp không hợp lệ. Gửi /start để xem hướng dẫn.", reply_to_message_id=msg_id)
                
    return jsonify(success=True)