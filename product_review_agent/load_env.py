"""环境变量加载工具 - 从 .env 文件读取环境变量"""
import os
from pathlib import Path


def load_env(env_file: str = ".env") -> None:
    """加载 .env 文件中的环境变量到 os.environ（不覆盖已存在的值）"""
    env_path = Path(env_file)
    if not env_path.is_file():
        return

    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            # 跳过空行和注释
            if not line or line.startswith("#"):
                continue
            # 解析 KEY=VALUE
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # 移除值两端的引号（支持 "value" 和 'value'）
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                # 不覆盖已存在的环境变量（命令行设置的优先级更高）
                if key not in os.environ:
                    os.environ[key] = value
