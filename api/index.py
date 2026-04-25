import os
import json
import requests
import hashlib
import hmac
from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import pytz
from redis import Redis
from openai import OpenAI  # <--- THAY ĐỔI: Import OpenAI

# --- CẤU HÌNH ---
AUTO_SEARCH_NETWORKS = ['bsc', 'eth', 'tron', 'polygon', 'arbitrum', 'base']
TIMEZONE = pytz.timezone('Asia/Ho_Chi_Minh')
CHINA_TIMEZONE = pytz.timezone('Asia/Shanghai')
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
CRON_SECRET = os.getenv("CRON_SECRET")
REMINDER_THRESHOLD_MINUTES = 5
SYMBOL_TO_ID_MAP = {
    'btc': 'bitcoin', 'eth': 'ethereum', 'bnb': 'binancecoin', 'sol': 'solana',
    'xrp': 'ripple', 'doge': 'dogecoin', 'shib': 'shiba-inu', 'degen': 'degen-base',
    'sui': 'sui', 'dev': 'scout-protocol-token', 'hype':'hyperliquid', 'link': 'chainlink',
    'ondo':'ondo-finance', 'virtual':'virtual-protocol', 'trx':'tron', 'towns':'towns',
    'in': 'infinit', 'yala': 'yala', 'vra':'verasity', 'tipn':'tipn', 'era':'caldera',
    'talent':'talent-protocol', 'bas':'bas', 'ron':'ronin', 'dolo':'dolomite', 'wod':'world-of-dypians',
    'zent':'zentry','wod':'world-of-dypians', 'open':'openledger-2', 'mirror':'black-mirror',
    'wct':'connect-token-wct', 'stbl':'stbl', 'synd':'syndicate-3', 'mira':'mira-3', 'ff':'falcon-finance-ff',
    'xan':'anoma', 'vang':'pax-gold', 'bless':'bless-2', 'bank':'lorenzo-protocol'
}

# --- CẤU HÌNH GROQ (Dùng thư viện OpenAI nhưng trỏ về server Groq) ---
# Import: from openai import OpenAI (Giữ nguyên dòng này ở đầu file)

# Đổi tên biến môi trường thành GROQ_API_KEY cho rõ ràng
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
openai_client = None

if GROQ_API_KEY:
    try:
        # Cấu hình Client trỏ về Groq
        openai_client = OpenAI(
            base_url="https://api.groq.com/openai/v1",
            api_key=GROQ_API_KEY
        )
    except Exception as e:
        print(f"Error configuring Groq: {e}")
else:
    print("Warning: GROQ_API_KEY is not set.")
# ----------------------------------->

# --- KẾT NỐI CƠ SỞ DỮ LIỆU ---
try:
    kv_url = os.getenv("teeboov2_REDIS_URL")
    if not kv_url: raise ValueError("teeboov2_REDIS_URL is not set.")
    kv = Redis.from_url(kv_url, decode_responses=True)
except Exception as e:
    print(f"FATAL: Could not connect to Redis. Error: {e}"); kv = None

