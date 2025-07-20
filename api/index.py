import os
import json
import requests
from flask import Flask, request, jsonify

# --- CẤU HÌNH ---
# Danh sách các mạng để bot tự động quét
AUTO_SEARCH_NETWORKS = ['bsc', 'eth', 'polygon', 'arbitrum', 'base']

### <<< THAY ĐỔI LỚN: Loại bỏ State Management ###
# Các hàm load_user_states, save_user_states, set_user_state, is_user_active
# đã được xóa bỏ hoàn toàn để đảm bảo tương thích với môi trường serverless (Vercel).
# Bot bây giờ sẽ luôn ở trạng thái "bật".

# --- LOGIC LẤY DỮ LIỆU TỪ API (Không thay đổi) ---
def get_token_price(network: str, token_address: str) -> tuple[float, str] | None:
    url = f"https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{token_address}"
    try:
        response = requests.get(url, headers={"accept": "application/json"}, timeout=5)
        if response.status_code != 200: return None
        data = response.json()
        attributes = data.get('data', {}).get('attributes', {})
        price_usd_str = attributes.get('price_usd')
        symbol = attributes.get('symbol', 'N/A')
        if price_usd_str: return (float(price_usd_str), symbol)
        return None
    except requests.RequestException: return None

def get_full_token_info(network: str, token_address: str) -> dict | None:
    url = f"https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{token_address}?include=top_pools"
    try:
        response = requests.get(url, headers={"accept": "application/json"}, timeout=10)
        if response.status_code != 200: return None
        response_data = response.json()
        token_data = response_data.get('data', {}).get('attributes', {})
        if not token_data: return None

        return {
            "network": network,
            "name": token_data.get('name'), "symbol": token_data.get('symbol'),
            "price_usd": token_data.get('price_usd'),
            "price_change_24h": token_data.get('price_change_percentage', {}).get('h24'),
            "address": token_data.get('address'),
            "gecko_terminal_link": f"https://www.geckoterminal.com/{network}/tokens/{token_address}"
        }
    except requests.RequestException: return None

# --- LOGIC XỬ LÝ TIN NHẮN (Không thay đổi) ---
def format_token_info_message(info: dict) -> str:
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
        f"24h: *{price_change_str}*\n\n"
        f"🔗 [Xem trên GeckoTerminal]({info.get('gecko_terminal_link')})\n\n"
        f"`{info.get('address')}`"
    )
    return result

def find_token_across_networks(address: str) -> str:
    for network in AUTO_SEARCH_NETWORKS:
        info = get_full_token_info(network, address.lower())
        if info:
            return format_token_info_message(info)
    return f"❌ Không tìm thấy token với địa chỉ `{address[:10]}...` trên các mạng đã quét."

def process_portfolio_text(message_text: str) -> str | None:
    lines = message_text.strip().split('\n')
    total_value, result_lines, valid_lines_count = 0.0, [], 0

    for line in lines:
        parts = line.strip().split()
        if len(parts) != 3: continue

        try:
            amount = float(parts[0])
            if not is_evm_address(parts[1]): continue
        except ValueError: continue

        valid_lines_count += 1
        amount_str, address, network = parts
        price_data = get_token_price(network.lower(), address.lower())
        if price_data:
            price, symbol = price_data
            value = amount * price
            total_value += value
            result_lines.append(f"*{symbol}*: ${price:,.4f} x {amount_str} = *${value:,.2f}*")
        else:
            result_lines.append(f"❌ Không tìm thấy giá cho `{address[:10]}...` trên `{network}`.")
    
    if valid_lines_count == 0: return None

    return "\n".join(result_lines) + f"\n--------------------\n*Tổng cộng: *${total_value:,.2f}**"

# --- CÁC HÀM TIỆN ÍCH ---
def is_evm_address(address_str: str) -> bool:
    return isinstance(address_str, str) and address_str.startswith('0x') and len(address_str) == 42

# --- HÀM GỬI/CHỈNH SỬA TIN NHẮN TELEGRAM ---
def create_refresh_button():
    return json.dumps({'inline_keyboard': [[{'text': '🔄 Refresh', 'callback_data': 'refresh_portfolio'}]]})

