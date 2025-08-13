# db.py
import os
import datetime
import mysql.connector
from mysql.connector import Error
from dotenv import load_dotenv
from dotenv import find_dotenv
# Load biến môi trường .env (an toàn hơn hardcode)
load_dotenv()

def get_conn():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER", "scanlabel"),
        password=os.getenv("DB_PASS", "Strong_Pass_ChangeMe!"),
        database=os.getenv("DB_NAME", "scan_label"),
        connection_timeout=5,
    )

def ping(detail: bool = False):
    try:
        cn = get_conn()
        cn.close()
        return (True, "") if detail else True
    except Error as e:
        msg = f"{getattr(e,'errno', '')} {e}"
        return (False, msg) if detail else False
# ========== WRITE ==========
def insert_event(
    tracking: str,
    status: str,
    video_path: str | None = None,
    device_id: int | None = None,
    source: str = "unknown",
    confidence: float | None = None,
    duration_stable_ms: int | None = None,
    notes: str | None = None,
    occurred_at: datetime.datetime | None = None,
):
    """
    Ghi 1 sự kiện mới vào tracking_events.
    Trigger ở DB sẽ tự cập nhật bảng shipments (last status).
    Trả: (ok: bool, info: lastrowid | 'duplicate_event' | 'error message')
    """
    if occurred_at is None:
        # DATETIME(3) — lấy tới ms
        occurred_at = datetime.datetime.now()

    sql = """
    INSERT INTO tracking_events
      (tracking, occurred_at, status, video_path, device_id, source, confidence, duration_stable_ms, notes)
    VALUES (%s, %s(3), %s, %s, %s, %s, %s, %s, %s)
    """ .replace("%s(3)", "%s")  # occurred_at đã là datetime; MySQL sẽ nhận DATETIME(3) chuẩn theo schema

    args = (
        tracking,
        occurred_at.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        status,
        video_path,
        device_id,
        source,
        confidence,
        duration_stable_ms,
        notes,
    )

    cn = get_conn()
    try:
        cur = cn.cursor()
        cur.execute(sql, args)
        cn.commit()
        return True, cur.lastrowid
    except Error as e:
        # 1062: duplicate key (trùng uq_event_hash) -> coi như sự kiện trùng, bỏ qua
        if getattr(e, "errno", None) == 1062:
            return False, "duplicate_event"
        return False, str(e)
    finally:
        try:
            cur.close()
        except Exception:
            pass
        cn.close()

# ========== READ ==========
def get_shipments(limit: int = 200, search: str | None = None):
    cn = get_conn()
    try:
        cur = cn.cursor(dictionary=True)
        if search:
            cur.execute(
                """
                SELECT tracking, last_status, last_seen_at, last_video_path, last_device_id
                FROM shipments
                WHERE tracking LIKE %s
                ORDER BY last_seen_at DESC
                LIMIT %s
                """,
                (f"%{search}%", limit),
            )
        else:
            cur.execute(
                """
                SELECT tracking, last_status, last_seen_at, last_video_path, last_device_id
                FROM shipments
                ORDER BY last_seen_at DESC
                LIMIT %s
                """,
                (limit,),
            )
        return cur.fetchall()
    finally:
        try: cur.close()
        except: pass
        cn.close()

def get_events(tracking: str, limit: int = 200):
    """
    Lấy lịch sử chi tiết các lần quét của 1 tracking trong tracking_events.
    """
    cn = get_conn()
    try:
        cur = cn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT id, tracking, occurred_at, status, video_path, device_id, source, confidence, duration_stable_ms, notes
            FROM tracking_events
            WHERE tracking = %s
            ORDER BY occurred_at DESC
            LIMIT %s
            """,
            (tracking, limit),
        )
        return cur.fetchall()
    finally:
        try:
            cur.close()
        except Exception:
            pass
        cn.close()

def upsert_device(name: str, location: str | None = None, camera_info: str | None = None):
    """
    Thêm nhanh 1 thiết bị nếu chưa có (dùng khi bạn muốn lưu device_id cho event).
    Trả: device_id
    """
    cn = get_conn()
    try:
        cur = cn.cursor()
        # thử tìm
        cur.execute("SELECT id FROM devices WHERE name=%s LIMIT 1", (name,))
        row = cur.fetchone()
        if row:
            return row[0]
        # chưa có -> insert
        cur.execute(
            "INSERT INTO devices (name, location, camera_info) VALUES (%s, %s, %s)",
            (name, location, camera_info),
        )
        cn.commit()
        return cur.lastrowid
    finally:
        try:
            cur.close()
        except Exception:
            pass
        cn.close()
