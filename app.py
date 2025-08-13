# app.py
import os
import time
import csv
import cv2
import re
import datetime
import pandas as pd
import streamlit as st
from db import insert_event, get_shipments, get_events, upsert_device, ping
# ============ CẤU HÌNH ============
CSV_FILE = "tracking_log.csv"
VIDEO_DIR = "videos"
os.makedirs(VIDEO_DIR, exist_ok=True)

CLIP_DURATION = 4      # giây
FPS = 20

# ============ THƯ VIỆN QUÉT ============
# Barcode (ưu tiên)
try:
    from pyzbar.pyzbar import decode as zbar_decode
    HAVE_ZBAR = True
except Exception:
    HAVE_ZBAR = False

# OCR (fallback)
try:
    import easyocr
    HAVE_EASYOCR = True
except Exception:
    HAVE_EASYOCR = False

# ============ STREAMLIT SETUP ============
st.set_page_config(page_title="ScanLabelTracking", layout="wide")
st.title("ScanLabelTracking - Printiz")
# --- Kiểm tra DB và khai báo device ---
DB_OK, DB_ERR = ping(detail=True)
if not DB_OK:
    st.error(f"Không kết nối được MySQL: {DB_ERR}")
else:
    st.success("MySQL OK")

# Tạo/lấy device_id 1 lần cho phiên này (chỉ khi DB_OK)
DEVICE_ID = upsert_device(name="Cam 01 - Line A", location="Cổng ra", camera_info="USB Cam #0") if DB_OK else None

# ============ SESSION STATE ============
def _ensure_state():
    for k, v in {
        "TRY_FULLFRAME_BARCODE_EVERY_N": 10,
        "OCR_EVERY_N": 3,            # chỉ OCR mỗi N frame
        "BARCODE_PREP_EVERY_N": 2,   # chỉ tiền xử lý barcode mỗi N frame
        "frame_idx": 0,
        "running": False,
        "avoid_duplicates": True,
        "mirror": False,
        "scanned": set(),
        "stable_frames": 0,
        "last_seen_at": 0.0,
        "last_seen_code": None,
        "recording": False,
        "rec_writer": None,
        "rec_start_time": 0.0,
        "cap": None,
        "current_tracking": "",
        "current_video_path": "",
        "current_timestamp": "",
        "current_source": "",
        "easyocr_reader": None,
        "refresh_table": False,
        "_conf_sum": 0.0,
        "_conf_cnt": 0,
        "just_finalized_until": 0.0,
    }.items():
        if k not in st.session_state:
            st.session_state[k] = v

_ensure_state()

# ============ CSV HELPERS ============
def normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["tracking", "timestamp", "video", "status"])
    norm = {c: re.sub(r"\s+", "", str(c).lower()) for c in df.columns}
    df = df.rename(columns=norm)
    alias_map = {
        "tracking": ["tracking", "mã", "ma", "code"],
        "timestamp": ["timestamp", "time", "ngaygio", "datetime", "date"],
        "video": ["video", "videopath", "videolink", "link", "videourl"],
        "status": ["status", "trangthai", "state"],
    }
    def pick(col_aliases):
        for name in col_aliases:
            if name in df.columns:
                return name
        return None
    tr_col = pick(alias_map["tracking"]) or "tracking"
    ts_col = pick(alias_map["timestamp"]) or "timestamp"
    vi_col = pick(alias_map["video"]) or "video"
    st_col = pick(alias_map["status"]) or "status"
    for col in [tr_col, ts_col, vi_col, st_col]:
        if col not in df.columns:
            df[col] = ""
    df = df[[tr_col, ts_col, vi_col, st_col]]
    df.columns = ["tracking", "timestamp", "video", "status"]
    for c in ["tracking", "timestamp", "video", "status"]:
        df[c] = df[c].astype(str).fillna("")
    return df

def read_csv_sorted() -> pd.DataFrame:
    if not os.path.exists(CSV_FILE):
        return pd.DataFrame(columns=["tracking", "timestamp", "video", "status"])
    df = pd.read_csv(CSV_FILE, on_bad_lines="skip", dtype=str, encoding="utf-8")
    df = normalize_df(df)
    if "timestamp" in df.columns and df["timestamp"].str.len().gt(0).any():
        df = df.sort_values("timestamp", ascending=False, na_position="last")
    else:
        df = df.iloc[::-1]
    return df.reset_index(drop=True)