# --- LOGIC QUẢN LÝ CÔNG VIỆC ---
def _get_processed_airdrop_events():
    """
    Hàm nội bộ: Lấy và xử lý dữ liệu airdrop, trả về danh sách các sự kiện
    đã được lọc với thời gian hiệu lực đã được tính toán.
    Đây là hàm logic cốt lõi.
    """
    AIRDROP_API_URL = "https://alpha123.uk/api/data?fresh=1"
    PRICE_API_URL = "https://alpha123.uk/api/price/?batch=today"
    HEADERS = {
      'referer': 'https://alpha123.uk/index.html',
      'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }

    def _get_price_data():
        try:
            res = requests.get(PRICE_API_URL, headers=HEADERS, timeout=10)
            if res.status_code == 200:
                price_json = res.json()
                if price_json.get('success') and 'prices' in price_json:
                    return price_json['prices']
        except Exception: pass
        return {}

    def _get_effective_event_time(event):
        """
        Trả về thời gian hiệu lực của sự kiện dưới dạng datetime object (đã ở múi giờ Việt Nam).
        """
        event_date_str = event.get('date')
        event_time_str = event.get('time')
        if not (event_date_str and event_time_str and ':' in event_time_str):
            return None
        try:
            cleaned_time_str = event_time_str.strip().split()[0]
            naive_dt = datetime.strptime(f"{event_date_str} {cleaned_time_str}", '%Y-%m-%d %H:%M')
            if event.get('phase') == 2:
                naive_dt += timedelta(hours=18)
            china_dt = CHINA_TIMEZONE.localize(naive_dt)
            vietnam_dt = china_dt.astimezone(TIMEZONE)
            return vietnam_dt
        except Exception:
            return None

    try:
        airdrop_res = requests.get(AIRDROP_API_URL, headers=HEADERS, timeout=20)
        if airdrop_res.status_code != 200: return None, f"❌ Lỗi khi gọi API sự kiện (Code: {airdrop_res.status_code})."
        
        data = airdrop_res.json()
        airdrops = data.get('airdrops', [])
        if not airdrops: return [], None

        price_data = _get_price_data()
        
        for event in airdrops:
            event['effective_dt'] = _get_effective_event_time(event)
            event['price_data'] = price_data

        return airdrops, None
    except requests.RequestException: return None, "❌ Lỗi mạng khi lấy dữ liệu sự kiện."
    except json.JSONDecodeError: return None, "❌ Dữ liệu trả về từ API sự kiện không hợp lệ."

def get_airdrop_events() -> tuple[str, str | None]:
    """
    Hàm giao diện: Gọi hàm logic cốt lõi và định dạng kết quả.
    Trả về: (tin nhắn đã định dạng, token của sự kiện gần nhất).
    """
    processed_events, error_message = _get_processed_airdrop_events()
    if error_message:
        return error_message, None
    if not processed_events:
        return "ℹ️ Không tìm thấy sự kiện airdrop nào.", None

    def _format_event_message(event, price_data, effective_dt, include_date=False):
        token, name = event.get('token', 'N/A'), event.get('name', 'N/A')
        points, amount_str = event.get('points') or '-', event.get('amount') or '-'
        
        display_time = event.get('time') or 'TBA'
        is_special_time = "Tomorrow" in display_time or "Day after" in display_time
        
        if effective_dt and not is_special_time:
            time_part = effective_dt.strftime('%H:%M')
            if include_date:
                date_part = effective_dt.strftime('%d/%m')
                display_time = f"{time_part} {date_part}"
            else:
                display_time = time_part
        
        price_str, value_str = "", ""
        if token in price_data:
            price_value = price_data[token].get('dex_price') or price_data[token].get('price', 0)
            if price_value > 0:
                price_str = f" `${price_value:,.4f}`"
                try:
                    value = float(amount_str) * price_value
                    value_str = f"\n  Value: `${value:,.2f}`"
                except (ValueError, TypeError): pass
        
        time_str = f"`{display_time}`"
        return (f"*{name} ({token}):*{price_str}\n"
                f"  Points: `{points}` | Amount: `{amount_str}`{value_str}\n"
                f"  Time: {time_str}")

    now_vietnam = datetime.now(TIMEZONE)
    today_date = now_vietnam.date()
    todays_events, upcoming_events = [], []

    for event in processed_events:
        effective_dt = event['effective_dt']
        if effective_dt and effective_dt < now_vietnam: continue
        
        event_date_str = event.get('date')
        if not event_date_str: continue

        try:
            event_day = effective_dt.date() if effective_dt else datetime.strptime(event_date_str, '%Y-%m-%d').date()
        except ValueError:
            continue

        if event_day == today_date:
            todays_events.append(event)
        elif event_day > today_date:
            upcoming_events.append(event)

    todays_events.sort(key=lambda x: x.get('effective_dt') or datetime.max.replace(tzinfo=TIMEZONE))
    upcoming_events.sort(key=lambda x: x.get('effective_dt') or datetime.max.replace(tzinfo=TIMEZONE))
    
    next_event_token = None
    all_future_events = todays_events + upcoming_events
    if all_future_events:
        next_event_token = all_future_events[0].get('token')

    message_parts = []
    price_data = processed_events[0]['price_data'] if processed_events else {}
    
    if todays_events:
        today_messages = [_format_event_message(e, price_data, e['effective_dt']) for e in todays_events]
        message_parts.append("🎁 *Today's Airdrops:*\n\n" + "\n\n".join(today_messages))

    if upcoming_events:
        if message_parts: message_parts.append("\n\n" + "-"*25 + "\n\n")
        
        upcoming_messages = []
        for event in upcoming_events:
            effective_dt = event['effective_dt']
            upcoming_messages.append(_format_event_message(event, price_data, effective_dt, include_date=True))
            
        message_parts.append("🗓️ *Upcoming Airdrops:*\n\n" + "\n\n".join(upcoming_messages))

    final_message = "".join(message_parts)
    if not final_message:
        final_message = "ℹ️ Không có sự kiện airdrop nào đáng chú ý trong hôm nay và các ngày sắp tới."
    
    return final_message, next_event_token

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
    tasks.append({"type": "simple", "time_iso": task_dt.isoformat(), "name": name_part})
    tasks.sort(key=lambda x: x['time_iso'])
    kv.set(f"tasks:{chat_id}", json.dumps(tasks))
    return True, f"✅ Đã thêm lịch: *{name_part}*."

def add_alpha_task(chat_id, task_string: str) -> tuple[bool, str]:
    if not kv: return False, "Lỗi: Chức năng lịch hẹn không khả dụng do không kết nối được DB."
    try:
        parts = task_string.split(' - ', 2)
        if len(parts) != 3: raise ValueError
        
        time_part, event_name, token_info_part = parts[0], parts[1].strip(), parts[2].strip()

        task_dt, _ = parse_task_from_string(f"{time_part} - {event_name}")
        if not task_dt or not event_name: raise ValueError

        token_info = token_info_part.strip("'").split()
        if len(token_info) != 2: raise ValueError
        amount_str, contract = token_info
        amount = float(amount_str)
        
        if not is_crypto_address(contract):
            return False, f"❌ Địa chỉ contract không hợp lệ: `{contract}`"
            
        token_details = get_token_details_by_contract(contract)
        if not token_details:
            return False, f"❌ Không tìm thấy token với contract `{contract[:10]}...` trên các mạng được hỗ trợ."
            
    except (ValueError, IndexError):
        return False, "❌ Cú pháp sai. Dùng: `/alpha DD/MM HH:mm - Tên sự kiện - 'số lượng' 'contract'`."

    if task_dt < datetime.now(TIMEZONE): return False, "❌ Không thể đặt lịch cho quá khứ."

    tasks = json.loads(kv.get(f"tasks:{chat_id}") or '[]')
    tasks.append({
        "type": "alpha",
        "time_iso": task_dt.isoformat(),
        "name": event_name,
        "amount": amount,
        "contract": contract
    })
    tasks.sort(key=lambda x: x['time_iso'])
    kv.set(f"tasks:{chat_id}", json.dumps(tasks))
    return True, f"✅ Đã thêm lịch Alpha: *{event_name}*."

def edit_task(chat_id, index_str: str, new_task_string: str) -> tuple[bool, str]:
    if not kv: return False, "Lỗi: Chức năng lịch hẹn không khả dụng do không kết nối được DB."
    try:
        task_index = int(index_str) - 1
        if task_index < 0: raise ValueError
    except (ValueError, AssertionError):
        return False, "❌ Số thứ tự không hợp lệ."

    user_tasks = json.loads(kv.get(f"tasks:{chat_id}") or '[]')
    now = datetime.now(TIMEZONE)
    active_tasks = [t for t in user_tasks if datetime.fromisoformat(t['time_iso']) > now]

    if task_index >= len(active_tasks):
        return False, "❌ Số thứ tự không hợp lệ."

    task_to_edit_ref = active_tasks[task_index]
    task_type = task_to_edit_ref.get("type", "simple")
    user_tasks = [t for t in user_tasks if t['time_iso'] != task_to_edit_ref['time_iso']]

    if task_type == "alpha":
        try:
            parts = new_task_string.split(' - ', 2)
            if len(parts) != 3: raise ValueError
            time_part, event_name, token_info_part = parts[0], parts[1].strip(), parts[2].strip()

            new_task_dt, _ = parse_task_from_string(f"{time_part} - {event_name}")
            if not new_task_dt or not event_name: raise ValueError

            token_info = token_info_part.strip("'").split()
            if len(token_info) != 2: raise ValueError
            amount_str, contract = token_info
            amount = float(amount_str)
            
            if not is_evm_address(contract):
                return False, f"❌ Địa chỉ contract BSC không hợp lệ: `{contract}`"
            
            initial_price = get_bsc_price_by_contract(contract)
            if initial_price is None:
                return False, f"❌ Không tìm thấy token với contract `{contract[:10]}...` trên mạng BSC."

            user_tasks.append({
                "type": "alpha",
                "time_iso": new_task_dt.isoformat(),
                "name": event_name,
                "amount": amount,
                "contract": contract
            })
            
        except (ValueError, IndexError):
            return False, "❌ Cú pháp sai. Dùng: `DD/MM HH:mm - Tên sự kiện - 'số lượng' 'contract'`."
    
    else: 
        new_task_dt, new_name_part = parse_task_from_string(new_task_string)
        if not new_task_dt or not new_name_part:
            return False, "❌ Cú pháp sai. Dùng: `DD/MM HH:mm - Tên công việc`."
        
        user_tasks.append({
            "type": "simple",
            "time_iso": new_task_dt.isoformat(),
            "name": new_name_part
        })

    user_tasks.sort(key=lambda x: x['time_iso'])
    kv.set(f"tasks:{chat_id}", json.dumps(user_tasks))
    return True, f"✅ Đã sửa công việc số *{task_index + 1}*."

def delete_task(chat_id, task_index_str: str) -> tuple[bool, str]:
    if not kv: return False, "Lỗi: Chức năng lịch hẹn không khả dụng do không kết nối được DB."
    try:
        task_index = int(task_index_str) - 1
        if task_index < 0: raise ValueError
    except (ValueError, AssertionError):
        return False, "❌ Số thứ tự không hợp lệ."

    user_tasks = json.loads(kv.get(f"tasks:{chat_id}") or '[]')
    active_tasks = [t for t in user_tasks if datetime.fromisoformat(t['time_iso']) > datetime.now(TIMEZONE)]

    if task_index >= len(active_tasks):
        return False, "❌ Số thứ tự không hợp lệ."

    task_to_delete = active_tasks[task_index]
    updated_tasks = [t for t in user_tasks if t['time_iso'] != task_to_delete['time_iso']]
    kv.set(f"tasks:{chat_id}", json.dumps(updated_tasks))
    return True, f"✅ Đã xóa lịch hẹn: *{task_to_delete['name']}*"

def list_tasks(chat_id) -> str:
    if not kv: return "Lỗi: Chức năng lịch hẹn không khả dụng do không kết nối được DB."
    user_tasks = json.loads(kv.get(f"tasks:{chat_id}") or '[]')
    active_tasks = [t for t in user_tasks if datetime.fromisoformat(t['time_iso']) > datetime.now(TIMEZONE)]
    if len(active_tasks) < len(user_tasks): kv.set(f"tasks:{chat_id}", json.dumps(active_tasks))
    if not active_tasks: return "Bạn không có lịch hẹn nào sắp tới.\nChuyển qua dùng /event để show toàn bộ sự kiện!"
    result_lines = ["*🗓️ Danh sách lịch hẹn của bạn:*"]
    for i, task in enumerate(active_tasks):
        result_lines.append(f"*{i+1}.* `{datetime.fromisoformat(task['time_iso']).strftime('%H:%M %d/%m')}` - {task['name']}")
    return "\n".join(result_lines)

# --- LOGIC CRYPTO & TIỆN ÍCH BOT ---
def get_coingecko_prices_by_symbols(symbols: list[str]) -> dict | None:
    if not symbols: return {}
    ids_to_fetch = [SYMBOL_TO_ID_MAP.get(s.lower(), s.lower()) for s in symbols]
    ids_string = ",".join(ids_to_fetch)
    url = f"https://api.coingecko.com/api/v3/simple/price"
    params = {'ids': ids_string, 'vs_currencies': 'usd'}
    try:
        res = requests.get(url, params=params, timeout=15)
        if res.status_code == 200:
            data = res.json()
            price_map = {}
            id_to_symbol_map = {v: k for k, v in SYMBOL_TO_ID_MAP.items()}
            for coin_id, price_data in data.items():
                symbol = id_to_symbol_map.get(coin_id, coin_id)
                price_map[symbol.lower()] = price_data.get('usd', 0)
            return price_map
        else:
            print(f"CoinGecko price API error: {res.status_code} - {res.text}")
            return None
    except requests.RequestException as e:
        print(f"Error fetching CoinGecko prices: {e}")
        return None

def process_folio_text(message_text: str) -> str:
    lines = message_text.strip().split('\n')
    if lines and lines[0].lower().startswith('/folio'):
        if len(lines[0].split()) == 1: lines = lines[1:]
        else: lines[0] = lines[0].split(maxsplit=1)[1]

    if not lines or all(not line.strip() for line in lines):
        return "Cú pháp: `/folio` sau đó xuống dòng nhập danh sách.\nVí dụ:\n`/folio\n0.5 btc\n10 eth`"

    portfolio_items = []
    symbols_to_fetch = set()
    for i, line in enumerate(lines):
        line = line.strip()
        if not line: continue
        parts = line.split()
        if len(parts) != 2: continue
        amount_str, symbol = parts
        try:
            amount = float(amount_str)
            portfolio_items.append({'amount': amount, 'symbol': symbol, 'line_num': i + 1})
            symbols_to_fetch.add(symbol)
        except ValueError:
            portfolio_items.append({'error': f"Số lượng không hợp lệ: `{amount_str}`", 'line_num': i + 1})

    prices = get_coingecko_prices_by_symbols(list(symbols_to_fetch))
    if prices is None:
        return "❌ Không thể lấy dữ liệu giá từ CoinGecko. Vui lòng thử lại sau."
        
    total_value = 0.0
    result_lines = []
    
    for item in portfolio_items:
        if 'error' in item:
            result_lines.append(f"Dòng {item['line_num']}: ❌ {item['error']}")
            continue
            
        symbol = item['symbol']
        amount = item['amount']
        price = prices.get(symbol.lower())
        
        if price is not None:
            value = amount * price
            total_value += value
            result_lines.append(f"*{symbol.upper()}*: `${price:,.4f}` x {amount} = *${value:,.2f}*")
        else:
            result_lines.append(f"❌ Không tìm thấy giá cho *{symbol.upper()}* trên CoinGecko.")
            
    final_result_text = "\n".join(result_lines)
    summary = f"\n--------------------\n*Tổng cộng:* *${total_value:,.2f}*"
    return final_result_text + summary

def get_bsc_price_by_contract(address: str) -> float | None:
    network = 'bsc'
    url = f"https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{address}"
    try:
        res = requests.get(url, headers={"accept": "application/json"}, timeout=10)
        if res.status_code == 200:
            data = res.json().get('data', {}).get('attributes', {})
            price_str = data.get('price_usd')
            if price_str:
                return float(price_str)
    except requests.RequestException as e:
        print(f"Error getting BSC price for {address}: {e}")
    return None

def get_price_by_symbol(symbol: str) -> float | None:
    coin_id = SYMBOL_TO_ID_MAP.get(symbol.lower(), symbol.lower())
    url = "https://api.coingecko.com/api/v3/simple/price"; params = {'ids': coin_id, 'vs_currencies': 'usd'}
    try:
        res = requests.get(url, params=params, timeout=10)
        return res.json().get(coin_id, {}).get('usd') if res.status_code == 200 else None
    except requests.RequestException: return None

# --- SỬA LẠI HÀM /GT (Dùng Model Llama 3 trên Groq) ---
def get_crypto_explanation(query: str) -> str:
    if not openai_client:
        return "❌ Lỗi cấu hình: Chưa cài đặt `GROQ_API_KEY` trong Settings của Vercel."
    
    try:
        # Sử dụng model llama-3.3-70b-versatile (Mạnh nhất, hỗ trợ tiếng Việt tốt)
        response = openai_client.chat.completions.create(
            model="llama-3.3-70b-versatile", 
            messages=[
                {"role": "system", "content": "Bạn là một trợ lý chuyên gia về tiền điện tử. Hãy trả lời câu hỏi sau một cách ngắn gọn, súc tích, và dễ hiểu bằng tiếng Việt cho người mới bắt đầu. Tập trung vào các khía cạnh quan trọng nhất."},
                {"role": "user", "content": query}
            ],
            max_tokens=1000
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Groq API Error: {e}")
        return f"❌ Lỗi Groq AI: {str(e)}"

def calculate_value(parts: list) -> str:
    if len(parts) != 3: return "Cú pháp: `/calc <ký hiệu> <số lượng>`\nVí dụ: `/calc btc 0.5`"
    symbol, amount_str = parts[1], parts[2]
    try: amount = float(amount_str)
    except ValueError: return f"❌ Số lượng không hợp lệ: `{amount_str}`"
    price = get_price_by_symbol(symbol)
    if price is None: return f"❌ Không tìm thấy giá cho ký hiệu `{symbol}`."
    total_value = price * amount
    return f"*{symbol.upper()}*: `${price:,.2f}` x {amount_str} = *${total_value:,.2f}*"

# --- SỬA LẠI HÀM /TR (Dùng Model Llama 3 trên Groq) ---
def translate_crypto_text(text_to_translate: str) -> str:
    if not openai_client:
        return "❌ Lỗi cấu hình: Chưa cài đặt `GROQ_API_KEY` trong Settings của Vercel."
    
    try:
        prompt = "Act as an expert translator specializing in finance and cryptocurrency. Your task is to translate the following English text into Vietnamese. Use accurate and natural-sounding financial/crypto jargon appropriate for a savvy investment community. Preserve the original nuance and meaning. Only provide the final Vietnamese translation, without any additional explanation."
        
        response = openai_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": text_to_translate}
            ],
            max_tokens=1000
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Groq API Error (Translation): {e}")
        return f"❌ Lỗi Groq AI: {str(e)}"

def find_perpetual_markets(symbol: str) -> str:
    """Tìm các sàn CEX và DEX có hợp đồng perpetual và hiển thị funding rate."""
    url = "https://api.coingecko.com/api/v3/derivatives"
    params = {'include_tickers': 'unexpired'}
    
    try:
        res = requests.get(url, params=params, timeout=25)
        if res.status_code != 200:
            return f"❌ Lỗi khi gọi API CoinGecko (Code: {res.status_code})."
        
        derivatives = res.json()
        if not derivatives:
            return "❌ Không thể lấy dữ liệu phái sinh từ CoinGecko."
        
        markets = []
        found = False
        search_symbol = symbol.upper()
        
        for contract in derivatives:
            contract_symbol = contract.get('symbol', '')
            
            if contract_symbol.startswith(search_symbol):
                found = True
                market_name = contract.get('market')
                funding_rate = contract.get('funding_rate')
                
                if market_name and funding_rate is not None:
                    markets.append({
                        'name': market_name,
                        'funding_rate': float(funding_rate)
                    })

        if not found or not markets:
            return f"ℹ️ Không tìm thấy thị trường Perpetual nào có dữ liệu funding rate cho *{symbol.upper()}*."

        markets.sort(key=lambda x: x['funding_rate'], reverse=True)
        
        message_parts = [f"📊 *Funding Rate cho {symbol.upper()} (Perpetual):*"]
        
        for market in markets[:15]:
            rate = market['funding_rate']
            emoji = "🟢" if rate > 0 else "🔴" if rate < 0 else "⚪️"
            message_parts.append(f"{emoji} `{market['name']}`: `{rate:+.4f}%`")
            
        return "\n".join(message_parts)

    except requests.RequestException as e:
        print(f"Error in find_perpetual_markets: {e}")
        return "❌ Lỗi mạng khi lấy dữ liệu thị trường phái sinh."

def unalert_price(chat_id, address: str) -> str:
    if not kv: return "Lỗi: Chức năng cảnh báo giá không khả dụng do không kết nối được DB."
    alert_key = f"{chat_id}:{address.lower()}"
    if kv.hexists("price_alerts", alert_key):
        kv.hdel("price_alerts", alert_key)
        return f"✅ Đã xóa cảnh báo giá cho token `{address[:6]}...{address[-4:]}`."
    else:
        return f"❌ Không tìm thấy cảnh báo nào cho token `{address[:6]}...{address[-4:]}`."

def set_price_alert(chat_id, address: str, percentage_str: str) -> str:
    if not kv: return "Lỗi: Chức năng cảnh báo giá không khả dụng do không kết nối được DB."
    try:
        percentage = float(percentage_str)
        if percentage <= 0:
            return unalert_price(chat_id, address)
    except ValueError:
        return "❌ Phần trăm không hợp lệ. Vui lòng nhập một con số (ví dụ: `5`)."

    token_info = get_token_details_by_contract(address)
    
    if not token_info:
        return f"❌ Không thể tìm thấy thông tin cho token `{address[:10]}...` để đặt cảnh báo."
    
    current_price = token_info['price']
    
    alert_data = {
        "address": address.lower(),
        "network": token_info['network'],
        "symbol": token_info['symbol'],
        "name": token_info['name'],
        "chat_id": chat_id,
        "threshold_percent": percentage,
        "reference_price": current_price
    }
    
    kv.hset("price_alerts", f"{chat_id}:{address.lower()}", json.dumps(alert_data))
    
    return (f"✅ Đã đặt cảnh báo cho *{token_info['name']} (${token_info['symbol']})*.\n"
            f"Bot sẽ thông báo mỗi khi giá thay đổi `±{percentage}%` so với giá tham chiếu hiện tại là `${current_price:,.4f}`.")

def list_price_alerts(chat_id) -> str:
    if not kv: return "Lỗi: Chức năng cảnh báo giá không khả dụng do không kết nối được DB."
    all_alerts_raw = kv.hgetall("price_alerts")
    user_alerts = []
    
    for key, alert_json in all_alerts_raw.items():
        if key.startswith(f"{chat_id}:"):
            try:
                alert = json.loads(alert_json)
                user_alerts.append(alert)
            except json.JSONDecodeError:
                continue
    
    if not user_alerts:
        return "Bạn chưa đặt cảnh báo giá nào."
        
    message_parts = ["*🔔 Danh sách cảnh báo giá đang hoạt động:*"]
    for alert in user_alerts:
        symbol = alert.get('symbol', 'N/A')
        name = alert.get('name', alert.get('address', 'N/A'))
        threshold = alert.get('threshold_percent', 'N/A')
        ref_price = alert.get('reference_price', 0)
        
        message_parts.append(
            f"\n- *{name} (${symbol})* | Ngưỡng: `±{threshold}%` | Giá tham chiếu: `${ref_price:,.4f}`"
        )
    return "\n".join(message_parts)

def get_token_details_by_contract(address: str) -> dict | None:
    for network in AUTO_SEARCH_NETWORKS:
        url = f"https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{address}"
        try:
            res = requests.get(url, headers={"accept": "application/json"}, timeout=10)
            if res.status_code == 200:
                data = res.json().get('data', {}).get('attributes', {})
                name = data.get('name')
                if name:
                    price_str = data.get('price_usd')
                    price = float(price_str) if price_str is not None else 0.0
                    return {
                        "price": price,
                        "network": network,
                        "symbol": data.get('symbol', 'N/A'),
                        "name": name
                    }
        except requests.RequestException:
            continue
    return None

def check_price_alerts():
    if not kv: print("Price Alert check skipped due to no DB connection."); return
    all_alerts_raw = kv.hgetall("price_alerts")
    for key, alert_json in all_alerts_raw.items():
        try:
            alert = json.loads(alert_json)
            address = alert['address']; network = alert['network']; chat_id = alert['chat_id']
            threshold = alert['threshold_percent']; ref_price = alert['reference_price']
            
            token_info = get_token_details_by_contract(address)
            
            if not token_info: continue
            
            current_price = token_info['price']
            
            price_change_pct = ((current_price - ref_price) / ref_price) * 100 if ref_price > 0 else 0
            
            if abs(price_change_pct) >= threshold:
                emoji = "📈" if price_change_pct > 0 else "📉"
                name = alert.get('name', address)
                symbol = alert.get('symbol', 'Token')
                
                message = (f"🚨 *Cảnh báo giá cho {name} (${symbol})!*\n\n"
                           f"Mạng: *{network.upper()}*\n\n"
                           f"{emoji} Giá đã thay đổi *{price_change_pct:+.2f}%*\n"
                           f"Giá cũ: `${ref_price:,.4f}`\n"
                           f"Giá mới: *`${current_price:,.4f}`*")
                
                send_telegram_message(chat_id, text=message)
                
                alert['reference_price'] = current_price
                kv.hset("price_alerts", key, json.dumps(alert))

        except (json.JSONDecodeError, KeyError) as e:
            print(f"Error processing price alert for key {key}: {e}")
            continue

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
                data = res.json()
                token_attr = data.get('data', {}).get('attributes', {})
                if not token_attr: continue
                price_str = token_attr.get('price_usd')
                price = float(price_str) if price_str is not None else 0.0
                change_pct_str = token_attr.get('price_change_percentage', {}).get('h24')
                change = float(change_pct_str) if change_pct_str is not None else 0.0
                return (f"✅ *Tìm thấy trên mạng {network.upper()}*\n"
                        f"*{token_attr.get('name', 'N/A')} ({token_attr.get('symbol', 'N/A')})*\n\n"
                        f"Giá: *${price:,.8f}*\n24h: *{'📈' if change >= 0 else '📉'} {change:+.2f}%*\n\n"
                        f"🔗 [Xem trên GeckoTerminal](https://www.geckoterminal.com/{network}/tokens/{address})\n\n`{address}`")
        except requests.RequestException:
            continue
    return f"❌ Không tìm thấy token với địa chỉ `{address[:10]}...`."

def process_portfolio_text(message_text: str) -> str | None:
    lines = message_text.strip().split('\n')
    
    # 1. Gom nhóm token theo network để gọi API 1 lần (Batching)
    # Cấu trúc: {'bsc': [{'addr': '0x...', 'amount': 100}, ...], 'base': [...]}
    network_groups = {}
    valid_lines_count = 0
    
    for line in lines:
        parts = line.strip().split()
        if len(parts) != 3: continue
        
        try: 
            amount = float(parts[0])
        except ValueError: 
            continue
            
        address, network = parts[1], parts[2].lower()
        
        if not is_crypto_address(address):
            continue

        valid_lines_count += 1
        
        if network not in network_groups:
            network_groups[network] = []
        network_groups[network].append({'amount': amount, 'address': address})

    if valid_lines_count == 0: return None

    total_value = 0.0
    result_lines = []

    # 2. Xử lý từng network (Giảm số lượng request từ N xuống 1)
    for network, items in network_groups.items():
        # Lấy danh sách address và nối chuỗi bằng dấu phẩy
        addresses = [item['address'] for item in items]
        addresses_str = ",".join(addresses)
        
        # Dùng endpoint multi-token của GeckoTerminal
        url = f"https://api.geckoterminal.com/api/v2/networks/{network}/tokens/multi/{addresses_str}"
        
        try:
            res = requests.get(url, headers={"accept": "application/json"}, timeout=15)
            
            if res.status_code == 200:
                data = res.json().get('data', [])
                
                # Tạo map để tra cứu giá nhanh: {address_lower: price_data}
                price_map = {}
                for token_data in data:
                    attrs = token_data.get('attributes', {})
                    addr = attrs.get('address', '').lower()
                    price_map[addr] = {
                        'price': float(attrs.get('price_usd') or 0),
                        'symbol': attrs.get('symbol', 'N/A')
                    }
                
                # Tính toán dựa trên danh sách input ban đầu
                for item in items:
                    addr_key = item['address'].lower()
                    amount = item['amount']
                    
                    if addr_key in price_map:
                        info = price_map[addr_key]
                        price = info['price']
                        symbol = info['symbol']
                        
                        if price > 0:
                            value = amount * price
                            total_value += value
                            result_lines.append(f"*{symbol}*: `${price:,.4f}` x {amount} = *${value:,.2f}*")
                        else:
                            result_lines.append(f"⚠️ *{symbol}*: Chưa có giá (Liquidity thấp)")
                    else:
                        # Trường hợp API trả về OK nhưng không tìm thấy token trong list đó
                        result_lines.append(f"❌ Không tìm thấy data: `{item['address'][:6]}...`")
            
            else:
                # Lỗi cả network (ví dụ nhập sai tên network)
                result_lines.append(f"❌ Lỗi mạng {network}: API trả về {res.status_code}")

        except requests.RequestException:
            result_lines.append(f"🔌 Lỗi kết nối khi check mạng {network}")

    return "\n".join(result_lines) + f"\n--------------------\n*Ai chơi alpha là chó con: *${total_value:,.2f}**"

# --- WEB SERVER (FLASK) ---
app = Flask(__name__)
@app.route('/', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        return '''
        <html>
            <head>
                <meta name="talentapp:project_verification" content="c6cf868d6d1698cf0b43b101e332f5ad9c891e925c371aaf2ec73ee23198897ce60c79f851d9c67d9ca150e10051c46c3f638f1e7aa18a403e1ce9c977ba3360">
            </head>
            <body>TeeBoo Bot Home</body>
        </html>
        '''
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
                             "`/alpha time - tên event - amount contract`\n"
                             "**Ví dụ: /alpha 20/08 22:00 - Alpha: GAME - 132 0x825459139c897d769339f295e962396c4f9e4a4d**\n"
                             "`/gia <ký hiệu>`\n"
                             "`/calc <ký hiệu> <số lượng>`\n"
                             "`/gt <thuật ngữ>`\n"
                             "`/tr <nội dung>`\n"
                             "`/event` - Xem lịch airdrop sắp tới\n"
                             "`/autonotify on` - Bật thông báo tự động cho nhóm\n"
                             "`/perp <ký hiệu>`\n"
                             "1️⃣ *Tra cứu Token theo Contract*\n"
                             "2️⃣ *Tính Portfolio (Event trade Alpha)*\n"
                             "Cú pháp: <số lượng> <contract> <chain>\n"
                             "Ví dụ: 20000 0x825459139c897d769339f295e962396c4f9e4a4d bsc")
            send_telegram_message(chat_id, text=start_message)
        elif cmd == "/autonotify":
            if len(parts) < 2:
                send_telegram_message(chat_id, text="Cú pháp sai. Dùng: `/autonotify on` hoặc `/autonotify off`.", reply_to_message_id=msg_id)
            else:
                sub_command = parts[1].lower()
                if sub_command == 'on':
                    if kv:
                        kv.sadd("event_notification_groups", chat_id)
                        send_telegram_message(chat_id, text="✅ Đã bật tính năng tự động thông báo và ghim tin nhắn cho các sự kiện airdrop trong nhóm này.")
                    else:
                        send_telegram_message(chat_id, text="❌ Lỗi: Không thể thực hiện do không kết nối được DB.")
                elif sub_command == 'off':
                    if kv:
                        kv.srem("event_notification_groups", chat_id)
                        send_telegram_message(chat_id, text="✅ Đã tắt tính năng tự động thông báo sự kiện trong nhóm này.")
                    else:
                        send_telegram_message(chat_id, text="❌ Lỗi: Không thể thực hiện do không kết nối được DB.")
                else:
                    send_telegram_message(chat_id, text="Cú pháp sai. Dùng: `/autonotify on` hoặc `/autonotify off`.", reply_to_message_id=msg_id)
        elif cmd == "/donate":
            send_telegram_message(chat_id, text="*1000u cho mỗi ví nào:*\n\n0xdejun.eth\ntieubochet.eth\nhipitutu.base.eth\nzeronftt.eth\ncuongeth.base.eth\nginmoney.base.eth\nhenryn6868.base.eth\nkorkwy.base.eth\nfunio.base.eth\ntienho.base.eth\npigrich.base.eth\n", reply_to_message_id=msg_id)
        elif cmd in ['/add', '/edit', '/del']:
            success = False; message = ""
            if cmd == '/add':
                success, message = add_task(chat_id, " ".join(parts[1:]))
            elif cmd == '/del':
                if len(parts) > 1:
                    success, message = delete_task(chat_id, parts[1])
                else:
                    message = "Cú pháp: `/del <số>`"
            elif cmd == '/edit':
                if len(parts) < 3:
                    message = "Cú pháp: `/edit <số> DD/MM HH:mm - Tên mới`"
                else:
                    success, message = edit_task(chat_id, parts[1], " ".join(parts[2:]))
            
            if success:
                temp_msg_id = send_telegram_message(chat_id, text=message, reply_to_message_id=msg_id)
                send_telegram_message(chat_id, text=list_tasks(chat_id))
                if temp_msg_id:
                    delete_telegram_message(chat_id, temp_msg_id)
            else:
                send_telegram_message(chat_id, text=message, reply_to_message_id=msg_id)
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
        elif cmd == '/event':
            temp_msg_id = send_telegram_message(chat_id, text="🔍 Teeboo đang tìm, đợi tí fen 😏", reply_to_message_id=msg_id)
            if temp_msg_id:
                result, next_token = get_airdrop_events()
                button_label = "🚀 Trade on Hyperliquid"
                if next_token:
                    button_label = f"🚀 Trade {next_token.upper()} on Hyperliquid"
                reply_markup = {
                    'inline_keyboard': [
                        [
                            {'text': button_label, 'url': 'https://app.hyperliquid.xyz/join/TIEUBOCHET'}
                        ]
                    ]
                }
                edit_telegram_message(chat_id, temp_msg_id, text=result, reply_markup=json.dumps(reply_markup))
        elif cmd == '/folio':
            result = process_folio_text(text)
            send_telegram_message(chat_id, text=result, reply_to_message_id=msg_id)
        elif cmd == '/alpha':
            success, message = add_alpha_task(chat_id, " ".join(parts[1:]))
            if success:
                temp_msg_id = send_telegram_message(chat_id, text=message, reply_to_message_id=msg_id)
                send_telegram_message(chat_id, text=list_tasks(chat_id))
                if temp_msg_id: delete_telegram_message(chat_id, temp_msg_id)
            else:
                send_telegram_message(chat_id, text=message, reply_to_message_id=msg_id)
        elif cmd == '/perp':
            if len(parts) < 2: send_telegram_message(chat_id, text="Cú pháp: `/perp <ký hiệu>`", reply_to_message_id=msg_id)
            else:
                symbol = parts[1]
                temp_msg_id = send_telegram_message(chat_id, text=f"🔍 Đang tìm các sàn Futures cho *{symbol.upper()}*...", reply_to_message_id=msg_id)
                if temp_msg_id: edit_telegram_message(chat_id, temp_msg_id, text=find_perpetual_markets(symbol))
        elif cmd == '/alert':
            if len(parts) < 3:
                send_telegram_message(chat_id, text="Cú pháp: `/alert <contract> <%>`", reply_to_message_id=msg_id)
            else: send_telegram_message(chat_id, text=set_price_alert(chat_id, parts[1], parts[2]), reply_to_message_id=msg_id)
        elif cmd == '/unalert':
            if len(parts) < 2:
                send_telegram_message(chat_id, text="Cú pháp: `/unalert <địa chỉ contract>`", reply_to_message_id=msg_id)
            else:
                send_telegram_message(chat_id, text=unalert_price(chat_id, parts[1]), reply_to_message_id=msg_id)
        elif cmd == '/alerts':
            send_telegram_message(chat_id, text=list_price_alerts(chat_id), reply_to_message_id=msg_id)
        return jsonify(success=True)
    
    if len(parts) == 1 and is_crypto_address(parts[0]):
        send_telegram_message(chat_id, text=find_token_across_networks(parts[0]), reply_to_message_id=msg_id, disable_web_page_preview=True)
    else:
        portfolio_result = process_portfolio_text(text)
        if portfolio_result:
            refresh_btn = {'inline_keyboard': [[{'text': '🔄 Refresh', 'callback_data': 'refresh_portfolio'}]]}
            send_telegram_message(chat_id, text=portfolio_result, reply_to_message_id=msg_id, reply_markup=json.dumps(refresh_btn))
    return jsonify(success=True)

def check_events_and_notify_groups():
    if not kv:
        print("Event check skipped: No DB connection.")
        return 0

    print(f"[{datetime.now()}] Running group event notification check...")
    events, error = _get_processed_airdrop_events()
    if error or not events:
        print(f"Could not fetch events for notification: {error or 'No events found.'}")
        return 0

    notifications_sent = 0
    now = datetime.now(TIMEZONE)
    
    subscribers = kv.smembers("event_notification_groups")
    if not subscribers:
        print("Event check skipped: No subscribed groups.")
        return 0

    for event in events:
        event_time = event.get('effective_dt')
        if not event_time: continue

        if event_time > now:
            time_until_event = event_time - now
            
            if timedelta(minutes=0) < time_until_event <= timedelta(minutes=REMINDER_THRESHOLD_MINUTES):
                event_id = f"{event.get('token')}-{event_time.isoformat()}"
                
                for chat_id in subscribers:
                    redis_key = f"event_notified:{chat_id}:{event_id}"

                    if not kv.exists(redis_key):
                        minutes_left = int(time_until_event.total_seconds() // 60) + 1
                        token, name = event.get('token', 'N/A'), event.get('name', 'N/A')
                        
                        message = (f"‼️ *ANH NHẮC EM*\n\n"
                                   f"Sự kiện: *{name} ({token})*\n"
                                   f"Thời gian: Trong vòng *{minutes_left} phút* nữa.")
                        
                        sent_message_id = send_telegram_message(chat_id, text=message)
                        
                        if sent_message_id:
                            # pin_telegram_message(chat_id, sent_message_id)
                            notifications_sent += 1
                            kv.set(redis_key, "1", ex=3600)

    print(f"Group event notification check finished. Sent: {notifications_sent} notifications.")
    return notifications_sent

@app.route('/check_events', methods=['POST'])
def event_cron_webhook():
    if not kv or not BOT_TOKEN or not CRON_SECRET:
        return jsonify(error="Server not configured"), 500
    
    secret = request.headers.get('X-Cron-Secret') or (request.is_json and request.get_json().get('secret'))
    if secret != CRON_SECRET:
        return jsonify(error="Unauthorized"), 403

    sent_count = check_events_and_notify_groups()
    return jsonify(success=True, notifications_sent=sent_count)

@app.route('/check_reminders', methods=['POST'])
def cron_webhook():
    if not kv or not BOT_TOKEN or not CRON_SECRET: return jsonify(error="Server not configured"), 500
    secret = request.headers.get('X-Cron-Secret') or (request.is_json and request.get_json().get('secret'))
    if secret != CRON_SECRET: return jsonify(error="Unauthorized"), 403
    
    print(f"[{datetime.now()}] Running reminder check...")
    reminders_sent = 0
    
    for key in kv.scan_iter("tasks:*"):
        chat_id = key.split(':')[1]
        user_tasks = json.loads(kv.get(key) or '[]')
        now = datetime.now(TIMEZONE)
        active_tasks_after_check = []
        tasks_changed = False
        
        for task in user_tasks:
            task_time = datetime.fromisoformat(task['time_iso'])
            if task_time > now:
                active_tasks_after_check.append(task)
                time_until_due = task_time - now
                
                if timedelta(seconds=1) < time_until_due <= timedelta(minutes=REMINDER_THRESHOLD_MINUTES):
                    last_reminded_key = f"last_reminded:{chat_id}:{task['time_iso']}"
                    last_reminded_ts_str = kv.get(last_reminded_key)
                    last_reminded_ts = float(last_reminded_ts_str) if last_reminded_ts_str else 0
                    
                    if (datetime.now().timestamp() - last_reminded_ts) > 270:
                        minutes_left = int(time_until_due.total_seconds() / 60)
                        
                        reminder_text = f"‼️ *ANH NHẮC EM*\n\nSự kiện: *{task['name']}*\nSẽ diễn ra trong khoảng *{minutes_left} phút* nữa."

                        if task.get("type") == "alpha":
                            token_details = get_token_details_by_contract(task['contract'])
                            if token_details:
                                price = token_details['price']
                                value = price * task['amount']
                                reminder_text = (
                                    f"‼️ *ANH NHẮC EM* ‼️\n\n"
                                    f"Sự kiện: *{task['name']}*\nSẽ diễn ra trong khoảng *{minutes_left} phút* nữa.\n\n"
                                    f"Giá token: `${price:,.6f}`\n"
                                    f"Tổng ≈ `${value:,.2f}`"
                                )
                        
                        sent_message_id = send_telegram_message(chat_id, text=reminder_text)
                        if sent_message_id:
                            # pin_telegram_message(chat_id, sent_message_id)
                            pass

                        kv.set(last_reminded_key, datetime.now().timestamp(), ex=3600)
                        reminders_sent += 1
            else:
                tasks_changed = True

        if tasks_changed:
            kv.set(key, json.dumps(active_tasks_after_check))

    result = {"status": "success", "reminders_sent": reminders_sent}
    print(result)
    return jsonify(result)

@app.route('/check_alerts', methods=['POST'])
def alert_cron_webhook():
    if not kv or not BOT_TOKEN or not CRON_SECRET: return jsonify(error="Server not configured"), 500
    secret = request.headers.get('X-Cron-Secret') or (request.is_json and request.get_json().get('secret'))
    if secret != CRON_SECRET: return jsonify(error="Unauthorized"), 403
    print(f"[{datetime.now()}] Running price alert check...")
    check_price_alerts()
    return jsonify(success=True)