import os
import json
import requests
from flask import Flask, request, jsonify

# --- CẤU HÌNH --- ### <<< THÊM MỚI
# Danh sách các mạng để bot tự động quét khi nhận được địa chỉ contract
# Sắp xếp theo thứ tự ưu tiên (bot sẽ dừng lại ở mạng đầu tiên tìm thấy)
AUTO_SEARCH_NETWORKS = ['bsc', 'eth', 'polygon', 'arbitrum', 'base']

# --- LOGIC QUẢN LÝ TRẠNG THÁI NGƯỜI DÙNG (Không thay đổi) ---
STATE_FILE_PATH = '/tmp/bot_user_states.json'

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
    return load_user_states().get(str(chat_id), False)

# --- LOGIC LẤY DỮ LIỆU TỪ API (Không thay đổi) ---
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
            "network": network, # Trả về cả network đã tìm thấy
            "name": token_data.get('name'), "symbol": token_data.get('symbol'),
            "price_usd": token_data.get('price_usd'),
            "price_change_24h": token_data.get('price_change_percentage', {}).get('h24'),
            "address": token_data.get('address'),
            "gecko_terminal_link": f"https://www.geckoterminal.com/{network}/tokens/{token_address}",
            "top_dex_name": top_dex_name
        }
    except Exception: return None

# --- LOGIC XỬ LÝ TIN NHẮN (Cập nhật) ---

def format_token_info_message(info: dict) -> str:
    """Định dạng thông tin token thành tin nhắn trả về."""
    network = info.get('network', 'N/A')
    price_str = f"${float(info['price_usd']):,.8f}" if info.get('price_usd') else "N/A"
    price_change_str = "N/A"
    if info.get('price_change_24h'):
        change = float(info['price_change_24h'])
        emoji = "📈" if change >= 0 else "📉"
        price_change_str = f"{emoji} {change:+.2f}%"
    result = (
        f"✅ *Tìm thấy trên mạng {network.upper()}*\n"
        f"*{info.get('name', 'N/A')} ({info.get('symbol', 'N/A')})*\n\n"
        f"Giá: *{price_str}*\n"
        f"24h: *{price_change_str}*\n"
        f"Sàn DEX chính: `{info.get('top_dex_name', 'N/A')}`\n\n"
        f"🔗 [Xem trên GeckoTerminal]({info.get('gecko_terminal_link')})\n\n"
        f"`{info.get('address')}`"
    )
    return result

### <<< THÊM MỚI
def find_token_across_networks(address: str) -> str:
    """Quét địa chỉ contract qua nhiều mạng và trả về kết quả đầu tiên."""
    for network in AUTO_SEARCH_NETWORKS:
        print(f"Searching for {address} on {network}...")
        info = get_full_token_info(network, address.lower())
        if info:
            # Tìm thấy! Định dạng và trả về kết quả ngay lập tức.
            return format_token_info_message(info)
    
    # Nếu vòng lặp kết thúc mà không tìm thấy
    return f"❌ Không tìm thấy token với địa chỉ `{address[:10]}...` trên các mạng được quét: `{'`, `'.join(AUTO_SEARCH_NETWORKS)}`."

def process_portfolio_text(message_text: str) -> str | None:
    """
    Xử lý tin nhắn tính toán portfolio.
    Trả về None nếu không có dòng nào hợp lệ để phân biệt với lỗi cú pháp.
    """
    lines = message_text.strip().split('\n')
    total_value = 0.0
    result_lines = []
    valid_lines_count = 0

    for i, line in enumerate(lines):
        parts = line.strip().split()
        if len(parts) != 3:
            continue # Bỏ qua các dòng không đúng cú pháp

        amount_str, address, network = parts
        try:
            amount = float(amount_str)
            if not is_evm_address(address): # Kiểm tra địa chỉ hợp lệ
                 result_lines.append(f"Dòng {i+1}: ❌ Địa chỉ không hợp lệ.")
                 continue
        except ValueError:
            # Nếu phần đầu không phải là số, đây không phải là dòng portfolio
            continue

        valid_lines_count += 1
        price_data = get_token_price(network.lower(), address.lower())
        if price_data:
            price, symbol = price_data
            value = amount * price
            total_value += value
            result_lines.append(f"*{symbol}*: ${price:,.4f} x {amount_str} = *${value:,.2f}*")
        else:
            result_lines.append(f"❌ Không tìm thấy giá cho `{address[:10]}...` trên `{network}`.")
    
    if valid_lines_count == 0:
        return None # Không có dòng nào hợp lệ, có thể đây là tin nhắn khác

    final_result_text = "\n".join(result_lines)
    summary = f"\n--------------------\n*Tổng cộng: *${total_value:,.2f}**"
    return final_result_text + summary

# --- CÁC HÀM TIỆN ÍCH ---
def is_evm_address(address_str: str) -> bool:
    return isinstance(address_str, str) and address_str.startswith('0x') and len(address_str) == 42