def append_csv_row(tracking: str, timestamp: str, video_path: str, status: str):
    row = {"tracking": f'="{tracking}"', "timestamp": timestamp, "video": video_path, "status": status}
    file_exists = os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["tracking", "timestamp", "video", "status"])
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)
# ============ DB HELPER ============
ALLOWED_STATUSES = {"finished","shipped","invalid","duplicate"}

def _normalize_status(s: str | None) -> str:
    if not s: 
        return "shipped"
    # "barcode_shipped" / "ocr_shipped" -> "shipped"
    if s.endswith("_shipped"):
        return "shipped"
    if s.endswith("_finished"):
        return "finished"
    return s if s in ALLOWED_STATUSES else "shipped"

def save_db_event(tracking_number: str, video_path: str, source: str,
                  confidence: float | None, stable_ms: int, status_label: str):
    status_db = _normalize_status(status_label)
    ok, info = insert_event(
        tracking=tracking_number,
        status=status_db,
        video_path=video_path,
        device_id=DEVICE_ID,
        source=source or "unknown",
        confidence=round(confidence * 100, 2) if isinstance(confidence, float) else confidence,
        duration_stable_ms=stable_ms,
        notes=None
    )
    if ok:
        st.success(f"Đã lưu DB: {tracking_number}")
    else:
        if info == "duplicate_event":
            st.info("Bỏ qua: sự kiện trùng (DB).")
        else:
            st.error(f"Lưu DB lỗi: {info}")

# ============ DELETE HELPERS ============
def delete_entry(ts: str, video_path: str, delete_file: bool = False) -> bool:
    """Xoá 1 dòng theo (timestamp, video). Trả True nếu xoá được ít nhất 1 dòng."""
    if not os.path.exists(CSV_FILE):
        return False
    try:
        df_raw = pd.read_csv(
            CSV_FILE,
            on_bad_lines="skip",
            dtype=str,
            encoding="utf-8",
            keep_default_na=False
        )
        df_raw = normalize_df(df_raw).fillna("")
        df_raw["timestamp"] = df_raw["timestamp"].astype(str)
        df_raw["video"]     = df_raw["video"].astype(str)

        want_ts  = str(ts)
        want_vid = os.path.normcase(os.path.normpath(str(video_path)))
        df_cmp   = df_raw.copy()
        df_cmp["__vid_norm"] = df_cmp["video"].apply(lambda p: os.path.normcase(os.path.normpath(p)))

        before = len(df_cmp)
        mask_keep = ~((df_cmp["timestamp"] == want_ts) & (df_cmp["__vid_norm"] == want_vid))
        df_new = df_raw[mask_keep]

        if len(df_new) == before:
            return False

        df_new.to_csv(CSV_FILE, index=False, encoding="utf-8")

        if delete_file and os.path.exists(video_path):
            try:
                os.remove(video_path)
            except Exception:
                pass
        return True
    except Exception as e:
        st.error(f"Không xoá được: {e}")
        return False

# ============ RENDER BẢNG (có nút xoá) ============
def render_table(placeholder):
    df = read_csv_sorted()
    with placeholder:
        st.markdown("### 📋 Danh sách Tracking")
        if df.empty:
            st.info("Chưa có bản ghi nào.")
            return

        # Bảng trên cùng
        df_display = df.copy()
        df_display["tracking"] = (
            df_display["tracking"].astype(str)
            .str.replace(r'^="', "", regex=True)
            .str.replace(r'"$', "", regex=True)
        )
        st.dataframe(df_display, use_container_width=True)

        delete_file_toggle = st.checkbox("🗑️ Xoá cả file video khi xoá bản ghi", value=False)

        # ===== Khung có thanh cuộn
        SCROLL_H = 420  # đổi chiều cao tuỳ ý
        with st.expander("⬇️ Tải video / Xoá", expanded=True):
            # anchor để CSS target đúng expander content
            st.markdown('<div id="dl-scroll-anchor"></div>', unsafe_allow_html=True)

            for i, row in df.iterrows():
                ts = str(row["timestamp"])
                vid_path = str(row["video"])
                tracking_clean = str(row["tracking"]).replace('="', "").removesuffix('"')

                cols = st.columns([3, 3, 4, 1, 1])
                cols[0].markdown(f"**Tracking:** {tracking_clean}")
                cols[1].markdown(f"**Time:** `{ts}`")

                if os.path.exists(vid_path):
                    with open(vid_path, "rb") as f:
                        video_bytes = f.read()
                    cols[2].download_button(
                        label=f"Tải {os.path.basename(vid_path)}",
                        data=video_bytes,
                        file_name=os.path.basename(vid_path),
                        mime="video/mp4",
                        key=f"dl-{i}-{ts}-{os.path.basename(vid_path)}",
                    )
                else:
                    cols[2].error("❌ File không tồn tại")

                cols[3].markdown(f"**{row['status']}**")

                if cols[4].button("Xoá", key=f"del-{i}-{ts}-{os.path.basename(vid_path)}"):
                    if delete_entry(ts, vid_path, delete_file_toggle):
                        st.success(f"Đã xoá tracking {tracking_clean}")
                        st.rerun()
                    else:
                        st.warning("Không tìm thấy bản ghi để xoá.")

            # CSS áp vào expander content (ổn định trên mọi bản Streamlit)
            st.markdown(f"""
            <style>
              /* giới hạn chiều cao + cuộn */
              .streamlit-expanderContent:has(#dl-scroll-anchor) {{
                  max-height: {SCROLL_H}px !important;
                  overflow-y: auto !important;
                  border: 1px solid rgba(49,51,63,.2);
                  border-radius: .5rem;
                  padding: 8px;
              }}
            </style>
            """, unsafe_allow_html=True)

