import os
from pydrive.auth import GoogleAuth
from pydrive.drive import GoogleDrive
try:
    from pydrive.auth import GoogleAuth
    from pydrive.drive import GoogleDrive
except ImportError as e:
    print("⚠️ Lỗi import pydrive:", e)

# Authenticate only once
gauth = GoogleAuth()
gauth.LocalWebserverAuth()
drive = GoogleDrive(gauth)

# Thư mục Drive bạn muốn upload vào
DRIVE_FOLDER_ID = "1oADFF3T7zuuXXUO_sIDWkZ1NDkSEHAYT"

def upload_to_drive(filepath):
    file = drive.CreateFile({
        'title': os.path.basename(filepath),
        'parents': [{'id': DRIVE_FOLDER_ID}]
    })
    file.SetContentFile(filepath)
    file.Upload()
    print(f"✅ Uploaded to Drive: {file['alternateLink']}")
    return file['alternateLink']
