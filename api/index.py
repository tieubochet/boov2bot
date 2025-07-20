import os
import json
import requests
from flask import Flask, request, jsonify

# --- LOGIC QUẢN LÝ TRẠNG THÁI NGƯỜI DÙNG (Không thay đổi) ---
STATE_FILE_PATH = '/tmp/bot_user_states.json'

def load_user_states():
    if not os.path.exists(STATE_FILE_PATH):
        return {}
    try:
        with open(STATE_FILE_PATH, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return {}

def save_user_states(states):
    os.makedirs(os.path.dirname(STATE_FILE_PATH), exist_ok=True)
    with open(STATE_FILE_PATH, 'w') as f:
        json.dump(states, f)

def set_user_state(chat_id, is_active: bool):
    chat_id_str = str(chat_id)
    states = load_user_states()
    states[chat_id_str] = is_active
    save_user_states(states)

def is_user_active(chat_id):
    chat_id_str = str(chat_id)
    states = load_user_states()
    return states.get(chat_id_str, False)


# --- LOGIC LẤY DỮ LIỆU TỪ API (Cập nhật) ---

def get_token_price(network: str, token_address: str) -> tuple[float, str] | None:
    """Hàm này chỉ lấy giá và symbol, dùng cho portfolio."""
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

### <<< THÊM MỚI
def get_full_token_info(network: str, token_address: str) -> dict | None:
    """Lấy thông tin chi tiết của một token."""
    # Thêm `include=top_pools` để lấy thông tin về các cặp giao dịch hàng đầu
    url = f"https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{token_address}?include=top_pools"
    try:
        response = requests.get(url, headers={"accept": "application/json"})
        if response.status_code != 200:
            print(f"GeckoTerminal API error for {network}/{token_address}: Status {response.status_code}")
            return None
            
        response_data = response.json()
        token_data = response_data.get('data', {}).get('attributes', {})
        if not token_data:
            return None

        # Xử lý để tìm tên DEX từ `included` data
        top_dex_name = "N/A"
        included_data = response_data.get('included', [])
        # Tạo một map để dễ dàng tra cứu thông tin từ 'included'
        included_map = {item['id']: item for item in included_data}
        
        # Tìm pool hàng đầu
        top_pools = response_data.get('data', {}).get('relationships', {}).get('top_pools', {}).get('data', [])
        if top_pools:
            top_pool_id = top_pools[0]['id']
            pool_info = included_map.get(top_pool_id)
            if pool_info:
                dex_id = pool_info.get('relationships', {}).get('dex', {}).get('data', {}).get('id')
                dex_info = included_map.get(dex_id)
                if dex_info:
                    top_dex_name = dex_info.get('attributes', {}).get('name')

        return {
            "name": token_data.get('name'),
            "symbol": token_data.get('symbol'),
            "price_usd": token_data.get('price_usd'),
            "price_change_24h": token_data.get('price_change_percentage', {}).get('h24'),
            "address": token_data.get('address'),
            "gecko_terminal_link": f"https://www.geckoterminal.com/{network}/tokens/{token_address}",
            "top_dex_name": top_dex_name
        }

    except Exception as e:
        print(f"Error calling or parsing GeckoTerminal API for full info: {e}")
        return None

# --- LOGIC XỬ LÝ TIN NHẮN (Cập nhật) ---

def process_portfolio_text(message_text: str) -> str:
    """Xử lý tin nhắn tính toán portfolio (Không thay đổi)."""
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

### <<< THÊM MỚI
def process_token_check_command(message_text: str) -> str:
    """Xử lý lệnh /check để tra cứu thông tin token."""
    parts = message_text.strip().split()
    if len(parts) != 3:
        return (
            "❌ *Cú pháp không hợp lệ.*\n"
            "Sử dụng: `/check [địa chỉ contract] [mạng]`\n"
            "Ví dụ: `/check 0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c bsc`"
        )
    
    _command, address, network = parts
    
    info = get_full_token_info(network.lower(), address.lower())
    
    if not info:
        return f"❌ Không tìm thấy thông tin cho token `{address[:10]}...` trên mạng `{network}`."
        
    price_str = "N/A"
    if info.get('price_usd'):
        price_str = f"${float(info['price_usd']):,.8f}" # Hiển thị nhiều số lẻ hơn cho giá token

    price_change_str = "N/A"
    if info.get('price_change_24h'):
        change = float(info['price_change_24h'])
        emoji = "📈" if change >= 0 else "📉"
        price_change_str = f"{emoji} {change:+.2f}%"

    # Định dạng tin nhắn trả về
    result = (
        f"*{info.get('name', 'N/A')} ({info.get('symbol', 'N/A')})*\n\n"
        f"Giá: *{price_str}*\n"
        f"24h: *{price_change_str}*\n"
        f"Mạng: `{network.upper()}`\n"
        f"Sàn DEX chính: `{info.get('top_dex_name', 'N/A')}`\n\n"
        f"🔗 [Xem trên GeckoTerminal]({info.get('gecko_terminal_link')})\n\n"
        f"`{info.get('address')}`"
    )
    return result


# --- HÀM GỬI/CHỈNH SỬA TIN NHẮN TELEGRAM (Không thay đổi) ---
def create_refresh_button():
    keyboard = {'inline_keyboard': [[{'text': '🔄 Refresh', 'callback_data': 'refresh_portfolio'}]]}
    return json.dumps(keyboard)

def send_telegram_message(chat_id, text, token, reply_to_message_id=None, reply_markup=None, disable_web_page_preview=False):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        'chat_id': chat_id, 
        'text': text, 
        'parse_mode': 'Markdown',
        'disable_web_page_preview': disable_web_page_preview
    }
    if reply_to_message_id: payload['reply_to_message_id'] = reply_to_message_id
    if reply_markup: payload['reply_markup'] = reply_markup
    requests.post(url, json=payload)