# ============ VIDEO FINALIZER ============
def finalize_recording(status_label="shipped"):
    """Kết thúc ghi & (nếu có file) thì ghi log. Trả về True nếu có thêm bản ghi mới."""
    saved = False
    if st.session_state.recording and st.session_state.rec_writer:
        st.session_state.rec_writer.release()
        st.session_state.rec_writer = None
        st.session_state.recording = False

        vp = st.session_state.current_video_path
        if vp and os.path.exists(vp) and os.path.getsize(vp) > 0:
            # CSV
            append_csv_row(
                st.session_state.current_tracking,
                st.session_state.current_timestamp,
                vp,
                status_label,
            )
            saved = True
            st.session_state.refresh_table = True

            # [DB] ghi vào MySQL
            try:
                # conf trung bình trong thời gian ổn định (nếu có)
                avg_conf = (st.session_state._conf_sum / max(1, st.session_state._conf_cnt)) if st.session_state._conf_cnt else None
                # nếu là barcode thì conf=1.0 (100)
                if st.session_state.current_source == "barcode":
                    avg_conf = 1.0
                save_db_event(
                    tracking_number=st.session_state.current_tracking,
                    video_path=vp,
                    source=st.session_state.current_source,
                    confidence=avg_conf,
                    stable_ms=2000,  # tuỳ chỉnh nếu bạn đổi tham số ổn định
                    status_label=status_label
                )
            except Exception as e:
                st.error(f"DB exception: {e}")

    # Reset trạng thái + cooldown (luôn chạy)
    st.session_state.last_seen_code = None
    st.session_state.stable_frames = 0
    st.session_state._conf_sum = 0.0
    st.session_state._conf_cnt = 0
    st.session_state.current_source = ""
    st.session_state.just_finalized_until = time.time() + 0.4  # 400ms
    return saved

# ============ ROI ============
def get_rois(frame):
    h, w = frame.shape[:2]
    top_b = int(h * 0.65); bot_b = int(h * 0.85)   # barcode band
    top_t = int(h * 0.85); bot_t = int(h * 0.97)   # text band
    left = int(w * 0.10); right = int(w * 0.90)
    roi_bar = frame[top_b:bot_b, left:right]
    roi_txt = frame[top_t:bot_t, left:right]
    rect_bar = (left, top_b, right, bot_b)
    rect_txt = (left, top_t, right, bot_t)
    return roi_bar, rect_bar, roi_txt, rect_txt

# ============ BARCODE & OCR ============
def _prep_for_barcode(img):
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    g = cv2.normalize(g, None, 0, 255, cv2.NORM_MINMAX)
    g = cv2.medianBlur(g, 3)
    _, bw = cv2.threshold(g, 0, 255, cv2.THRESH_OTSU)
    return bw

