import os
import pandas as pd
from datetime import datetime

LOG_FILE = "log.xlsx"

def init_log():
    if not os.path.exists(LOG_FILE):
        df = pd.DataFrame(columns=["Tracking", "Timestamp", "Video Link", "Status"])
        df.to_excel(LOG_FILE, index=False)

def add_log(tracking, video_link, status="shipped", log_file=LOG_FILE):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_data = {"Tracking": tracking, "Timestamp": timestamp, "Video Link": video_link, "Status": status}

    if not os.path.exists(log_file):
        pd.DataFrame(columns=new_data.keys()).to_excel(log_file, index=False)

    df = pd.read_excel(log_file)
    df = pd.concat([df, pd.DataFrame([new_data])], ignore_index=True)
    df.to_excel(log_file, index=False)
    print("✅ Ghi log:", new_data)
