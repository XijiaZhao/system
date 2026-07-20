# -*- coding: utf-8 -*-
"""
真机采集脚本 v5.0 - 分组断点续采版
- 相机：海康 MV-CU060-10UC
- 舵机：Hiwonder LSC-32，13个唇部舵机
- 采样：固定点位全组合遍历，38880张
- 特性：分6组采集，每组6480张约3.6小时，支持断点续采

使用方法：
  python collect_final.py        # 自动从断点继续
  python collect_final.py --test # 只采10张测试
  python collect_final.py --group 1  # 只采第1组（第1~6480张）
"""

import sys
import ctypes
import serial
import cv2
import numpy as np
import time
import json
import itertools
import argparse
from datetime import datetime
from pathlib import Path

# ========== 海康SDK路径 ==========
SDK_PATH = r"C:\Program Files (x86)\MVS\Development\Samples\Python\MvImport"
sys.path.append(SDK_PATH)
from MvCameraControl_class import *

# ========== 配置参数 ==========
SERIAL_PORT    = 'COM7'
BAUDRATE       = 9600
OUT_DIR        = Path(r"G:\lipbuild\data\facesetC")

EXPOSURE_TIME  = 70000.0
GAIN           = 0.0
IMG_W          = 3072
IMG_H          = 2048

FACE_X1, FACE_Y1 = 600,  0
FACE_X2, FACE_Y2 = 2400, 1800
LIP_X1,  LIP_Y1  = 1273, 1142
LIP_X2,  LIP_Y2  = 1758, 1627
FACE_OUT_SIZE    = 480
LIP_OUT_SIZE     = 256

SERVO_WAIT_TIME  = 1.0
SERVO_MOVE_TIME  = 500
MAX_RANGE        = 180

SERVO_CONFIG = {
    15: (1050, 1170, 1085, "上嘴唇中间(前后)"),
    16: (1580, 1680, 1685, "上嘴唇中间(上下)"),
    17: (1985, 2110, 2041, "下嘴唇中间(前后)"),
    18: (1370, 1480, 1433, "下嘴唇中间(上下)"),
    19: (1001, 1050, 1025, "上嘴唇左边(前后)"),
    20: (1891, 1940, 1915, "上嘴唇右边(前后)"),
    21: (1300, 1470, 1385, "下嘴唇左边(上下)"),
    22: (1544, 1714, 1629, "下嘴唇右边(上下)"),
    23: (1596, 1680, 1638, "左嘴角(前后收缩)"),
    24: (1869, 2049, 1959, "左嘴角(上下)"),
    25: (1349, 1433, 1391, "右嘴角(前后收缩)"),
    26: (1040, 1220, 1130, "右边嘴角(上下)"),
    27: (1505, 1630, 1567, "下巴上下"),
}
ACTIVE_SERVO_IDS = list(SERVO_CONFIG.keys())

# 分组设置：每组6480张，共6组
GROUP_SIZE = 6480
TOTAL      = 38880

# ========== 遍历点位 ==========
PAIRED_POINTS = {
    (24, 26): [(0, 1.0),   (0.25, 0.75), (0.5, 0.5), (0.75, 0.25), (1.0, 0)],
    (21, 22): [(0, 0.944), (0.3, 0.644), (0.6, 0.344), (0.944, 0)],
    (23, 25): [(0, 0.467), (0.23, 0.237), (0.467, 0)],
    (19, 20): [(0, 0.272), (0.272, 0)],
}
SINGLE_POINTS = {
    17: [0, 0.35, 0.694],
    27: [0, 0.23, 0.46, 0.694],
    15: [0, 0.33, 0.667],
    18: [0, 0.3,  0.611],
    16: [0, 0.28, 0.556],
}


def generate_all_combinations():
    all_dims = list(PAIRED_POINTS.values()) + list(SINGLE_POINTS.values())
    combos = []
    for combo in itertools.product(*all_dims):
        cmd = {}
        for i, (s1, s2) in enumerate(PAIRED_POINTS.keys()):
            cmd[s1], cmd[s2] = float(combo[i][0]), float(combo[i][1])
        offset = len(PAIRED_POINTS)
        for i, sid in enumerate(SINGLE_POINTS.keys()):
            cmd[sid] = float(combo[offset + i])
        combos.append(cmd)
    return combos


def norm_to_pwm(sid, val):
    mn, mx, _, _ = SERVO_CONFIG[sid]
    return int(round(np.clip(mn + val * MAX_RANGE, mn, mx)))


def build_servo_command(positions, move_time_ms):
    n = len(positions)
    cmd = bytearray([0x55, 0x55, n*3+5, 0x03, n,
                     move_time_ms & 0xFF, (move_time_ms >> 8) & 0xFF])
    for sid, pos in positions.items():
        pos = max(0, min(4000, int(pos)))
        cmd += bytes([sid, pos & 0xFF, (pos >> 8) & 0xFF])
    return bytes(cmd)