def try_decode_barcode(frame, full_frame=None):
    if not HAVE_ZBAR:
        return None, 0.0

    codes = zbar_decode(frame)
    for c in codes:
        txt = c.data.decode("utf-8", errors="ignore")
        digits = re.sub(r"\D", "", txt)
        if 10 <= len(digits) <= 30:
            return digits, 1.0

    fi = st.session_state.frame_idx
    if fi % st.session_state.BARCODE_PREP_EVERY_N == 0:
        for cand in (_prep_for_barcode(frame), cv2.rotate(frame, cv2.ROTATE_180)):
            codes = zbar_decode(cand)
            for c in codes:
                txt = c.data.decode("utf-8", errors="ignore")
                digits = re.sub(r"\D", "", txt)
                if 10 <= len(digits) <= 30:
                    return digits, 1.0

    if full_frame is not None and fi % st.session_state.TRY_FULLFRAME_BARCODE_EVERY_N == 0:
        codes = zbar_decode(full_frame)
        for c in codes:
            txt = c.data.decode("utf-8", errors="ignore")
            digits = re.sub(r"\D", "", txt)
            if 10 <= len(digits) <= 30:
                return digits, 1.0

    return None, 0.0

def _prep_for_ocr(img):
    h, w = img.shape[:2]
    # cắt bớt phần trên/dưới – tuỳ nhãn của bạn, có thể chỉnh 0.20/0.80
    y1 = int(h * 0.15); y2 = int(h * 0.85)
    img = img[y1:y2, :]

    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # tăng tương phản
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
    g = clahe.apply(g)
    # phóng lớn hơn để OCR tốt hơn
    g = cv2.resize(g, None, fx=3.0, fy=3.0, interpolation=cv2.INTER_CUBIC)
    # unsharp mask (làm nét)
    blur = cv2.GaussianBlur(g, (0,0), 1.0)
    g = cv2.addWeighted(g, 1.5, blur, -0.5, 0)
    # nhị phân hoá + đóng mở để sạch nhiễu
    g = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                              cv2.THRESH_BINARY, 31, 10)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3,3))
    g = cv2.morphologyEx(g, cv2.MORPH_CLOSE, kernel, iterations=1)
    return g


def try_ocr(frame):
    if not HAVE_EASYOCR:
        return None, 0.0
    pre = _prep_for_ocr(frame)
    reader = st.session_state.easyocr_reader
    if reader is None:
        st.session_state.easyocr_reader = easyocr.Reader(["en"], gpu=False)
        reader = st.session_state.easyocr_reader
    results = reader.readtext(
        pre, detail=1, allowlist='0123456789',
        width_ths=0.7,  # gộp các box gần nhau
        link_threshold=0.3,
        text_threshold=0.7,
        low_text=0.4
    )
    best = (None, 0.0)
    for _, text, conf in results:
        digits = re.sub(r"\D", "", text)
        if 10 <= len(digits) <= 30 and conf > best[1]:
            best = (digits, float(conf))
    return best


# ============ UI TRÊN: TRÁI (điều khiển) / PHẢI (video) ============
left, right = st.columns([1, 2])

with left:
    st.session_state.avoid_duplicates = st.checkbox("🚫 Không ghi trùng mã vừa quét", value=st.session_state.avoid_duplicates)
    st.session_state.mirror = st.checkbox("🔁 Lật gương camera", value=st.session_state.mirror)

    st.markdown("### ⚙️ Nhạy & Ổn định")
    STABLE_FRAMES = st.slider("Số frame ổn định (OCR)", 3, 30, 8, 1)
    MIN_CONF      = st.slider("Ngưỡng tin cậy OCR", 0.50, 1.00, 0.80, 0.05)
    GRACE_MS      = st.slider("Grace khi mất mã (ms)", 0, 800, 300, 50)


with right:
    video_col = st.container()
    frame_placeholder = video_col.empty()
# --- Hàm tìm camera khả dụng ---
def detect_cameras(max_id=5):
    found = []
    for i in range(max_id + 1):
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)  # <-- thêm CAP_DSHOW
        if cap.isOpened():
            found.append(i)
        cap.release()
    return found

# --- Khởi tạo session_state ---
if "camera_id" not in st.session_state:
    st.session_state.camera_id = 0
if "camera_options" not in st.session_state:
    st.session_state.camera_options = detect_cameras(5) or [0]

