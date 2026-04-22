from pathlib import Path
import shutil

assets_dir = Path(__file__).parent.resolve() / "assets"

def configure_ocr_model():
    # 检查源 OCR 资源主目录是否存在
    assets_ocr_dir = assets_dir / "MaaCommonAssets" / "OCR"
    if not assets_ocr_dir.exists():
        print(f"File Not Found: {assets_ocr_dir}")
        exit(1)

    ocr_dir = assets_dir / "resource" / "base" / "model" / "ocr"
    ocr_dir.mkdir(parents=True, exist_ok=True)

    ppocr_dir = assets_ocr_dir / "ppocr_v4"

    # det: 移动版 (zh_cn，无后缀文件夹)
    det_src = ppocr_dir / "zh_cn" / "det.onnx"
    det_dst = ocr_dir / "det.onnx"
    print(f"正在复制 det 模型 (移动版): {det_src} ...")
    shutil.copy2(det_src, det_dst)

    # rec: 服务端版 (zh_cn-server/model.onnx -> rec.onnx)
    rec_src = ppocr_dir / "zh_cn-server" / "rec.onnx"
    rec_dst = ocr_dir / "rec.onnx"
    print(f"正在复制 rec 模型 (服务端版): {rec_src} ...")
    shutil.copy2(rec_src, rec_dst)

    # keys: 字典文件 (zh_cn-server/keys.txt)
    keys_src = ppocr_dir / "zh_cn-server" / "keys.txt"
    keys_dst = ocr_dir / "keys.txt"
    print(f"正在复制字典文件: {keys_src} ...")
    shutil.copy2(keys_src, keys_dst)

    print("✅ OCR 模型复制完成！")

if __name__ == "__main__":
    configure_ocr_model()
    print("OCR model configured.")