def send_servo_command(ser, cmd_dict):
    positions = {sid: norm_to_pwm(sid, val) for sid, val in cmd_dict.items()}
    ser.write(build_servo_command(positions, SERVO_MOVE_TIME))
    ser.flush()
    time.sleep(SERVO_WAIT_TIME)
    # 调试日志：打印发送的PWM值
    print(f"  → 舵机命令已发送: {positions}", end='')


def reset_servos(ser):
    positions = {sid: cfg[2] for sid, cfg in SERVO_CONFIG.items()}
    ser.write(build_servo_command(positions, 1000))
    ser.flush()
    time.sleep(1.5)
    print("舵机已归位")


def init_camera():
    MvCamera.MV_CC_Initialize()
    deviceList = MV_CC_DEVICE_INFO_LIST()
    ret = MvCamera.MV_CC_EnumDevices(MV_GIGE_DEVICE | MV_USB_DEVICE, deviceList)
    if ret != 0 or deviceList.nDeviceNum == 0:
        print("[错误] 未找到海康相机")
        return None
    cam = MvCamera()
    stDeviceList = cast(deviceList.pDeviceInfo[0], POINTER(MV_CC_DEVICE_INFO)).contents
    cam.MV_CC_CreateHandle(stDeviceList)
    cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
    cam.MV_CC_SetFloatValue("ExposureTime", EXPOSURE_TIME)
    cam.MV_CC_SetFloatValue("Gain", GAIN)
    cam.MV_CC_StartGrabbing()
    time.sleep(2.0)
    print("相机初始化完成")
    return cam


def capture_frame(cam):
    stFrameInfo = MV_FRAME_OUT_INFO_EX()
    pData = (ctypes.c_ubyte * (IMG_W * IMG_H * 3))()
    ret = cam.MV_CC_GetOneFrameTimeout(pData, IMG_W * IMG_H * 3, stFrameInfo, 3000)
    if ret != 0:
        return None
    w, h = stFrameInfo.nWidth, stFrameInfo.nHeight
    raw = np.frombuffer(pData, dtype=np.uint8, count=w*h).reshape(h, w)
    return cv2.cvtColor(raw, cv2.COLOR_BayerRG2RGB)


def close_camera(cam):
    cam.MV_CC_StopGrabbing()
    cam.MV_CC_CloseDevice()
    cam.MV_CC_DestroyHandle()
    MvCamera.MV_CC_Finalize()


def crop_face(frame):
    return cv2.resize(frame[FACE_Y1:FACE_Y2, FACE_X1:FACE_X2],
                      (FACE_OUT_SIZE, FACE_OUT_SIZE), interpolation=cv2.INTER_AREA)


def crop_lip(frame):
    return cv2.resize(frame[LIP_Y1:LIP_Y2, LIP_X1:LIP_X2],
                      (LIP_OUT_SIZE, LIP_OUT_SIZE), interpolation=cv2.INTER_AREA)


def save_image(path, image, ext=".jpg"):
    ok, buf = cv2.imencode(ext, image)
    if ok:
        with open(path, "wb") as f:
            f.write(buf.tobytes())
    return ok


def get_existing_count(lip_dir):
    """检查已采集张数（断点续采用）"""
    existing = sorted(lip_dir.glob("*_lip.jpg"))
    return len(existing)


