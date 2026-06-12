"""
预下载 FinBERT 模型到本地目录，供 PyInstaller 打包使用。

运行方式：
    python scripts/prepare_model.py

输出：
    dist_data/finbert_model/  —— 约 300MB，包含 config.json + pytorch_model.bin + tokenizer 文件
"""

import os
import shutil
import sys
from pathlib import Path

MODEL_ID = "mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "dist_data" / "finbert_model"


def main():
    print(f"=== 准备 FinBERT 模型 ===")
    print(f"模型: {MODEL_ID}")
    print(f"输出: {OUTPUT_DIR}")

    if OUTPUT_DIR.exists() and (OUTPUT_DIR / "config.json").exists():
        print(f"模型已存在，跳过下载。")
        print(f"如需重新下载，请删除 {OUTPUT_DIR} 后重试。")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("正在加载模型...")
    try:
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        from huggingface_hub import try_to_load_from_cache

        # 阶段 1：检查 HF 本地缓存
        cached = try_to_load_from_cache(MODEL_ID, "config.json")
        if cached:
            print(f"本地缓存命中: {cached}")
            model = AutoModelForSequenceClassification.from_pretrained(
                MODEL_ID, local_files_only=True)
            tokenizer = AutoTokenizer.from_pretrained(
                MODEL_ID, local_files_only=True)
        else:
            # 阶段 2：联网下载（带进度条）
            print("本地缓存未命中，从 HuggingFace 下载（约 300MB）...")
            model = AutoModelForSequenceClassification.from_pretrained(MODEL_ID)
            tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)

        model.save_pretrained(str(OUTPUT_DIR))
        tokenizer.save_pretrained(str(OUTPUT_DIR))

        print(f"✓ 模型已导出到 {OUTPUT_DIR}")
        _verify(OUTPUT_DIR)
    except Exception as e:
        print(f"✗ 失败: {e}")
        print("请检查网络连接，或手动复制模型到 dist_data/finbert_model/")
        sys.exit(1)


def _verify(path: Path):
    """验证模型文件完整性。"""
    required = ["config.json", "tokenizer_config.json"]
    missing = [f for f in required if not (path / f).exists()]
    # model.safetensors 或 pytorch_model.bin 至少一个
    has_weights = (path / "model.safetensors").exists() or (path / "pytorch_model.bin").exists()
    has_tokenizer = (path / "tokenizer.json").exists() or (path / "vocab.json").exists()
    if not has_weights:
        missing.append("model.safetensors / pytorch_model.bin")
    if not has_tokenizer:
        missing.append("tokenizer.json / vocab.json")
    if missing:
        print(f"⚠️ 缺失文件: {missing}")
    else:
        total_size = sum(f.stat().st_size for f in path.iterdir() if f.is_file())
        print(f"✓ 模型验证通过 ({len(required)} 个核心文件, 总大小 {total_size / 1024 / 1024:.0f} MB)")


if __name__ == "__main__":
    main()