# --- UI chọn camera ---
with left:
    st.markdown("### Camera")
    st.session_state.camera_id = st.selectbox(
        "Chọn camera",
        st.session_state.camera_options,
        index=st.session_state.camera_options.index(st.session_state.camera_id)
        if st.session_state.camera_id in st.session_state.camera_options else 0
    )
    if st.button("🔎 Quét camera khả dụng"):
        st.session_state.camera_options = detect_cameras(5) or [0]
        st.rerun()

    # rồi tới Start/Stop như mục (1)

# ====== CALLBACKS ======
def on_start():
    # Nếu Windows, dùng CAP_DSHOW (an toàn cho mọi nền tảng vì có fallback)
    st.session_state.cap = cv2.VideoCapture(st.session_state.camera_id, cv2.CAP_DSHOW)
    st.session_state.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    st.session_state.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

    if not st.session_state.cap.isOpened():
        st.error(f"Không mở được camera ID {st.session_state.camera_id}")
        st.session_state.running = False
        return

    st.session_state.running = True
    st.session_state.recording = False
    st.session_state.last_seen_code = None
    st.session_state.stable_frames = 0
    st.session_state.last_seen_at = 0.0
    st.session_state._conf_sum = 0.0
    st.session_state._conf_cnt = 0

def on_stop():
    if finalize_recording(f"{st.session_state.get('current_source','')}_shipped"):
        if st.session_state.avoid_duplicates and st.session_state.current_tracking:
            st.session_state.scanned.add(st.session_state.current_tracking)
    st.session_state.running = False
    if st.session_state.cap:
        st.session_state.cap.release()
        st.session_state.cap = None
    if st.session_state.rec_writer:
        st.session_state.rec_writer.release()
        st.session_state.rec_writer = None
    st.session_state.recording = False

with left:
    if st.button("▶️ Bắt đầu", disabled=st.session_state.running):
        on_start()
    if st.button("⏹️ Dừng", disabled=not st.session_state.running):
        on_stop()

# ====== BẢNG LOG Ở DƯỚI ======
st.markdown("---")
table_placeholder = st.container()
render_table(table_placeholder)
# ====== Xem nhanh từ MySQL ======
with st.expander("📦 Danh sách lô hàng (last status)"):
    search = st.text_input("Tìm theo tracking (optional)")
    rows = get_shipments(limit=200, search=search or None)
    df_rows = pd.DataFrame(rows)
    if not df_rows.empty:
        df_rows = df_rows.drop(columns=["last_confidence"], errors="ignore")
    st.dataframe(df_rows, use_container_width=True)

with st.expander("🧾 Lịch sử quét theo tracking"):
    q = st.text_input("Nhập tracking để xem lịch sử")
    if q:
        history = get_events(q, limit=200)
        df_hist = pd.DataFrame(history)
        if not df_hist.empty:
            df_hist = df_hist.drop(columns=["confidence"], errors="ignore")
        st.dataframe(df_hist, use_container_width=True)


