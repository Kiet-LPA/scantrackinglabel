import pyzxing
import subprocess
import os

def read_barcode_from_image(image_path):
    reader = pyzxing.BarCodeReader(jar_path="zxing.jar")
    result = reader.decode(image_path)

    if result and 'parsed' in result[0]:
        return result[0]['parsed']
    return None

def scan_tracking_from_image(image_path):
    try:
        # Gọi thư viện ZXing qua .jar
        result = subprocess.run(
            ["java", "-jar", "zxing.jar", image_path],
            capture_output=True, text=True
        )
        output = result.stdout.strip()
        if output:
            tracking = output.split(":")[-1].strip()
            return tracking
        return None
    except Exception as e:
        print(f"❌ Lỗi quét barcode: {e}")
        return None
