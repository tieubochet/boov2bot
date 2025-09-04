import requests
import json # Thư viện để làm việc với JSON, ở đây dùng để in đẹp hơn

# 1. Khai báo thông tin cần thiết
url = "https://alpha123.uk/api/price/?batch=today"

# Header 'referer' rất quan trọng, vì server có thể kiểm tra xem
# yêu cầu có phải đến từ trang web của họ hay không.
# Bạn đã làm đúng ở bước này.
headers = {
  'referer': 'https://alpha123.uk/vi/index.html'
}

print(f"Đang gửi yêu cầu đến: {url}")

# 2. Gửi yêu cầu và xử lý kết quả
try:
    # Sử dụng requests.get() cho yêu cầu GET, sẽ ngắn gọn hơn
    # Không cần payload cho yêu cầu GET này
    response = requests.get(url, headers=headers, timeout=10) # Thêm timeout để tránh chờ quá lâu

    # Kiểm tra xem yêu cầu có thành công không (status code 200)
    response.raise_for_status()  # Nếu có lỗi (4xx, 5xx), dòng này sẽ báo lỗi ngay lập tức

    # 3. Chuyển đổi dữ liệu JSON thành đối tượng Python
    # Đây là bước quan trọng nhất
    data = response.json()

    # 4. In dữ liệu ra màn hình một cách có cấu trúc
    print("Yêu cầu thành công! Dữ liệu nhận được:")
    
    # Dùng json.dumps để in ra cho đẹp và dễ đọc
    print(json.dumps(data, indent=2, ensure_ascii=False))

    # --- Ví dụ cách sử dụng dữ liệu ---
    # Dữ liệu trả về là một danh sách (list) các từ điển (dictionary)
    # Bạn có thể duyệt qua danh sách này
    if isinstance(data, list) and len(data) > 0:
        print("\n--- Ví dụ xử lý dữ liệu ---")
        
        # Lấy thông tin của sản phẩm đầu tiên trong danh sách
        first_item = data[0]
        print(f"Thông tin sản phẩm đầu tiên:")
        print(f"  - Tên (name): {first_item.get('name')}")
        print(f"  - Giá (price): {first_item.get('price')}")
        print(f"  - Ngày cập nhật (date): {first_item.get('date')}")

except requests.exceptions.RequestException as e:
    # Bắt tất cả các lỗi liên quan đến thư viện requests (mất mạng, DNS lỗi, ...)
    print(f"Đã xảy ra lỗi khi gửi yêu cầu: {e}")
except json.JSONDecodeError:
    # Bắt lỗi nếu server không trả về JSON hợp lệ
    print("Lỗi: Không thể phân tích dữ liệu JSON từ server.")
    print("Dữ liệu thô nhận được:", response.text)