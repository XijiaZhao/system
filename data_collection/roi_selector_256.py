# -*- coding: utf-8 -*-
"""
lipbuild 256x256 嘴唇裁剪标定脚本

支持：
  - 从图片文件选择ROI
  - 从摄像头直接选择ROI

输出：
  - 固定 256x256 的裁剪坐标 LIP_X1/LIP_Y1/LIP_X2/LIP_Y2

使用：
  python scripts/roi_selector_256.py --image "G:\test_capture.png"
  python scripts/roi_selector_256.py --camera --preview-dir "G:\lip_preview"
"""

import argparse
from pathlib import Path

import cv2


TARGET_SIZE = 256
MAX_DISPLAY_WIDTH = 1600
MAX_DISPLAY_HEIGHT = 900


def parse_args():
    parser = argparse.ArgumentParser(description="lipbuild 256x256 嘴唇裁剪标定工具")
    parser.add_argument("--image", type=str, default=None,
                        help="待标定图片路径，若不指定则使用摄像头")
    parser.add_argument("--camera", action="store_true",
                        help="使用摄像头实时画面进行 ROI 选择")
    parser.add_argument("--preview-dir", type=str, default=None,
                        help="保存裁剪预览图的目录")
    return parser.parse_args()


def load_image(image_path):
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(f"图片不存在：{image_path}")
    img = cv2.imread(str(image_path))
    if img is None:
        raise RuntimeError(f"无法读取图片：{image_path}")
    return img


def capture_camera_frame():
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    if not cap.isOpened():
        raise RuntimeError("无法打开摄像头，请检查设备和驱动")
    ret, frame = cap.read()
    cap.release()
    if not ret or frame is None:
        raise RuntimeError("摄像头读取画面失败")
    return frame


def scale_image_for_display(img):
    h, w = img.shape[:2]
    scale = min(1.0, MAX_DISPLAY_WIDTH / w, MAX_DISPLAY_HEIGHT / h)
    if scale < 1.0:
        return cv2.resize(img, (int(w * scale), int(h * scale))), scale
    return img.copy(), 1.0


def fix_square_roi(cx, cy, img_w, img_h, size=TARGET_SIZE):
    half = size // 2
    x1 = max(0, cx - half)
    y1 = max(0, cy - half)
    x2 = x1 + size
    y2 = y1 + size
    if x2 > img_w:
        x2 = img_w
        x1 = max(0, x2 - size)
    if y2 > img_h:
        y2 = img_h
        y1 = max(0, y2 - size)
    return x1, y1, x2, y2


def save_preview_images(crop, preview_dir):
    preview_dir = Path(preview_dir)
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_files = []
    raw_path = preview_dir / f"lip_crop_{TARGET_SIZE}x{TARGET_SIZE}.png"
    cv2.imwrite(str(raw_path), crop)
    preview_files.append(raw_path)

    for size in [128, 256, 480]:
        out_path = preview_dir / f"lip_crop_{size}x{size}.png"
        resized = cv2.resize(crop, (size, size), interpolation=cv2.INTER_LANCZOS4)
        cv2.imwrite(str(out_path), resized)
        preview_files.append(out_path)
    return preview_files


def main():
    args = parse_args()

    if args.image is None and not args.camera:
        raise SystemExit("请指定 --image 图片路径，或使用 --camera 从摄像头选择")

    if args.image:
        img = load_image(args.image)
        print(f"加载图片：{args.image}，尺寸 {img.shape[1]}x{img.shape[0]}")
    else:
        img = capture_camera_frame()
        print(f"摄像头画面尺寸：{img.shape[1]}x{img.shape[0]}")

    display_img, scale = scale_image_for_display(img)
    window_name = "lipbuild 256x256 ROI 选择"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    roi = cv2.selectROI(window_name, display_img, fromCenter=False, showCrosshair=True)
    cv2.destroyAllWindows()

    if roi == (0, 0, 0, 0):
        raise SystemExit("未选择区域，程序退出")

    rx, ry, rw, rh = roi
    x1 = int(rx / scale)
    y1 = int(ry / scale)
    x2 = int((rx + rw) / scale)
    y2 = int((ry + rh) / scale)

    cx = x1 + (x2 - x1) // 2
    cy = y1 + (y2 - y1) // 2
    x1, y1, x2, y2 = fix_square_roi(cx, cy, img.shape[1], img.shape[0])

    print("\n=== 256x256 裁剪坐标 ===")
    print(f"LIP_X1 = {x1}")
    print(f"LIP_Y1 = {y1}")
    print(f"LIP_X2 = {x2}")
    print(f"LIP_Y2 = {y2}")
    print(f"裁剪尺寸 = {x2 - x1}x{y2 - y1}")

    crop = img[y1:y2, x1:x2]
    if crop.shape[0] != TARGET_SIZE or crop.shape[1] != TARGET_SIZE:
        raise RuntimeError("裁剪结果尺寸异常，请重新选择区域")

    if args.preview_dir:
        previews = save_preview_images(crop, args.preview_dir)
        print("已保存预览图：")
        for p in previews:
            print(f"  {p}")

    print("\n请将上述 LIP_X1/LIP_Y1/LIP_X2/LIP_Y2 写入采集脚本中的裁剪参数。")


if __name__ == '__main__':
    main()
