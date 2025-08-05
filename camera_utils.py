import cv2
import time
import os

def record_video(tracking_code="unknown", save_dir="videos", duration=3):
    os.makedirs(save_dir, exist_ok=True)
    cap = cv2.VideoCapture(0)

    if not cap.isOpened():
        print("❌ Không mở được camera.")
        return None

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    video_path = os.path.join(save_dir, f"{tracking_code}_{timestamp}.avi")

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    out = cv2.VideoWriter(video_path, cv2.VideoWriter_fourcc(*'XVID'), 10, (width, height))

    print(f"📹 Ghi video: {video_path}")

    start_time = time.time()
    while time.time() - start_time < duration:
        ret, frame = cap.read()
        if ret:
            out.write(frame)

    cap.release()
    out.release()
    return video_path
