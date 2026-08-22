import asyncio
import json
import os
import sys

from core.bot import LunarBot

if not os.path.exists("config.json"):
    print("配置文件 config.json 不存在，请创建")
    sys.exit(1)
with open("config.json", encoding="utf-8") as f:
    config = json.load(f)

if not os.path.exists("admin114.json"):
    print("管理员文件 admin114.json 不存在，请创建")
    sys.exit(1)
with open("admin114.json", encoding="utf-8") as f:
    admin_config = json.load(f)

config.update(admin_config)


async def main114():
    main_event_loop = asyncio.get_running_loop()
    bot = LunarBot(config, main_event_loop)
    await bot.run()


if __name__ == "__main__":
    try:
        asyncio.run(main114())
    except KeyboardInterrupt:
        print("Bot stopped by user.")
    except Exception as e:
        print(f"An error occurred: {e}")
