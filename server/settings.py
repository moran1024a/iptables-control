"""Runtime paths, component versions and protocol limits."""

import json
import os

VERSION = "0.5.3"
DATABASE_VERSION = 504
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(BASE_DIR, "components.json")) as _f:
    COMPONENTS = json.load(_f)
FORWARD_VERSION = COMPONENTS["forward_agent"]
REPO = "moran1024a/iptables-control"
AGENT_URL = f"https://github.com/{REPO}/releases/download/v{VERSION}/ictl.sh"
DB_PATH = os.environ.get("ICTL_DB", "/var/lib/ictl-server/ictl.db")
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
# 同版本的节点端脚本，供节点自动更新时下载：Docker 镜像中为 /app/agent/ictl.sh，源码目录中为 ../agent/ictl.sh
AGENT_FILES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), p) for p in ("agent/ictl.sh", "../agent/ictl.sh")
]
AGENT_AUTO_UPDATE = (0, 1, 7)  # 从该版本起，节点端按服务端的更新信号自动更新

OFFLINE_AFTER = 120  # 超过该秒数没有上报视为离线（主动节点每轮等待 5-10 秒）
MAX_SKEW = 60  # 节点时间允许的最大偏差（秒）
MAX_BODY = 8192  # 同步请求体上限
MAX_API_BODY = 65536  # 面板请求体上限
WINDOW = 60  # 被动活动与旧版状态记录：每格 60 秒
ACTIVE_WINDOW = 20  # 主动状态条：每格 20 秒，覆盖多次 5–10 秒轮询
BAR_WINDOWS = 60  # 状态条显示的窗口数
RETENTION = 7 * 86400  # 状态窗口与事件保留时长
EVENTS_SHOWN = 50
SESSION_TTL = 7 * 86400
SESSION_COOKIE = "ictl_session"
LOGIN_MAX_FAILS = 5
LOGIN_LOCK = 300
DEFAULT_USER = "admin"
DEFAULT_PASS = "admin_pass"
NOT_FOUND = "404 page not found\n"
STATIC = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "topology.js": "text/javascript; charset=utf-8",
    "topology_paths.js": "text/javascript; charset=utf-8",
    "pools.js": "text/javascript; charset=utf-8",
    "subscriptions.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
}
HEALTH = {"ok": "正常", "error": "故障", "skew": "时间不一致"}
KINDS = ("nodes", "routes", "targets", "chains", "links")
DEFAULT_GROUP = "默认分组"
