# -*- coding: utf-8 -*-

import os
import json
import requests
import re
from datetime import datetime
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
import uuid
import atexit
from flask import Flask, request, jsonify

# --- CẤU HÌNH ---
# Danh sách các mạng để bot tự động quét khi nhận được địa chỉ contract
# Sắp xếp theo thứ tự ưu tiên (bot sẽ dừng lại ở mạng đầu tiên tìm thấy)
AUTO_SEARCH_NETWORKS = ['bsc', 'eth', 'polygon', 'arbitrum', 'base']

# --- QUẢN LÝ TRẠNG THÁI & LỊCH HẸN ---
STATE_FILE_PATH = '/tmp/bot_user_states.json'
REMINDER_FILE_PATH = '/tmp/bot_reminders.json'

# --- LOGIC QUẢN LÝ TRẠNG THÁI NGƯỜI DÙNG ---
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
    # Mặc định là True, người dùng không cần /start ở lần đầu tiên.
    return load_user_states().get(str(chat_id), True)

# --- LOGIC QUẢN LÝ LỊCH HẸN ---
def load_reminders():
    if not os.path.exists(REMINDER_FILE_PATH): return []
    try:
        with open(REMINDER_FILE_PATH, 'r') as f: return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError): return []

def save_reminders(reminders):
    os.makedirs(os.path.dirname(REMINDER_FILE_PATH), exist_ok=True)
    with open(REMINDER_FILE_PATH, 'w') as f: json.dump(reminders, f, indent=2)

def parse_reminder_text(text: str) -> dict | None:
    """
    Phân tích cú pháp tin nhắn lịch hẹn.
    Định dạng: <HH:MM UTC+7 DD/MM/YYYY>:<Nội dung công việc>
    Trả về một dict chứa thời gian UTC và nội dung, hoặc None nếu sai cú pháp.
    """
    pattern = r"^\s*<(\d{2}:\d{2})\s*(UTC[+-]\d{1,2})\s*(\d{2}/\d{2}/\d{4})>\s*:(.*)$"
    match = re.match(pattern, text, re.IGNORECASE | re.DOTALL)
    
    if not match:
        return None

    time_str, tz_str, date_str, task_description = match.groups()
    
    try:
        tz_offset = int(tz_str.replace("UTC", ""))
        tz = pytz.FixedOffset(tz_offset * 60)

        local_dt_str = f"{date_str} {time_str}"
        naive_dt = datetime.strptime(local_dt_str, "%d/%m/%Y %H:%M")
        local_dt = tz.localize(naive_dt)

        utc_dt = local_dt.astimezone(pytz.utc)

        if utc_dt <= datetime.now(pytz.utc): # Không cho đặt lịch trong quá khứ
            return "past_date"

        return {
            "trigger_time_utc": utc_dt.isoformat(),
            "task_description": task_description.strip(),
            "user_timezone_str": tz_str.upper()
        }
    except Exception as e:
        print(f"Error parsing date/time: {e}")
        return None

def format_reminders_list(chat_id: int) -> str:
    """Tạo danh sách các lịch hẹn đang chờ cho một người dùng."""
    all_reminders = load_reminders()
    user_reminders = [r for r in all_reminders if r.get('chat_id') == chat_id]

    if not user_reminders:
        return "Bạn không có lịch hẹn nào đang chờ."

    user_reminders.sort(key=lambda r: r['trigger_time_utc'])
    hcm_tz = pytz.timezone('Asia/Ho_Chi_Minh')
    
    result_lines = ["*🗓️ Danh sách lịch hẹn của bạn:*"]
    for r in user_reminders:
        utc_dt = datetime.fromisoformat(r['trigger_time_utc'].replace('Z', '+00:00'))
        local_dt = utc_dt.astimezone(hcm_tz)
        time_display = local_dt.strftime('%H:%M ngày %d/%m/%Y')
        result_lines.append(f"- `{time_display}`: {r['task_description']}")
    
    return "\n".join(result_lines)

# --- LOGIC LẤY DỮ LIỆU TỪ API ---
def get_token_price(network: str, token_address: str) -> tuple[float, str] | None:
    url = f"https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{token_address}"
    try:
        response = requests.get(url, headers={"accept": "application/json"})
        if response.status_code != 200: return None
        data = response.json()
        attributes = data.get('data', {}).get('attributes', {})
        price_usd_str = attributes.get('price_usd')
        symbol = attributes.get('symbol', 'N/A')
        if price_usd_str: return (float(price_usd_str), symbol)
        return None
    except Exception: return None

