"""容器启动时检查配置、更新数据库、准备数据，再启动 API。"""

import os
import subprocess
import sys
from urllib.parse import urlsplit

from sqlalchemy import URL


def main() -> None:
    origin = os.environ["TARA_PUBLIC_ORIGIN"]
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path:
        raise ValueError("TARA_PUBLIC_ORIGIN 必须是完整网站地址，且不带末尾斜杠")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("TARA_PUBLIC_ORIGIN 不能包含参数、片段或账号密码")
    if os.environ["TARA_ENVIRONMENT"] == "production" and parsed.scheme != "https":
        raise ValueError("生产环境必须使用 HTTPS，否则浏览器不能发送登录 Cookie")
    if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
        raise ValueError("请配置 DEEPSEEK_API_KEY 后启动")

    # 使用标准接口处理密码中的 @、: 等字符，避免手工拼接连接地址。
    os.environ["TARA_DATABASE_URL"] = URL.create(
        "postgresql+asyncpg",
        username=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        host="postgres",
        port=5432,
        database=os.environ["POSTGRES_DB"],
    ).render_as_string(hide_password=False)

    print("正在更新数据库结构……", flush=True)
    subprocess.run(["alembic", "upgrade", "head"], check=True)
    print("正在检查科学数据；数据与处理版本未变化时自动跳过……", flush=True)
    subprocess.run(["tara-data"], check=True)
    # 直接替换启动进程，让 Docker 停止时 API 能收到退出信号。
    os.execvp(
        "uvicorn",
        [
            "uvicorn",
            "tara_agent.api.app:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
            "--proxy-headers",
            "--forwarded-allow-ips",
            "*",
        ],
    )


if __name__ == "__main__":
    try:
        main()
    except (KeyError, ValueError, subprocess.CalledProcessError) as error:
        print(f"后端启动失败：{error}", file=sys.stderr, flush=True)
        raise SystemExit(1) from error