# ============ LOOP CAMERA ============
if st.session_state.running and st.session_state.cap and st.session_state.cap.isOpened():
    cap = st.session_state.cap

    while st.session_state.running:
        ret, frame = cap.read()
        if not ret:
            st.warning("⚠️ Không đọc được frame từ camera. Đang dừng.")
            finalize_recording(f"{st.session_state.get('current_source','')}_shipped")
            st.session_state.running = False
            break

        if st.session_state.mirror:
            frame = cv2.flip(frame, 1)

        roi_bar, rect_bar, roi_txt, rect_txt = get_rois(frame)

        tracking, conf, source = None, 0.0, None
        code_b, conf_b = try_decode_barcode(roi_bar, full_frame=frame)
        if code_b:
            tracking, conf, source = code_b, conf_b, "barcode"
        else:
            if st.session_state.frame_idx % st.session_state.OCR_EVERY_N == 0:
                code_t, conf_t = try_ocr(roi_txt)
                if code_t:
                    tracking, conf, source = code_t, conf_t, "ocr"
            else:
                if st.session_state.last_seen_code:
                    tracking, conf, source = st.session_state.last_seen_code, 0.0, "ocr"

        roi_color = (0, 255, 0)  # READY

        # COOLDOWN sau khi finalize
        now = time.time()
        if st.session_state.just_finalized_until > now:
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            frame_placeholder.image(frame_rgb, channels="RGB")
            st.session_state.frame_idx += 1
            time.sleep(0.001)
            continue

        if tracking:
            if st.session_state.avoid_duplicates and tracking in st.session_state.scanned and not st.session_state.recording:
                roi_color = (0, 255, 0)
            else:
                same_as_last = (tracking == st.session_state.last_seen_code)
                if source == "barcode" and not st.session_state.recording:
                    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                    file_name = f"{tracking}_{ts}.mp4"
                    video_path = os.path.join(VIDEO_DIR, file_name)
                    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                    h, w = frame.shape[:2]
                    writer = cv2.VideoWriter(video_path, fourcc, FPS, (w, h))

                    st.session_state.rec_writer = writer
                    st.session_state.rec_start_time = now
                    st.session_state.recording = True
                    st.session_state.current_tracking = tracking
                    st.session_state.current_video_path = video_path
                    st.session_state.current_timestamp = ts
                    st.session_state.current_source = source
                    roi_color = (0, 0, 255)
                else:
                    if not same_as_last:
                        st.session_state.last_seen_code = tracking
                        st.session_state.stable_frames = 0
                        st.session_state.last_seen_at = now
                        st.session_state._conf_sum = 0.0
                        st.session_state._conf_cnt = 0

                    st.session_state._conf_sum += max(conf, 0.0)
                    st.session_state._conf_cnt += 1
                    avg_conf = (st.session_state._conf_sum / max(1, st.session_state._conf_cnt))

                    if conf >= MIN_CONF:
                        st.session_state.stable_frames += 1
                        st.session_state.last_seen_at = now

                    if not st.session_state.recording:
                        if (now - st.session_state.last_seen_at) * 1000 > GRACE_MS:
                            st.session_state.stable_frames = 0
                            st.session_state.last_seen_code = None
                            st.session_state._conf_sum = 0.0
                            st.session_state._conf_cnt = 0
                        elif (st.session_state.stable_frames >= STABLE_FRAMES) or (avg_conf >= 0.70):
                            ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                            file_name = f"{tracking}_{ts}.mp4"
                            video_path = os.path.join(VIDEO_DIR, file_name)
                            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                            h, w = frame.shape[:2]
                            writer = cv2.VideoWriter(video_path, fourcc, FPS, (w, h))

                            st.session_state.rec_writer = writer
                            st.session_state.rec_start_time = now
                            st.session_state.recording = True
                            st.session_state.current_tracking = tracking
                            st.session_state.current_video_path = video_path
                            st.session_state.current_timestamp = ts
                            st.session_state.current_source = source
                            roi_color = (0, 0, 255)
                        else:
                            roi_color = (0, 255, 255)  # HOLD STEADY...
        else:
            if (now - st.session_state.last_seen_at) * 1000 > GRACE_MS:
                st.session_state.last_seen_code = None
                st.session_state.stable_frames = 0

        # Nếu đang ghi → chỉ ghi frame, không OCR/barcode
        if st.session_state.recording:
            st.session_state.rec_writer.write(frame)

            # đủ thời lượng thì finalize
            if (now - st.session_state.rec_start_time) >= CLIP_DURATION:
                if finalize_recording(f"{st.session_state.current_source}_shipped" if st.session_state.current_source else "shipped"):
                    if st.session_state.avoid_duplicates:
                        st.session_state.scanned.add(st.session_state.current_tracking)

            # VẼ label TRƯỚC rồi mới convert RGB
            (l2, t2, r2, b2) = get_rois(frame)[3]
            cv2.putText(frame, "RECORDING...", (l2, t2 - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)

            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            frame_placeholder.image(frame_rgb, channels="RGB")

            st.session_state.frame_idx += 1
            time.sleep(0.001)
            continue

        # Vẽ 2 ROI + nhãn
        (l1, t1, r1, b1) = rect_bar
        (l2, t2, r2, b2) = rect_txt
        cv2.rectangle(frame, (l1, t1), (r1, b1), roi_color, 2)
        cv2.rectangle(frame, (l2, t2), (r2, b2), roi_color, 2)
        label = "READY"
        if roi_color == (0, 255, 255): label = "HOLD STEADY..."
        if roi_color == (0, 0, 255):   label = "RECORDING..."
        cv2.putText(frame, label, (l2, t2 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.7, roi_color, 2)

        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        frame_placeholder.image(frame_rgb, channels="RGB")

        st.session_state.frame_idx += 1
        time.sleep(0.001)

# # ===== Refresh bảng 1 lần/turn =====
# if st.session_state.get("refresh_table"):
#     table_placeholder.empty()
#     render_table(table_placeholder)
#     st.session_state.refresh_table = False