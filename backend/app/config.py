from __future__ import annotations
import os
from pathlib import Path

PROJECT_NAME = "抄表簿"
ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "meterseal.db"

# SQLite 落盘强度。默认 FULL（每次提交 fsync，崩溃安全）；
# 仅测试/CI 等临时库场景允许用 SQLITE_SYNCHRONOUS=OFF 关闭 fsync 提速，
# 生产环境不设置此变量即保持安全默认。
SQLITE_SYNCHRONOUS = os.environ.get("SQLITE_SYNCHRONOUS", "FULL").upper()
