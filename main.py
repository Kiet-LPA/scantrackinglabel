import cv2
import time
import os
import datetime
import csv
import easyocr
import torch
import winsound
import re

# ========== CẤU HÌNH ==========
VIDEO_DIR = 'videos'
CSV_FILE = 'tracking_log.csv'
os.makedirs(VIDEO_DIR, exist_ok=True)

use_gpu = torch.cuda.is_available()
print("✅ CUDA GPU hỗ trợ:", use_gpu)

reader = easyocr.Reader(['en'], gpu=use_gpu)

if not os.path.exists(CSV_FILE):
    with open(CSV_FILE, 'w', newline='', encoding='utf-8') as f:
        csv.writer(f).writerow(['tracking_number', 'timestamp', 'video_path', 'status'])

# ========== CAMERA ==========
cap = cv2.VideoCapture(0)
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

scanned_trackings = set()
current_tracking = None
tracking_start_time = None

interval = 1.2
last_check = 0

print("🚀 Camera đã bật. Đang theo dõi sản phẩm...")

def extract_tracking_number_and_conf(texts):
    for _, raw_text, conf in texts:
        # Tìm chuỗi số dài từ 10–20 chữ số
        matches = re.findall(r'\d{10,30}', raw_text.replace(" ", ""))
        if matches:
            return matches[0], conf
    return None, 0

# ========== MAIN LOOP ==========
while True:
    ret, frame = cap.read()
    if not ret:
        continue

    h, w, _ = frame.shape

    # ROI
    roi_top = int(h * 0.82)
    roi_bottom = int(h * 0.95)
    roi_left = int(w * 0.15)
    roi_right = int(w * 0.85)
    roi = frame[roi_top:roi_bottom, roi_left:roi_right]

    now = time.time()
    roi_color = (0, 255, 0)

    if now - last_check >= interval:
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (3, 3), 0)
        _, thresh = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        results = reader.readtext(thresh)
        print("\n🧠 Kết quả OCR:")
        for box, text, conf in results:
            print(f"  📌 '{text}' (conf: {conf:.2f})")

        tracking, confidence = extract_tracking_number_and_conf(results)

        if tracking:
            if tracking not in scanned_trackings:
                if tracking != current_tracking:
                    current_tracking = tracking
                    tracking_start_time = now
                    print(f"⏳ Đang theo dõi mã: {tracking}")
                    roi_color = (0, 255, 255)
                else:
                    duration = now - tracking_start_time
                    if duration >= 2.0 or confidence >= 0.85:
                        timestamp = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
                        video_name = f"{tracking}_{timestamp}.mp4"
                        video_path = os.path.join(VIDEO_DIR, video_name)

                        try:
                            with open(CSV_FILE, 'a', newline='', encoding='utf-8') as f:
                                csv.writer(f).writerow([f'="{tracking}"', timestamp, video_path, "shipped"])
                        except PermissionError:
                            print("❌ Không thể ghi vào file CSV (đang mở?)")
                            continue

                        scanned_trackings.add(tracking)
                        current_tracking = None
                        tracking_start_time = None

                        print(f"✅ Mã hợp lệ (conf {confidence:.2f}, duration {duration:.1f}s): {tracking} → quay video...")

                        winsound.Beep(1000, 200)

                        # Quay video 4 giây
                        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                        out = cv2.VideoWriter(video_path, fourcc, 20.0, (frame.shape[1], frame.shape[0]))
                        start = time.time()
                        while time.time() - start < 4:
                            ret, f = cap.read()
                            if not ret:
                                break
                            out.write(f)
                        out.release()
                        print(f"🎞️ Đã lưu video: {video_name}")
                    else:
                        print(f"⚠️ Mã '{tracking}' OCR chưa đủ điều kiện (conf {confidence:.2f}, duration {duration:.1f}s) — không lưu.")

            else:
                print(f"🔁 Đã quét mã này rồi: {tracking}")
        else:
            current_tracking = None
            tracking_start_time = None


        last_check = now

    # Hiển thị
    cv2.rectangle(frame, (roi_left, roi_top), (roi_right, roi_bottom), roi_color, 2)
    cv2.imshow("🚚 ScanLabelTracking", frame)
    cv2.imshow("🔍 ROI Tracking", roi)

    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()
print("🛑 Đã thoát.")
