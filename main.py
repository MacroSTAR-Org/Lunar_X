import asyncio
import json
import os
import sys
import traceback

from core.bot import LunarBot
from core.security import atomic_write_json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
ADMIN_PATH = os.path.join(BASE_DIR, "admin114.json")


def _load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        print(
            "配置文件 config.json 不存在。\n"
            "请先运行一次 python webui.py 生成默认配置，或复制 examples/config.example.jsonc。"
        )
        sys.exit(1)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"config.json 不是合法 JSON: {e}")
        sys.exit(1)
    if not isinstance(data, dict):
        print("config.json 顶层必须是 JSON 对象")
        sys.exit(1)
    return data


def _load_admin_config() -> dict:
    if not os.path.exists(ADMIN_PATH):
        # 首次启动自动生成空权限表，避免用户卡在“文件不存在”上；
        # 默认不授予任何人权限，需要在 WebUI「用户」页显式添加自己。
        print("[main] admin114.json 不存在，已生成空权限表（super_users/manager_users 为空）")
        atomic_write_json(ADMIN_PATH, {"super_users": [], "manager_users": []}, mode=0o600)
        return {"super_users": [], "manager_users": []}

    try:
        with open(ADMIN_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"admin114.json 不是合法 JSON: {e}")
        sys.exit(1)
    if not isinstance(data, dict):
        print("admin114.json 顶层必须是 JSON 对象")
        sys.exit(1)
    return data


config = _load_config()
config.update(_load_admin_config())


async def main114():
    main_event_loop = asyncio.get_running_loop()
    bot = LunarBot(config, main_event_loop)
    if not await bot.run():
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main114())
    except KeyboardInterrupt:
        print("Bot stopped by user.")
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