def send_telegram_message(chat_id, text, token, **kwargs):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown', **kwargs}
    try:
        requests.post(url, json=payload, timeout=10)
    except requests.RequestException as e:
        print(f"Error sending message: {e}")

def edit_telegram_message(chat_id, message_id, text, token, **kwargs):
    url = f"https://api.telegram.org/bot{token}/editMessageText"
    payload = {'chat_id': chat_id, 'message_id': message_id, 'text': text, 'parse_mode': 'Markdown', **kwargs}
    try:
        requests.post(url, json=payload, timeout=10)
    except requests.RequestException as e:
        print(f"Error editing message: {e}")

def answer_callback_query(callback_query_id, token):
    url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
    try:
        requests.post(url, json={'callback_query_id': callback_query_id}, timeout=5)
    except requests.RequestException as e:
        print(f"Error answering callback query: {e}")

# --- WEB SERVER VỚI FLASK (Đã được đơn giản hóa) ---
app = Flask(__name__)

@app.route('/', methods=['POST'])
def webhook():
    BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
    if not BOT_TOKEN:
        print("FATAL: TELEGRAM_TOKEN not configured.")
        return "Bot token not configured", 500

    data = request.get_json()
    
    # Xử lý callback query (Nút Refresh)
    if "callback_query" in data:
        callback_query = data["callback_query"]
        answer_callback_query(callback_query["id"], BOT_TOKEN)
        
        if callback_query.get("data") == "refresh_portfolio":
            chat_id = callback_query["message"]["chat"]["id"]
            message_id_to_edit = callback_query["message"]["message_id"]
            
            if "reply_to_message" in callback_query["message"]:
                original_message_text = callback_query["message"]["reply_to_message"]["text"]
                portfolio_result = process_portfolio_text(original_message_text)
                if portfolio_result:
                    refresh_button_markup = create_refresh_button()
                    edit_telegram_message(chat_id, message_id_to_edit, portfolio_result, BOT_TOKEN, reply_markup=refresh_button_markup, disable_web_page_preview=True)
            else:
                edit_telegram_message(chat_id, message_id_to_edit, "Lỗi: Không tìm thấy tin nhắn gốc.", BOT_TOKEN)
        return jsonify(success=True)

    # Xử lý tin nhắn văn bản
    if "message" in data and "text" in data["message"]:
        chat_id = data["message"]["chat"]["id"]
        message_id = data["message"]["message_id"]
        message_text = data["message"]["text"].strip()
        
        # 1. Xử lý lệnh /start để nhận hướng dẫn
        if message_text.lower() == "/start":
            start_message = (
                "Chào mừng bạn đến với Bot!\n\n"
                "1️⃣ *Tra cứu Token:*\n"
                "Gửi một địa chỉ contract duy nhất. Bot sẽ tự động tìm kiếm trên các mạng phổ biến.\n"
                "Ví dụ: `0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c`\n\n"
                "2️⃣ *Tính toán Portfolio:*\n"
                "Gửi danh sách token theo cú pháp (mỗi token một dòng):\n"
                "`[số lượng] [địa chỉ contract] [mạng]`"
            )
            send_telegram_message(chat_id, start_message, BOT_TOKEN)
            return jsonify(success=True)
            
        # 2. Xử lý các tin nhắn khác
        parts = message_text.split()
        
        # Ưu tiên 1: Kiểm tra xem có phải là một địa chỉ contract duy nhất không
        if len(parts) == 1 and is_evm_address(parts[0]):
            address = parts[0]
            result_text = find_token_across_networks(address)
            send_telegram_message(chat_id, result_text, BOT_TOKEN, reply_to_message_id=message_id, disable_web_page_preview=True)
        else:
            # Ưu tiên 2: Thử xử lý như một portfolio
            portfolio_result = process_portfolio_text(message_text)
            if portfolio_result:
                refresh_button_markup = create_refresh_button()
                send_telegram_message(chat_id, portfolio_result, BOT_TOKEN, reply_to_message_id=message_id, reply_markup=refresh_button_markup)
            else:
                # Nếu cả hai đều không thành công -> Gửi hướng dẫn lỗi
                error_message = "🤔 *Cú pháp không hợp lệ.*\n\nGửi /start để xem hướng dẫn."
                send_telegram_message(chat_id, error_message, BOT_TOKEN, reply_to_message_id=message_id)
        
    return jsonify(success=True)