def save_npz_and_json(out_dir, lip_dir, cmd_dir):
    """扫描已有文件重新打包npz和json（每组结束后调用）"""
    lip_files = sorted(lip_dir.glob("*_lip.jpg"))
    if not lip_files:
        return

    # 读取所有嘴唇图
    lips = []
    for f in lip_files:
        img = cv2.imread(str(f))
        if img is not None:
            lips.append(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))

    lips_arr = np.stack(lips, axis=0)
    np.savez_compressed(out_dir / "images.npz", images=lips_arr)
    print(f"  images.npz 更新: {lips_arr.shape}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--test',  action='store_true', help='只采10张测试')
    parser.add_argument('--group', type=int, default=0,
                        help='指定采集组号(1~6)，0表示自动从断点继续')
    args = parser.parse_args()

    # 创建输出目录
    UNCROP_DIR = OUT_DIR / "uncropped"
    FACE_DIR   = OUT_DIR / "face_480"
    LIP_DIR    = OUT_DIR / "lip_256"
    JSON_DIR   = OUT_DIR / "json_records"
    for d in [UNCROP_DIR, FACE_DIR, LIP_DIR, JSON_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    # 生成所有组合
    all_combos = generate_all_combinations()
    print(f"总组合数: {len(all_combos)} 张")

    # 确定采集范围
    if args.test:
        combos_to_run = all_combos[:10]
        start_idx = 0
        print("=== 测试模式：采集前10张 ===")
    elif args.group > 0:
        g = args.group
        start_idx = (g - 1) * GROUP_SIZE
        end_idx   = min(g * GROUP_SIZE, TOTAL)
        combos_to_run = all_combos[start_idx:end_idx]
        print(f"=== 第{g}组：第{start_idx+1}~{end_idx}张，共{len(combos_to_run)}张 ===")
    else:
        # 自动断点续采
        existing = get_existing_count(LIP_DIR)
        start_idx = existing
        combos_to_run = all_combos[existing:]
        cur_group = existing // GROUP_SIZE + 1
        print(f"=== 断点续采：已有{existing}张，从第{existing+1}张继续（第{cur_group}组）===")

    if not combos_to_run:
        print("所有组合已采集完毕！")
        return

    eta_hours = len(combos_to_run) * 2 / 3600
    print(f"本次采集: {len(combos_to_run)} 张，预计约 {eta_hours:.1f} 小时")

    # 初始化串口
    try:
        ser = serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1)
        print(f"串口 {SERIAL_PORT} 打开成功")
    except Exception as e:
        print(f"[错误] 无法打开串口: {e}")
        return

    # 初始化相机
    cam = init_camera()
    if cam is None:
        ser.close()
        return

    print("舵机归位中...")
    reset_servos(ser)

    records = []
    success_count = 0

    print(f"开始采集...")
    for i, cmd_dict in enumerate(combos_to_run):
        global_idx = start_idx + i  # 全局索引（0-based）
        file_idx   = global_idx + 1  # 文件命名（1-based）

        print(f"\n[{i+1}/{len(combos_to_run)}] 发送舵机命令...", end='')
        send_servo_command(ser, cmd_dict)

        frame = capture_frame(cam)
        if frame is None:
            print(f"\n[警告] 第{file_idx}张抓帧失败，跳过")
            continue

        print(f" 抓帧成功", end=' ')
        face_img = crop_face(frame)
        lip_img  = crop_lip(frame)
        lip_rgb  = cv2.cvtColor(lip_img, cv2.COLOR_BGR2RGB)

        uncrop_name = f"{file_idx:05d}.jpg"
        face_name   = f"{file_idx:05d}_face.jpg"
        lip_name    = f"{file_idx:05d}_lip.jpg"

        save_image(UNCROP_DIR / uncrop_name, frame,   ".jpg")
        save_image(FACE_DIR   / face_name,  face_img, ".jpg")
        save_image(LIP_DIR    / lip_name,   lip_img,  ".jpg")

        cmd_vec = np.array([cmd_dict.get(sid, 0.0) for sid in ACTIVE_SERVO_IDS],
                           dtype=np.float32)
        records.append({
            "idx":       file_idx,
            "uncropped": uncrop_name,
            "face":      face_name,
            "lip":       lip_name,
            "timestamp": datetime.now().isoformat(),
            "command":   [float(x) for x in cmd_vec.tolist()]
        })
        success_count += 1
        print(f"保存完成")

        # 每100张保存一次JSON（防止意外丢数据）
        if success_count % 100 == 0:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            tmp_json = JSON_DIR / f"records_{file_idx:05d}_{ts}.json"
            with open(tmp_json, 'w', encoding='utf-8') as f:
                json.dump(records, f, ensure_ascii=False)

        # 进度显示
        pct = (i + 1) * 100 // len(combos_to_run)
        elapsed = (i + 1) * 2
        remaining = (len(combos_to_run) - i - 1) * 2
        print(f"进度: {file_idx}/{start_idx+len(combos_to_run)}  "
              f"[{pct}%]  "
              f"已用:{elapsed//3600}h{elapsed%3600//60}m  "
              f"剩余:{remaining//3600}h{remaining%3600//60}m",
              end='\r')

    print(f"\n本次采集完成，成功 {success_count} 张")

    # 归位
    print("舵机归位中...")
    reset_servos(ser)

    # 保存本组JSON
    if records:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_path = JSON_DIR / f"records_final_{ts}.json"
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        print(f"JSON已保存: {json_path.name}")

    # 更新NPZ（合并所有已采图片）
    print("更新images.npz...")
    save_npz_and_json(OUT_DIR, LIP_DIR, JSON_DIR)

    # 汇总commands.npy
    all_json = sorted(JSON_DIR.glob("records_final_*.json"))
    all_records = []
    for jf in all_json:
        with open(jf, encoding='utf-8') as f:
            all_records += json.load(f)
    if all_records:
        cmds = np.array([[float(x) for x in r['command']] for r in all_records],
                        dtype=np.float32)
        np.save(OUT_DIR / "commands.npy", cmds)
        print(f"commands.npy 更新: {cmds.shape}")

    total_existing = get_existing_count(LIP_DIR)
    print(f"\n当前总进度: {total_existing}/{TOTAL} 张 ({total_existing*100//TOTAL}%)")
    if total_existing >= TOTAL:
        print("🎉 全部38880张采集完成！")
    else:
        remaining_groups = (TOTAL - total_existing + GROUP_SIZE - 1) // GROUP_SIZE
        print(f"还剩约 {TOTAL-total_existing} 张，"
              f"约 {(TOTAL-total_existing)*2/3600:.1f} 小时")

    ser.close()
    close_camera(cam)


if __name__ == "__main__":
    main()