def get_full_token_info(network: str, token_address: str) -> dict | None:
    url = f"https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{token_address}?include=top_pools"
    try:
        response = requests.get(url, headers={"accept": "application/json"})
        if response.status_code != 200: return None
        response_data = response.json()
        token_data = response_data.get('data', {}).get('attributes', {})
        if not token_data: return None
        top_dex_name = "N/A"
        included_data = response_data.get('included', [])
        included_map = {item['id']: item for item in included_data}
        top_pools = response_data.get('data', {}).get('relationships', {}).get('top_pools', {}).get('data', [])
        if top_pools:
            pool_info = included_map.get(top_pools[0]['id'])
            if pool_info:
                dex_id = pool_info.get('relationships', {}).get('dex', {}).get('data', {}).get('id')
                dex_info = included_map.get(dex_id)
                if dex_info: top_dex_name = dex_info.get('attributes', {}).get('name')
        return {
            "network": network,
            "name": token_data.get('name'), "symbol": token_data.get('symbol'),
            "price_usd": token_data.get('price_usd'),
            "price_change_24h": token_data.get('price_change_percentage', {}).get('h24'),
            "address": token_data.get('address'),
            "gecko_terminal_link": f"https://www.geckoterminal.com/{network}/tokens/{token_address}",
            "top_dex_name": top_dex_name
        }
    except Exception: return None

# --- LOGIC XỬ LÝ TIN NHẮN ---
def format_token_info_message(info: dict) -> str:
    network = info.get('network', 'N/A')
    price_str = f"${float(info['price_usd']):,.8f}" if info.get('price_usd') else "N/A"
    price_change_str = "N/A"
    if info.get('price_change_24h'):
        change = float(info['price_change_24h'])
        emoji = "📈" if change >= 0 else "📉"
        price_change_str = f"{emoji} {change:+.2f}%"
    return (
        f"✅ *Tìm thấy trên mạng {network.upper()}*\n"
        f"*{info.get('name', 'N/A')} ({info.get('symbol', 'N/A')})*\n\n"
        f"Giá: *{price_str}*\n"
        f"24h: *{price_change_str}*\n"
        f"Sàn DEX chính: `{info.get('top_dex_name', 'N/A')}`\n\n"
        f"🔗 [Xem trên GeckoTerminal]({info.get('gecko_terminal_link')})\n\n"
        f"`{info.get('address')}`"
    )

def find_token_across_networks(address: str) -> str:
    for network in AUTO_SEARCH_NETWORKS:
        print(f"Searching for {address} on {network}...")
        info = get_full_token_info(network, address.lower())
        if info:
            return format_token_info_message(info)
    return f"❌ Không tìm thấy token với địa chỉ `{address[:10]}...` trên các mạng được quét: `{'`, `'.join(AUTO_SEARCH_NETWORKS)}`."

def process_portfolio_text(message_text: str) -> str | None:
    lines = message_text.strip().split('\n')
    total_value = 0.0
    result_lines = []
    valid_lines_count = 0
    for i, line in enumerate(lines):
        parts = line.strip().split()
        if len(parts) != 3: continue
        amount_str, address, network = parts
        try:
            amount = float(amount_str)
            if not is_evm_address(address):
                 result_lines.append(f"Dòng {i+1}: ❌ Địa chỉ không hợp lệ.")
                 continue
        except ValueError: continue
        valid_lines_count += 1
        price_data = get_token_price(network.lower(), address.lower())
        if price_data:
            price, symbol = price_data
            value = amount * price
            total_value += value
            result_lines.append(f"*{symbol}*: ${price:,.4f} x {amount_str} = *${value:,.2f}*")
        else:
            result_lines.append(f"❌ Không tìm thấy giá cho `{address[:10]}...` trên `{network}`.")
    if valid_lines_count == 0: return None
    final_result_text = "\n".join(result_lines)
    summary = f"\n--------------------\n*Tổng cộng: *${total_value:,.2f}**"
    return final_result_text + summary

# --- CÁC HÀM TIỆN ÍCH ---
def is_evm_address(address_str: str) -> bool:
    return isinstance(address_str, str) and address_str.startswith('0x') and len(address_str) == 42

# --- HÀM GỬI/CHỈNH SỬA TIN NHẮN TELEGRAM ---
def create_refresh_button():
    return json.dumps({'inline_keyboard': [[{'text': '🔄 Refresh', 'callback_data': 'refresh_portfolio'}]]})

