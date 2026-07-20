# -*- coding: utf-8 -*-
"""
真机采集脚本 v2.0 - 第二轮采集（facesetD）
- 相机：海康 MV-CU060-10UC
- 舵机：Hiwonder LSC-32，13个唇部舵机
- 采样：6个独立自由度全组合遍历，共10368张
- 特性：支持断点续采，每100张自动保存JSON
- 存储：只保存480全脸图和256嘴唇图，不再保存原始大图
- 配对方式：直接使用手工配对的归一化点位表（不再用镜像公式计算）

使用方法：
  python collect_facesetD.py          # 自动从断点继续
  python collect_facesetD.py --test   # 只采10张测试
  python collect_facesetD.py --reset  # 归位后退出
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
SERIAL_PORT   = 'COM8'
BAUDRATE      = 9600
OUT_DIR       = Path(r"G:\lipbuild\data\facesetD")

EXPOSURE_TIME = 70000.0
GAIN          = 0.0
IMG_W         = 3072
IMG_H         = 2048

FACE_X1, FACE_Y1 = 600,  0
FACE_X2, FACE_Y2 = 2400, 1800
LIP_X1,  LIP_Y1  = 1273, 1142
LIP_X2,  LIP_Y2  = 1758, 1627
FACE_OUT_SIZE    = 480
LIP_OUT_SIZE     = 256

SERVO_WAIT_TIME  = 1.2   # 舵机到位等待时间（秒）
SERVO_MOVE_TIME  = 500   # 舵机运动时间（ms）

TOTAL = 10368
MAX_CAPTURE_RETRIES = 2
JSON_SAVE_INTERVAL = 100
CHECKPOINT_INTERVAL = 10
CURRENT_RECORDS_FILE = "records_current.json"

# ========== 舵机配置（严格按你给的对应关系）==========
# 每个舵机的点位列表和对应的PWM值列表（一一对应）
# 格式：points: 归一化值列表, pwms: 对应的PWM值列表
SERVO_POINT_CONFIG = {
    15: {'points': [0.33, 0.67, 0.90], 'pwms': [1110, 1171, 1212]},
    16: {'points': [0.33], 'pwms': [1640]},                            # 固定
    17: {'points': [0.0, 0.35, 0.69], 'pwms': [1985, 2048, 2109]},
    18: {'points': [0.33], 'pwms': [1430]},                            # 固定
    19: {'points': [0.0, 0.272], 'pwms': [1001, 1050]},
    20: {'points': [0.0, 0.272], 'pwms': [1891, 1940]},               # 0→1891, 0.272→1940
    21: {'points': [0.3, 0.6, 0.94, 1.2], 'pwms': [1354, 1408, 1469, 1516]},
    22: {'points': [-0.256, 0.004, 0.344, 0.644], 'pwms': [1498, 1545, 1606, 1660]},  # 镜像norm值从小到大，精确对应配对点位
    23: {'points': [-1.0, -0.5, 0.0, 0.23], 'pwms': [1416, 1506, 1596, 1637]},
    24: {'points': [-0.6, -0.3, 0.0, 0.3, 0.6, 0.9], 'pwms': [1761, 1815, 1869, 1923, 1977, 2031]},
    25: {'points': [0.237, 0.467, 0.967, 1.467], 'pwms': [1392, 1433, 1523, 1613]},  # 镜像norm值从小到大
    26: {'points': [0.1, 0.4, 0.7, 1.0, 1.3, 1.6], 'pwms': [1058, 1112, 1166, 1220, 1274, 1328]},  # 镜像norm值从小到大
    27: {'points': [0.0, 0.2, 0.4, 0.6, 0.8, 1.0], 'pwms': [1505, 1541, 1577, 1613, 1649, 1685]},
}

# 默认PWM值（归位用）
SERVO_DEFAULT = {
    15: 1085,
    16: 1640,
    17: 2041,
    18: 1430,
    19: 1025,
    20: 1915,
    21: 1385,
    22: 1629,
    23: 1638,
    24: 1959,
    25: 1391,
    26: 1130,
    27: 1567,
}
ACTIVE_SERVO_IDS = list(SERVO_POINT_CONFIG.keys())  # [15,16,...,27]

# ========== 采集点位 ==========
# 配对舵机：直接使用手工配好的归一化点位对（左舵机值, 右舵机值）
# 每个元素是 (左归一化值, 右归一化值)
PAIRED_POINTS = [
    # 左嘴角上下(24) ↔ 右嘴角上下(26)，6对
    # 24: -0.6→1761配26: 1.6→1328, 24: 0.9→2031配26: 0.1→1058
    (24, 26, [
        (-0.6, 1.6), (-0.3, 1.3), (0.0, 1.0),
        (0.3,  0.7), (0.6,  0.4), (0.9, 0.1)
    ]),
    # 下嘴唇左上下(21) ↔ 下嘴唇右上下(22)，4对
    # 21: 0.3→1354配22: 0.644→1660, 21: 1.2→1516配22: -0.256→1498
    (21, 22, [
        (0.3, 0.644), (0.6, 0.344), (0.94, 0.004), (1.2, -0.256)
    ]),
    # 左嘴角前后(23) ↔ 右嘴角前后(25)，4对
    # 23: -1.0→1416配25: 1.467→1613, 23: 0.23→1637配25: 0.237→1392
    (23, 25, [
        (-1.0, 1.467), (-0.5, 0.967), (0.0, 0.467), (0.23, 0.237)
    ]),
    # 上嘴唇左前后(19) ↔ 上嘴唇右前后(20)，2对
    # 19: 0→1001配20: 0.272→1940, 19: 0.272→1050配20: 0→1891
    (19, 20, [
        (0.0, 0.272), (0.272, 0.0)
    ]),
]

# 独立单舵机（只存点位值，PWM由SERVO_POINT_CONFIG线性插值计算）
SINGLE_POINTS = {
    17: [0.0, 0.35, 0.69],              # 下嘴唇中前后，3点
    27: [0.0, 0.2, 0.4, 0.6, 0.8, 1.0], # 下巴上下，6点
    15: [0.33, 0.67, 0.90],             # 上嘴唇中前后，3点
    18: [0.33],                          # 下嘴唇中上下，固定
    16: [0.33],                          # 上嘴唇中上下，固定
}


def norm_to_pwm(sid, val):
    """
    归一化值转PWM：在SERVO_POINT_CONFIG中线性插值
    支持PWM随val增大而减小的镜像舵机（如舵机20/22/25/26）
    """
    config = SERVO_POINT_CONFIG[sid]
    points = config['points']
    pwms = config['pwms']

    # 只有1个点位，直接返回
    if len(points) == 1:
        return pwms[0]

    # val小于最小点位，返回对应端点PWM
    if val <= points[0]:
        return pwms[0]
    # val大于最大点位，返回对应端点PWM
    if val >= points[-1]:
        return pwms[-1]

    # 找到val所在区间，线性插值（支持PWM正向或反向）
    for i in range(len(points) - 1):
        if points[i] <= val <= points[i+1]:
            ratio = (val - points[i]) / (points[i+1] - points[i])
            return int(round(pwms[i] + ratio * (pwms[i+1] - pwms[i])))

    return pwms[0] if val < points[0] else pwms[-1]


def generate_all_combinations():
    """生成所有 10368 种组合（使用手工配好的点位对）"""
    dims = []
    
    # 配对舵机：直接使用手工配好的点位对
    for s1, s2, point_pairs in PAIRED_POINTS:
        dims.append(point_pairs)
    
    # 独立舵机：直接使用点位列表
    for points in SINGLE_POINTS.values():
        dims.append(points)
    
    combos = []
    for combo in itertools.product(*dims):
        cmd = {}
        # 配对舵机
        for i, (s1, s2, _) in enumerate(PAIRED_POINTS):
            cmd[s1] = float(combo[i][0])
            cmd[s2] = float(combo[i][1])
        # 独立舵机
        offset = len(PAIRED_POINTS)
        for j, sid in enumerate(SINGLE_POINTS.keys()):
            cmd[sid] = float(combo[offset + j])
        combos.append(cmd)
    
    return combos


def build_servo_command(positions, move_time_ms):
    """构建LSC-32多舵机同步指令"""
    n = len(positions)
    cmd = bytearray([0x55, 0x55, n*3+5, 0x03, n,
                     move_time_ms & 0xFF, (move_time_ms >> 8) & 0xFF])
    for sid, pos in positions.items():
        pos = max(0, min(4000, int(pos)))
        cmd += bytes([sid, pos & 0xFF, (pos >> 8) & 0xFF])
    return bytes(cmd)


def send_servo_command(ser, cmd_dict):
    """发送舵机指令并等待到位"""
    positions = {sid: norm_to_pwm(sid, val) for sid, val in cmd_dict.items()}
    ser.write(build_servo_command(positions, SERVO_MOVE_TIME))
    ser.flush()
    time.sleep(SERVO_WAIT_TIME)
    print(f"  → PWM: {positions}", end='')


def reset_servos(ser):
    """所有舵机归位"""
    positions = {sid: SERVO_DEFAULT[sid] for sid in ACTIVE_SERVO_IDS}
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


def get_existing_count(face_dir, lip_dir):
    """统计已采集张数（断点续采用）"""
    face_files = sorted(face_dir.glob("*_face.jpg"))
    lip_files = sorted(lip_dir.glob("*_lip.jpg"))
    return min(len(face_files), len(lip_files))


def parse_indices(directory, suffix):
    indices = set()
    for p in directory.glob(f"*_{suffix}.jpg"):
        stem = p.stem
        parts = stem.split("_")
        if len(parts) >= 2 and parts[-1] == suffix:
            try:
                indices.add(int(parts[0]))
            except ValueError:
                continue
    return indices


def get_resume_index(face_dir, lip_dir):
    """返回下一个需要采集的组合起始索引（0-based）。

    如果存在缺失编号，则从第一个缺失位置继续；否则从已完整采集张数继续。
    """
    face_indices = parse_indices(face_dir, "face")
    lip_indices = parse_indices(lip_dir, "lip")
    for idx in range(1, TOTAL + 1):
        if idx not in face_indices or idx not in lip_indices:
            return idx - 1
    return TOTAL


def load_json_records(json_dir, face_indices=None, lip_indices=None):
    """加载已有记录，并按编号去重。"""
    records_by_idx = {}
    for jf in sorted(json_dir.glob("records*.json")):
        try:
            with open(jf, encoding='utf-8') as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, list):
            continue
        for r in data:
            idx = r.get("idx")
            if not isinstance(idx, int) or idx <= 0:
                continue
            if face_indices is not None and idx not in face_indices:
                continue
            if lip_indices is not None and idx not in lip_indices:
                continue
            records_by_idx[idx] = r
    return records_by_idx


def save_json_records(path, records, indent=None):
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(records, f, ensure_ascii=False, indent=indent)
        return True
    except OSError:
        return False


def main():
    parser = argparse.ArgumentParser(description='facesetD 第二轮采集')
    parser.add_argument('--test',  action='store_true', help='只采10张测试')
    parser.add_argument('--reset', action='store_true', help='仅归位舵机后退出')
    args = parser.parse_args()

    try:
        ser = serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1)
        print(f"串口 {SERIAL_PORT} 打开成功")
    except Exception as e:
        print(f"[错误] 无法打开串口: {e}")
        return

    if args.reset:
        print("归位模式...")
        reset_servos(ser)
        ser.close()
        return

    # 创建输出目录（不再创建uncropped目录）
    FACE_DIR   = OUT_DIR / "face_480"
    LIP_DIR    = OUT_DIR / "lip_256"
    JSON_DIR   = OUT_DIR / "json_records"
    for d in [FACE_DIR, LIP_DIR, JSON_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    all_combos = generate_all_combinations()
    print(f"总组合数: {len(all_combos)} 张（应为10368）")

    if args.test:
        combos_to_run = all_combos[:10]
        start_idx = 0
        face_indices = None
        lip_indices = None
        print("=== 测试模式：采集前10张 ===")
    else:
        face_indices = parse_indices(FACE_DIR, "face")
        lip_indices = parse_indices(LIP_DIR, "lip")
        resume_idx = get_resume_index(FACE_DIR, LIP_DIR)
        start_idx = resume_idx
        combos_to_run = all_combos[resume_idx:]
        print(f"=== 断点续采：从第{resume_idx+1}张开始（按文件名检测首个缺失位置） ===")

    records_by_idx = load_json_records(JSON_DIR, face_indices if not args.test else None,
                                       lip_indices if not args.test else None)
    records = [records_by_idx[idx] for idx in sorted(records_by_idx)]
    if records:
        save_json_records(JSON_DIR / CURRENT_RECORDS_FILE, records, indent=2)

    if not combos_to_run:
        print("所有组合已采集完毕！")
        ser.close()
        return

    eta_min = len(combos_to_run) * (SERVO_WAIT_TIME + 0.5) / 60
    print(f"本次采集: {len(combos_to_run)} 张，预计约 {eta_min:.0f} 分钟 ({eta_min/60:.1f} 小时)")

    cam = init_camera()
    if cam is None:
        ser.close()
        return

    print("舵机归位中...")
    reset_servos(ser)

    records = []
    success_count = 0
    fail_count = 0

    print("开始采集...")
    t0 = time.time()

    for i, cmd_dict in enumerate(combos_to_run):
        global_idx = start_idx + i
        file_idx   = global_idx + 1

        print(f"\n[{i+1}/{len(combos_to_run)}] 发送舵机命令...", end='')
        send_servo_command(ser, cmd_dict)

        frame = None
        for attempt in range(1, MAX_CAPTURE_RETRIES + 1):
            frame = capture_frame(cam)
            if frame is not None:
                break
            print(f"\n[警告] 第{file_idx}张抓帧失败，重试 {attempt}/{MAX_CAPTURE_RETRIES}...", end='')
            if attempt < MAX_CAPTURE_RETRIES:
                time.sleep(0.5)

        if frame is None:
            print(f"\n[错误] 第{file_idx}张连续抓帧失败，跳过")
            fail_count += 1
            save_json_records(JSON_DIR / CURRENT_RECORDS_FILE, records, indent=2)
            continue

        print(f" 抓帧成功", end=' ')

        face_img = crop_face(frame)
        lip_img  = crop_lip(frame)

        face_name = f"{file_idx:05d}_face.jpg"
        lip_name  = f"{file_idx:05d}_lip.jpg"

        ok_face = save_image(FACE_DIR / face_name, face_img, ".jpg")
        ok_lip = save_image(LIP_DIR / lip_name, lip_img, ".jpg")
        if not ok_face or not ok_lip:
            print(f"\n[错误] 第{file_idx}张图像保存失败，跳过")
            fail_count += 1
            save_json_records(JSON_DIR / CURRENT_RECORDS_FILE, records, indent=2)
            continue

        cmd_vec = np.array([cmd_dict.get(sid, 0.0) for sid in ACTIVE_SERVO_IDS],
                           dtype=np.float32)
        record = {
            "idx":       file_idx,
            "face":      face_name,
            "lip":       lip_name,
            "timestamp": datetime.now().isoformat(),
            "command":   [float(x) for x in cmd_vec.tolist()]
        }
        records_by_idx[file_idx] = record
        records = [records_by_idx[idx] for idx in sorted(records_by_idx)]
        success_count += 1
        print("保存完成")

        if success_count % JSON_SAVE_INTERVAL == 0 or success_count % CHECKPOINT_INTERVAL == 0:
            save_json_records(JSON_DIR / CURRENT_RECORDS_FILE, records, indent=2)
            if success_count % JSON_SAVE_INTERVAL == 0:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                tmp_json = JSON_DIR / f"records_{file_idx:05d}_{ts}.json"
                save_json_records(tmp_json, records)
                print(f"  [自动保存] {tmp_json.name}")

        elapsed   = time.time() - t0
        per_img   = elapsed / (i + 1)
        remaining = per_img * (len(combos_to_run) - i - 1)
        pct = (i + 1) * 100 // len(combos_to_run)
        print(f"  进度: {file_idx}/{start_idx+len(combos_to_run)} [{pct}%]  "
              f"已用:{int(elapsed//3600)}h{int(elapsed%3600//60)}m  "
              f"剩余:{int(remaining//3600)}h{int(remaining%3600//60)}m",
              end='\r')

    print(f"\n\n采集完成：成功{success_count}张，失败{fail_count}张")

    print("舵机归位中...")
    reset_servos(ser)

    if records:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_path = JSON_DIR / f"records_final_{ts}.json"
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        print(f"JSON已保存: {json_path.name}")

    print("打包 images.npz...")
    lip_files = sorted(LIP_DIR.glob("*_lip.jpg"))
    if lip_files:
        lips = []
        for f in lip_files:
            img = cv2.imread(str(f))
            if img is not None:
                lips.append(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        if lips:
            lips_arr = np.stack(lips, axis=0)
            np.savez_compressed(OUT_DIR / "images.npz", images=lips_arr)
            print(f"  images.npz: {lips_arr.shape}")

    print("汇总 commands.npy...")
    all_json = sorted(JSON_DIR.glob("records_final_*.json"))
    all_records = []
    for jf in all_json:
        with open(jf, encoding='utf-8') as f:
            all_records += json.load(f)
    if all_records:
        cmds = np.array([[float(x) for x in r['command']] for r in all_records],
                        dtype=np.float32)
        np.save(OUT_DIR / "commands.npy", cmds)
        print(f"  commands.npy: {cmds.shape}")

    total_existing = get_existing_count(FACE_DIR, LIP_DIR)
    print(f"\n当前总进度: {total_existing}/{TOTAL} 张 ({total_existing*100//TOTAL}%)")
    if total_existing >= TOTAL:
        print("🎉 全部10368张采集完成！")
    else:
        print(f"还剩 {TOTAL - total_existing} 张")

    ser.close()
    close_camera(cam)


if __name__ == "__main__":
    main()