# --- HÀM GỬI/CHỈNH SỬA TIN NHẮN TELEGRAM (Không thay đổi) ---
def create_refresh_button():
    return json.dumps({'inline_keyboard': [[{'text': '🔄 Refresh', 'callback_data': 'refresh_portfolio'}]]})

def send_telegram_message(chat_id, text, token, reply_to_message_id=None, reply_markup=None, disable_web_page_preview=False):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown', 'disable_web_page_preview': disable_web_page_preview}
    if reply_to_message_id: payload['reply_to_message_id'] = reply_to_message_id
    if reply_markup: payload['reply_markup'] = reply_markup
    requests.post(url, json=payload)

def edit_telegram_message(chat_id, message_id, text, token, reply_markup=None, disable_web_page_preview=False):
    #... (Giữ nguyên code)
    url = f"https://api.telegram.org/bot{token}/editMessageText"
    payload = {'chat_id': chat_id, 'message_id': message_id, 'text': text, 'parse_mode': 'Markdown', 'disable_web_page_preview': disable_web_page_preview}
    if reply_markup: payload['reply_markup'] = reply_markup
    requests.post(url, json=payload)

def answer_callback_query(callback_query_id, token):
    #... (Giữ nguyên code)
    url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
    payload = {'callback_query_id': callback_query_id}
    requests.post(url, json=payload)

# --- WEB SERVER VỚI FLASK (Logic xử lý được viết lại hoàn toàn) ---
app = Flask(__name__)

@app.route('/', methods=['POST'])
def webhook():
    BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
    if not BOT_TOKEN: return "Bot token not configured", 500

    data = request.get_json()
    
    # Xử lý callback query (Nút Refresh)
    if "callback_query" in data:
        # ... (giữ nguyên logic xử lý callback)
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
                "1️⃣ *Tra cứu Token:*\n"
                "Gửi một địa chỉ contract duy nhất. Bot sẽ tự động tìm kiếm trên các mạng phổ biến.\n"
                "Ví dụ: `0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c`\n\n"
                "2️⃣ *Tính toán Portfolio:*\n"
                "Gửi danh sách token theo cú pháp (mỗi token một dòng):\n"
                "`[số lượng] [địa chỉ contract] [mạng]`\n\n"
                "Gõ /sto để tạm dừng bot."
            )
            send_telegram_message(chat_id, start_message, BOT_TOKEN)
            return jsonify(success=True)
            
        elif message_text.lower() == "/sto":
            set_user_state(chat_id, False)
            stop_message = "☑️ *Bot đã được tắt.* Mọi tin nhắn (trừ lệnh) sẽ được bỏ qua.\n\nGõ /sta để bật lại."
            send_telegram_message(chat_id, stop_message, BOT_TOKEN)
            return jsonify(success=True)
            
        # 2. XỬ LÝ CÁC TIN NHẮN KHÁC (CHỈ KHI BOT ĐANG BẬT)
        if is_user_active(chat_id):
            # Ưu tiên 1: Kiểm tra xem có phải là một địa chỉ contract duy nhất không
            parts = message_text.split()
            if len(parts) == 1 and is_evm_address(parts[0]):
                address = parts[0]
                send_telegram_message(chat_id, f"🔍 Đang tìm kiếm địa chỉ `{address[:10]}...`", BOT_TOKEN, reply_to_message_id=message_id)
                result_text = find_token_across_networks(address)
                # Dùng edit thay vì gửi mới để tránh spam
                edit_telegram_message(chat_id, message_id + 1, result_text, BOT_TOKEN, disable_web_page_preview=True)
            else:
                # Ưu tiên 2: Thử xử lý như một portfolio
                portfolio_result = process_portfolio_text(message_text)
                if portfolio_result:
                    send_telegram_message(chat_id, "Đang tính toán portfolio...", BOT_TOKEN, reply_to_message_id=message_id)
                    refresh_button_markup = create_refresh_button()
                    # Dùng edit thay vì gửi mới
                    edit_telegram_message(chat_id, message_id + 1, portfolio_result, BOT_TOKEN, reply_markup=refresh_button_markup)
                #else:
                    # Nếu cả hai đều không thành công -> Gửi hướng dẫn
                    # error_message = (
                    #    "🤔 *Cú pháp không hợp lệ.*\n\n"
                    #    "Vui lòng thử một trong hai cách sau:\n\n"
                    #    "1️⃣ *Để tra cứu Token:*\n"
                    #    "Gửi một địa chỉ contract duy nhất.\n\n"
                    #    "2️⃣ *Để tính Portfolio:*\n"
                    #    "Gửi danh sách theo cú pháp:\n`số_lượng địa_chỉ mạng`"
                    # )
                    #send_telegram_message(chat_id, error_message, BOT_TOKEN, reply_to_message_id=message_id)
        
    return jsonify(success=True)