#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地语音识别采集脚本（NL Blender Designer · C3）。

独立于 Blender 运行（由插件以子进程方式调用），使用 Vosk 离线中文模型把麦克风
语音转成文字，最终结果写入 --out 指定的 JSON 文件，供插件轮询读取。

依赖（在独立 venv 中安装）：vosk, sounddevice, numpy
用法：
  python voice_capture.py --model <模型目录> --out <结果json> [--timeout 15] [--device 0]

输出 JSON：{"status": "ok"|"error", "text": <识别文字>, "error": <错误信息>}
"""
import sys
import os
import json
import time
import argparse


def write_result(out_path, status, text="", error=""):
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"status": status, "text": text, "error": error},
                      f, ensure_ascii=False)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="Vosk 中文模型目录")
    ap.add_argument("--out", required=True, help="结果输出 JSON 文件路径")
    ap.add_argument("--timeout", type=float, default=15.0, help="最长聆听秒数")
    ap.add_argument("--device", type=int, default=None, help="麦克风设备 id（默认默认设备）")
    args = ap.parse_args()

    # 依赖检查（友好错误，便于插件侧定位）
    try:
        from vosk import Model, KaldiRecognizer
        import sounddevice as sd
        import numpy as np
    except Exception as e:  # noqa: BLE001
        write_result(args.out, "error", error="缺少依赖 vosk/sounddevice/numpy：%s" % e)
        return 1

    if not os.path.isdir(args.model):
        write_result(args.out, "error", error="模型目录不存在：%s" % args.model)
        return 1

    try:
        model = Model(args.model)
    except Exception as e:  # noqa: BLE001
        write_result(args.out, "error", error="模型加载失败：%s" % e)
        return 1

    rec = KaldiRecognizer(model, 16000)
    rec.SetWords(False)

    collected = []            # 已确认（final）的句子片段
    last_final_time = [time.time()]
    start = time.time()

    def callback(indata, frames, time_info, status):
        # sounddevice 的 callback 在它自己的线程里跑：只做识别，绝不碰 bpy（安全）。
        if status:
            pass
        data = indata.tobytes() if hasattr(indata, "tobytes") else bytes(indata)
        if rec.AcceptWaveform(data):
            try:
                res = json.loads(rec.Result())
            except Exception:
                res = {}
            txt = (res.get("text") or "").strip()
            if txt:
                collected.append(txt)
                last_final_time[0] = time.time()

    try:
        stream = sd.RawInputStream(
            samplerate=16000, blocksize=8000, dtype='int16',
            channels=1, device=args.device, callback=callback)
    except Exception as e:  # noqa: BLE001
        write_result(args.out, "error", error="无法打开麦克风：%s" % e)
        return 1

    with stream:
        # 主线程循环：超时 或 「已听到内容且静音超过 1.2s（自然停顿）」即结束
        while True:
            time.sleep(0.1)
            now = time.time()
            if now - start > args.timeout:
                break
            if collected and (now - last_final_time[0] > 1.2):
                break

    # 收尾：把最后一次 partial 也并入
    try:
        final_res = json.loads(rec.FinalResult())
        tail = (final_res.get("text") or "").strip()
        if tail:
            collected.append(tail)
    except Exception:
        pass

    text = " ".join(collected).strip()
    write_result(args.out, "ok", text=text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
