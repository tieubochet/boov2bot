import os
import json
import requests
from flask import Flask, request, jsonify

# --- LOGIC LẤY GIÁ (Không thay đổi) ---
def get_token_price(network: str, token_address: str) -> tuple[float, str] | None:
    """
    Lấy giá và ký hiệu của một token từ API GeckoTerminal.
    Trả về một tuple (price, symbol) nếu thành công, None nếu có lỗi.
    """
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
    """Xử lý văn bản đầu vào và trả về chuỗi kết quả theo định dạng mới."""
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
            result_lines.append(
                f"*{symbol}*: ${price:,.4f} x {amount_str} = *${value:,.2f}*"
            )
        else:
            result_lines.append(f"❌ Không tìm thấy giá cho `{address[:10]}...` trên `{network}`.")

    final_result_text = "\n".join(result_lines)
    summary = f"\n--------------------\n*Tổng cộng: *${total_value:,.2f}**"
    
    return final_result_text + summary

# --- HÀM GỬI/CHỈNH SỬA TIN NHẮN TELEGRAM (Cập nhật và thêm mới) ---

### <<< THAY ĐỔI: Hàm tạo nút bấm ---
def create_refresh_button():
    """Tạo đối tượng inline keyboard cho nút Refresh."""
    keyboard = {
        'inline_keyboard': [[
            {'text': '🔄 Refresh', 'callback_data': 'refresh_portfolio'}
        ]]
    }
    return json.dumps(keyboard)

### <<< THAY ĐỔI: Cập nhật hàm send_telegram_message để có thể reply và thêm nút ---
def send_telegram_message(chat_id, text, token, reply_to_message_id=None, reply_markup=None):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'Markdown'
    }
    if reply_to_message_id:
        payload['reply_to_message_id'] = reply_to_message_id
    if reply_markup:
        payload['reply_markup'] = reply_markup
        
    requests.post(url, json=payload)

### <<< THAY ĐỔI: Thêm hàm mới để chỉnh sửa tin nhắn ---
def edit_telegram_message(chat_id, message_id, text, token, reply_markup=None):
    url = f"https://api.telegram.org/bot{token}/editMessageText"
    payload = {
        'chat_id': chat_id,
        'message_id': message_id,
        'text': text,
        'parse_mode': 'Markdown'
    }
    if reply_markup:
        payload['reply_markup'] = reply_markup
        
    requests.post(url, json=payload)
    
### <<< THAY ĐỔI: Thêm hàm để xác nhận đã xử lý callback ---
def answer_callback_query(callback_query_id, token):
    """Gửi xác nhận cho Telegram để dừng icon loading trên nút bấm."""
    url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
    payload = {'callback_query_id': callback_query_id}
    requests.post(url, json=payload)


# --- WEB SERVER VỚI FLASK (Cập nhật để xử lý callback) ---
app = Flask(__name__)

@app.route('/', methods=['POST'])
def webhook():
    BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
    if not BOT_TOKEN:
        return "Bot token not configured", 500

    data = request.get_json()
    
    # --- XỬ LÝ TIN NHẮN THÔNG THƯỜNG ---
    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        message_id = data["message"]["message_id"] # Lấy message_id để reply
        message_text = data["message"]["text"]
        
        if message_text.strip() == "/start":
            start_message = (
                "Gửi cho tôi danh sách token của bạn để tính toán tổng giá trị.\n\n"
                "Sử dụng cú pháp sau (mỗi token một dòng):\n"
                "`[số lượng] [địa chỉ contract] [mạng]`\n\n"
                "Ví dụ:\n"
                "```\n"
                "357 0x22b1458e780f8fa71e2f84502cee8b5a3cc731fa bsc\n"
                "0.5 0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee eth\n"
                "```"
            )
            send_telegram_message(chat_id, start_message, BOT_TOKEN)
        else:
            # Gửi tin nhắn tạm thời báo đang tính
            send_telegram_message(chat_id, "Đang tính toán, vui lòng chờ...", BOT_TOKEN)
            
            # Xử lý tính toán portfolio
            result_text = process_portfolio_text(message_text)
            
            # ### <<< THAY ĐỔI: Tạo nút và gửi kết quả kèm theo nút refresh
            refresh_button_markup = create_refresh_button()
            send_telegram_message(
                chat_id, 
                result_text, 
                BOT_TOKEN, 
                reply_to_message_id=message_id, # Reply lại tin nhắn gốc
                reply_markup=refresh_button_markup
            )
            
    # ### <<< THAY ĐỔI: XỬ LÝ KHI NGƯỜI DÙNG NHẤN NÚT ---
    elif "callback_query" in data:
        callback_query = data["callback_query"]
        callback_id = callback_query["id"]
        
        # Luôn gửi answer callback để UI trên Telegram mượt mà
        answer_callback_query(callback_id, BOT_TOKEN)
        
        # Kiểm tra xem có phải lệnh refresh không
        if callback_query.get("data") == "refresh_portfolio":
            chat_id = callback_query["message"]["chat"]["id"]
            message_id_to_edit = callback_query["message"]["message_id"]
            
            # Kiểm tra xem tin nhắn có reply không để lấy text gốc
            if "reply_to_message" in callback_query["message"]:
                original_message_text = callback_query["message"]["reply_to_message"]["text"]
                
                # Tính toán lại
                new_result_text = process_portfolio_text(original_message_text)
                
                # Tạo lại nút
                refresh_button_markup = create_refresh_button()
                
                # Chỉnh sửa tin nhắn cũ với kết quả mới
                edit_telegram_message(
                    chat_id, 
                    message_id_to_edit, 
                    new_result_text, 
                    BOT_TOKEN, 
                    reply_markup=refresh_button_markup
                )
            else:
                # Trường hợp không tìm thấy tin nhắn gốc (dự phòng)
                edit_telegram_message(chat_id, message_id_to_edit, "Lỗi: Không tìm thấy tin nhắn gốc để làm mới.", BOT_TOKEN)

    return jsonify(success=True)

# Vercel sẽ tự động tìm và chạy đối tượng `app` này.