def send_telegram_message(chat_id, text, token, reply_to_message_id=None, reply_markup=None, disable_web_page_preview=False):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown', 'disable_web_page_preview': disable_web_page_preview}
    if reply_to_message_id: payload['reply_to_message_id'] = reply_to_message_id
    if reply_markup: payload['reply_markup'] = reply_markup
    requests.post(url, json=payload)

def edit_telegram_message(chat_id, message_id, text, token, reply_markup=None, disable_web_page_preview=False):
    url = f"https://api.telegram.org/bot{token}/editMessageText"
    payload = {'chat_id': chat_id, 'message_id': message_id, 'text': text, 'parse_mode': 'Markdown', 'disable_web_page_preview': disable_web_page_preview}
    if reply_markup: payload['reply_markup'] = reply_markup
    requests.post(url, json=payload)

def answer_callback_query(callback_query_id, token):
    url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
    payload = {'callback_query_id': callback_query_id}
    requests.post(url, json=payload)

# --- HÀM KIỂM TRA LỊCH HẸN CỦA SCHEDULER ---
def check_and_send_reminders():
    BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
    if not BOT_TOKEN:
        print("Cannot run reminder job: TELEGRAM_TOKEN not set.")
        return
        
    print(f"[{datetime.now()}] Running scheduled job: Checking reminders...")
    all_reminders = load_reminders()
    due_reminders_indices = []
    now_utc = datetime.now(pytz.utc)

    for i, reminder in enumerate(all_reminders):
        trigger_time = datetime.fromisoformat(reminder['trigger_time_utc'].replace('Z', '+00:00'))
        if trigger_time <= now_utc:
            try:
                reminder_message = f"⏰ *LỊCH HẸN ĐẾN HẠN!*\n\nNội dung: *{reminder['task_description']}*"
                send_telegram_message(reminder['chat_id'], reminder_message, BOT_TOKEN, reply_to_message_id=reminder['message_id'])
                print(f"Sent reminder for task '{reminder['task_description']}' to chat {reminder['chat_id']}")
            except Exception as e:
                print(f"Failed to send reminder for task '{reminder['task_description']}': {e}")
            finally:
                due_reminders_indices.append(i)

    if due_reminders_indices:
        for i in sorted(due_reminders_indices, reverse=True):
            del all_reminders[i]
        save_reminders(all_reminders)
        print(f"Removed {len(due_reminders_indices)} due reminders.")

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
        if callback_query.get("data") == "refresh_portfolio":
            answer_callback_query(callback_query["id"], BOT_TOKEN)
            chat_id = callback_query["message"]["chat"]["id"]
            message_id_to_edit = callback_query["message"]["message_id"]
            if "reply_to_message" in callback_query["message"]:
                original_message_text = callback_query["message"]["reply_to_message"]["text"]
                portfolio_result = process_portfolio_text(original_message_text)
                if portfolio_result:
                    refresh_button_markup = create_refresh_button()
                    edit_telegram_message(chat_id, message_id_to_edit, portfolio_result, BOT_TOKEN, reply_markup=refresh_button_markup)
            else:
                edit_telegram_message(chat_id, message_id_to_edit, "Lỗi: Không tìm thấy tin nhắn gốc để làm mới.", BOT_TOKEN)
        return jsonify(success=True)

    # Xử lý tin nhắn văn bản
    if "message" in data and "text" in data["message"]:
        chat_id = data["message"]["chat"]["id"]
        message_id = data["message"]["message_id"]
        message_text = data["message"]["text"].strip()
        
        # 1. XỬ LÝ CÁC LỆNH ĐIỀU KHIỂN
        if message_text.lower() in ["/start", "/sta"]:
            set_user_state(chat_id, True)
            start_message = (
                "✅ *Bot đã được bật.*\n\n"
                "1️⃣ *Tra cứu Token:*\nGửi một địa chỉ contract duy nhất.\nVí dụ: `0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c`\n\n"
                "2️⃣ *Tính toán Portfolio:*\nGửi danh sách token theo cú pháp (mỗi token một dòng):\n`[số lượng] [địa chỉ contract] [mạng]`\n\n"
                "3️⃣ *Thêm Lịch hẹn:*\n`<HH:MM UTC+7 DD/MM/YYYY>:<Công việc>`\nVí dụ: `<09:00 UTC+7 25/12/2024>:Claim token X`\n\n"
                "4️⃣ *Xem Lịch hẹn:*\nGõ `/lich`\n\n"
                "Gõ /sto để tạm dừng bot."
            )
            send_telegram_message(chat_id, start_message, BOT_TOKEN)
            return jsonify(success=True)
            
        elif message_text.lower() == "/sto":
            set_user_state(chat_id, False)
            stop_message = "☑️ *Bot đã được tắt.* Mọi tin nhắn (trừ lệnh) sẽ được bỏ qua.\n\nGõ /sta để bật lại."
            send_telegram_message(chat_id, stop_message, BOT_TOKEN)
            return jsonify(success=True)
            
        elif message_text.lower() == "/lich":
            reminders_list_text = format_reminders_list(chat_id)
            send_telegram_message(chat_id, reminders_list_text, BOT_TOKEN, reply_to_message_id=message_id)
            return jsonify(success=True)

        # 2. XỬ LÝ CÁC TIN NHẮN KHÁC (CHỈ KHI BOT ĐANG BẬT)
        if is_user_active(chat_id):
            # Ưu tiên 1: Kiểm tra có phải là lịch hẹn không
            parsed_reminder = parse_reminder_text(message_text)
            if parsed_reminder:
                if parsed_reminder == "past_date":
                    send_telegram_message(chat_id, "❌ Không thể đặt lịch cho một thời điểm trong quá khứ.", BOT_TOKEN, reply_to_message_id=message_id)
                    return jsonify(success=True)

                all_reminders = load_reminders()
                new_reminder = { "id": str(uuid.uuid4()), "chat_id": chat_id, "message_id": message_id, **parsed_reminder }
                all_reminders.append(new_reminder)
                save_reminders(all_reminders)
                
                hcm_tz = pytz.timezone('Asia/Ho_Chi_Minh')
                utc_dt = datetime.fromisoformat(new_reminder['trigger_time_utc'].replace('Z', '+00:00'))
                local_dt = utc_dt.astimezone(hcm_tz)
                time_display = local_dt.strftime('%H:%M ngày %d/%m/%Y')
                confirmation_message = (
                    f"✅ *Đã lên lịch thành công!*\n\n"
                    f"Nội dung: *{new_reminder['task_description']}*\n"
                    f"Thời gian: `{time_display} (UTC+7)`\n\n"
                    f"Gõ /lich để xem tất cả."
                )
                send_telegram_message(chat_id, confirmation_message, BOT_TOKEN, reply_to_message_id=message_id)
            
            # Ưu tiên 2: Kiểm tra xem có phải là một địa chỉ contract duy nhất không
            elif len(message_text.split()) == 1 and is_evm_address(message_text):
                address = message_text
                # Gửi tin nhắn tạm thời và edit sau để người dùng biết bot đang xử lý
                response = requests.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={'chat_id': chat_id, 'text': f"🔍 Đang tìm kiếm địa chỉ `{address[:10]}...`", 'parse_mode': 'Markdown', 'reply_to_message_id': message_id}
                ).json()
                if response.get('ok'):
                    message_id_to_edit = response['result']['message_id']
                    result_text = find_token_across_networks(address)
                    edit_telegram_message(chat_id, message_id_to_edit, result_text, BOT_TOKEN, disable_web_page_preview=True)

            # Ưu tiên 3: Thử xử lý như một portfolio
            else:
                portfolio_result = process_portfolio_text(message_text)
                if portfolio_result:
                    # Gửi tin nhắn tạm thời và edit sau
                    response = requests.post(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                        json={'chat_id': chat_id, 'text': "Đang tính toán portfolio...", 'parse_mode': 'Markdown', 'reply_to_message_id': message_id}
                    ).json()
                    if response.get('ok'):
                        message_id_to_edit = response['result']['message_id']
                        refresh_button_markup = create_refresh_button()
                        edit_telegram_message(chat_id, message_id_to_edit, portfolio_result, BOT_TOKEN, reply_markup=refresh_button_markup)
        
    return jsonify(success=True)

# --- KHỞI TẠO VÀ CHẠY SCHEDULER ---
# Chỉ khởi tạo một lần duy nhất khi ứng dụng bắt đầu
scheduler = BackgroundScheduler(timezone=pytz.utc)
scheduler.add_job(
    func=check_and_send_reminders,
    trigger="interval",
    seconds=30  # Kiểm tra mỗi 30 giây
)
scheduler.start()

# Đảm bảo scheduler được tắt một cách an toàn khi ứng dụng thoát
atexit.register(lambda: scheduler.shutdown())

# Đoạn này để chạy test local, khi deploy thực tế sẽ dùng Gunicorn/uWSGI
if __name__ == '__main__':
    print("Starting Flask app with scheduler...")
    # Lấy port từ biến môi trường, mặc định là 5000
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)