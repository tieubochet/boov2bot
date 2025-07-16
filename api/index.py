import os
import json
import requests
from flask import Flask, request, jsonify

# --- LOGIC QUẢN LÝ TRẠNG THÁI NGƯỜI DÙNG --- ### <<< THÊM MỚI
STATE_FILE_PATH = '/tmp/bot_user_states.json'

def load_user_states():
    """Tải trạng thái (bật/tắt) của người dùng từ file JSON."""
    if not os.path.exists(STATE_FILE_PATH):
        return {}
    try:
        with open(STATE_FILE_PATH, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return {}

def save_user_states(states):
    """Lưu trạng thái người dùng vào file JSON."""
    # Đảm bảo thư mục /tmp tồn tại
    os.makedirs(os.path.dirname(STATE_FILE_PATH), exist_ok=True)
    with open(STATE_FILE_PATH, 'w') as f:
        json.dump(states, f)

def set_user_state(chat_id, is_active: bool):
    """Đặt trạng thái cho một người dùng cụ thể."""
    # chat_id phải là string để làm key trong JSON
    chat_id_str = str(chat_id)
    states = load_user_states()
    states[chat_id_str] = is_active
    save_user_states(states)

def is_user_active(chat_id):
    """Kiểm tra xem bot có đang hoạt động cho người dùng này không."""
    chat_id_str = str(chat_id)
    states = load_user_states()
    # Mặc định là TẮT nếu người dùng chưa có trong danh sách
    return states.get(chat_id_str, False)

# --- LOGIC LẤY GIÁ (Không thay đổi) ---
def get_token_price(network: str, token_address: str) -> tuple[float, str] | None:
    url = f"https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{token_address}"
    try:
        response = requests.get(url, headers={"accept": "application/json"})
        if response.status_code != 200:
            print(f"GeckoTerminal API error for {network}/{token_address}: Status {response.status_code}")
            return None
        data = response.json()
        attributes = data.get('data', {}).get('attributes', {})
        price_usd_str = attributes.get('price_usd')
        symbol = attributes.get('symbol', 'N/A')
        if price_usd_str:
            return (float(price_usd_str), symbol)
        else:
            print(f"Price not found in API response for {network}/{token_address}")
            return None
    except Exception as e:
        print(f"Error calling or parsing GeckoTerminal API: {e}")
        return None

# --- LOGIC TÍNH TOÁN PORTFOLIO (Không thay đổi) ---
def process_portfolio_text(message_text: str) -> str:
    lines = message_text.strip().split('\n')
    total_value = 0.0
    result_lines = []
    for i, line in enumerate(lines):
        parts = line.strip().split()
        if len(parts) != 3:
            result_lines.append(f"Dòng {i+1}: ❌ Lỗi cú pháp.")
            continue
        amount_str, address, network = parts
        try:
            amount = float(amount_str)
        except ValueError:
            result_lines.append(f"Dòng {i+1} ('{amount_str}'): ❌ Số lượng không hợp lệ.")
            continue
        price_data = get_token_price(network.lower(), address.lower())
        if price_data is not None:
            price, symbol = price_data
            value = amount * price
            total_value += value
            result_lines.append(f"*{symbol}*: ${price:,.4f} x {amount_str} = *${value:,.2f}*")
        else:
            result_lines.append(f"❌ Không tìm thấy giá cho `{address[:10]}...` trên `{network}`.")
    final_result_text = "\n".join(result_lines)
    summary = f"\n--------------------\n*Tổng cộng: *${total_value:,.2f}**"
    return final_result_text + summary

# --- HÀM GỬI/CHỈNH SỬA TIN NHẮN TELEGRAM (Không thay đổi) ---
def create_refresh_button():
    keyboard = {'inline_keyboard': [[{'text': '🔄 Refresh', 'callback_data': 'refresh_portfolio'}]]}
    return json.dumps(keyboard)

def send_telegram_message(chat_id, text, token, reply_to_message_id=None, reply_markup=None):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'}
    if reply_to_message_id: payload['reply_to_message_id'] = reply_to_message_id
    if reply_markup: payload['reply_markup'] = reply_markup
    requests.post(url, json=payload)

def edit_telegram_message(chat_id, message_id, text, token, reply_markup=None):
    url = f"https://api.telegram.org/bot{token}/editMessageText"
    payload = {'chat_id': chat_id, 'message_id': message_id, 'text': text, 'parse_mode': 'Markdown'}
    if reply_markup: payload['reply_markup'] = reply_markup
    requests.post(url, json=payload)

def answer_callback_query(callback_query_id, token):
    url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
    payload = {'callback_query_id': callback_query_id}
    requests.post(url, json=payload)

# --- WEB SERVER VỚI FLASK (Cập nhật logic xử lý) ---
app = Flask(__name__)

@app.route('/', methods=['POST'])
def webhook():
    BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
    if not BOT_TOKEN: return "Bot token not configured", 500

    data = request.get_json()
    
    # XỬ LÝ TIN NHẮN THÔNG THƯỜNG
    if "message" in data and "text" in data["message"]:
        chat_id = data["message"]["chat"]["id"]
        message_id = data["message"]["message_id"]
        message_text = data["message"]["text"].strip()
        
        # --- LOGIC ĐIỀU KHIỂN BOT --- ### <<< THAY ĐỔI
        
        # Lệnh /start hoặc /sta để BẬT bot
        if message_text == "/start" or message_text == "/sta":
            set_user_state(chat_id, True)
            start_message = (
                "✅ *Bot đã được bật.*\n\n"
                "Gửi cho tôi danh sách token của bạn để tính toán.\n"
                "Sử dụng cú pháp sau (mỗi token một dòng):\n"
                "`[số lượng] [địa chỉ contract] [mạng]`\n\n"
                "Ví dụ:\n"
                "```\n"
                "357 ...fa bsc\n"
                "0.5 ...eee eth\n"
                "```\n"
                "Gõ /sto để tạm dừng bot."
            )
            send_telegram_message(chat_id, start_message, BOT_TOKEN)
            
        # Lệnh /sto để TẮT bot
        elif message_text == "/sto":
            set_user_state(chat_id, False)
            stop_message = "☑️ *Bot đã được tắt.* Mọi tin nhắn sẽ được bỏ qua.\n\nGõ /sta để bật lại."
            send_telegram_message(chat_id, stop_message, BOT_TOKEN)
            
        # Xử lý các tin nhắn khác CHỈ KHI bot đang BẬT
        else:
            if is_user_active(chat_id):
                # Gửi tin nhắn tạm thời báo đang tính
                send_telegram_message(chat_id, "Đang tính toán, vui lòng chờ...", BOT_TOKEN)
                
                # Xử lý tính toán portfolio
                result_text = process_portfolio_text(message_text)
                
                # Tạo nút và gửi kết quả
                refresh_button_markup = create_refresh_button()
                send_telegram_message(
                    chat_id, 
                    result_text, 
                    BOT_TOKEN, 
                    reply_to_message_id=message_id,
                    reply_markup=refresh_button_markup
                )
            # Nếu bot đang TẮT, nó sẽ không làm gì cả, bỏ qua tin nhắn.
            
    # XỬ LÝ KHI NGƯỜI DÙNG NHẤN NÚT REFRESH (Không thay đổi)
    elif "callback_query" in data:
        callback_query = data["callback_query"]
        callback_id = callback_query["id"]
        answer_callback_query(callback_id, BOT_TOKEN)
        
        if callback_query.get("data") == "refresh_portfolio":
            chat_id = callback_query["message"]["chat"]["id"]
            message_id_to_edit = callback_query["message"]["message_id"]
            
            if "reply_to_message" in callback_query["message"]:
                original_message_text = callback_query["message"]["reply_to_message"]["text"]
                new_result_text = process_portfolio_text(original_message_text)
                refresh_button_markup = create_refresh_button()
                edit_telegram_message(
                    chat_id, 
                    message_id_to_edit, 
                    new_result_text, 
                    BOT_TOKEN, 
                    reply_markup=refresh_button_markup
                )
            else:
                edit_telegram_message(chat_id, message_id_to_edit, "Lỗi: Không tìm thấy tin nhắn gốc để làm mới.", BOT_TOKEN)

    return jsonify(success=True)