def edit_telegram_message(chat_id, message_id, text, token, reply_markup=None, disable_web_page_preview=False):
    url = f"https://api.telegram.org/bot{token}/editMessageText"
    payload = {
        'chat_id': chat_id, 
        'message_id': message_id, 
        'text': text, 
        'parse_mode': 'Markdown',
        'disable_web_page_preview': disable_web_page_preview
    }
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
        
        # --- LOGIC ĐIỀU KHIỂN BOT --- ### <<< CẬP NHẬT
        
        # Lệnh /start hoặc /sta để BẬT bot
        if message_text == "/start" or message_text == "/sta":
            set_user_state(chat_id, True)
            ### <<< CẬP NHẬT TIN NHẮN HƯỚNG DẪN
            start_message = (
                "✅ *Bot đã được bật.*\n\n"
                "1️⃣ *Tính toán Portfolio:*\n"
                "Gửi danh sách token theo cú pháp (mỗi token một dòng):\n"
                "`[số lượng] [địa chỉ contract] [mạng]`\n"
                "Ví dụ:\n"
                "```\n"
                "357 ...fa bsc\n"
                "0.5 ...eee eth\n"
                "```\n\n"
                "2️⃣ *Kiểm tra một Token:*\n"
                "Sử dụng lệnh `/check [địa chỉ] [mạng]`\n"
                "Ví dụ:\n"
                "`/check 0x...95c bsc`\n\n"
                "Gõ /sto để tạm dừng bot."
            )
            send_telegram_message(chat_id, start_message, BOT_TOKEN)
            
        # Lệnh /sto để TẮT bot
        elif message_text == "/sto":
            set_user_state(chat_id, False)
            stop_message = "☑️ *Bot đã được tắt.* Mọi tin nhắn (trừ lệnh) sẽ được bỏ qua.\n\nGõ /sta để bật lại."
            send_telegram_message(chat_id, stop_message, BOT_TOKEN)

        ### <<< THÊM MỚI: Xử lý lệnh /check
        elif message_text.startswith('/check '):
            # Lệnh này hoạt động ngay cả khi bot đang "tắt"
            result_text = process_token_check_command(message_text)
            # Tắt preview link để tin nhắn gọn gàng hơn
            send_telegram_message(chat_id, result_text, BOT_TOKEN, disable_web_page_preview=True)
            
        # Xử lý các tin nhắn khác CHỈ KHI bot đang BẬT
        else:
            if is_user_active(chat_id):
                send_telegram_message(chat_id, "Đang tính toán portfolio, vui lòng chờ...", BOT_TOKEN, reply_to_message_id=message_id)
                result_text = process_portfolio_text(message_text)
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

# Lệnh để chạy cục bộ (tùy chọn, không cần thiết cho production trên serverless)
# if __name__ == '__main__':
#     # Đảm bảo bạn đã đặt biến môi trường TELEGRAM_TOKEN
#     # export TELEGRAM_TOKEN="your_bot_token_here"
#     app.run(debug=True, port=5001)