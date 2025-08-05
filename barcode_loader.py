import pyzxing

def extract_largest_barcode(image_path):
    reader = pyzxing.BarCodeReader()
    result = reader.decode(image_path)

    if not result:
        print("❌ Không đọc được barcode")
        return None

    biggest = max(result, key=lambda r: r['bounds'][2] - r['bounds'][0])
    return biggest.get('parsed')
