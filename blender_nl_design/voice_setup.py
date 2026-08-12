#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载 Vosk 中文语音模型到 ~/.cache/vosk（C3 语音模块准备，纯标准库）。

用法：
  python voice_setup.py
模型最终位于：~/.cache/vosk/vosk-model-small-cn-0.22
"""
import os
import sys
import zipfile
import urllib.request

MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip"
MODEL_NAME = "vosk-model-small-cn-0.22"
DEST = os.path.join(os.path.expanduser("~"), ".cache", "vosk")


def main():
    os.makedirs(DEST, exist_ok=True)
    target = os.path.join(DEST, MODEL_NAME)
    if os.path.isdir(target) and os.path.isdir(os.path.join(target, "am")):
        print("模型已存在，跳过下载：", target)
        return 0
    zip_path = os.path.join(DEST, MODEL_NAME + ".zip")
    print("下载语音模型：", MODEL_URL)
    try:
        def hook(block_num, block_size, total_size):
            if total_size:
                pct = 100.0 * block_num * block_size / total_size
                print("  进度 %.0f%%" % min(100.0, pct), end="\r")
        urllib.request.urlretrieve(MODEL_URL, zip_path, hook)
    except Exception as e:  # noqa: BLE001
        print("\n下载失败：%s" % e)
        return 1
    print("\n解压中...")
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(DEST)
    try:
        os.remove(zip_path)
    except Exception:
        pass
    print("完成：", target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
