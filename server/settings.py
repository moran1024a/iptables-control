"""Runtime paths, component versions and protocol limits."""

import json
import os

VERSION = "0.6.3"
DATABASE_VERSION = 603
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(BASE_DIR, "components.json")) as _f:
    COMPONENTS = json.load(_f)
REPO = "moran1024a/iptables-control"
DB_PATH = os.environ.get("ICTL_DB", "/var/lib/ictl-server/ictl.db")
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

OFFLINE_AFTER = 30  # 主动节点超过 30 秒没有上报视为离线
MAX_SKEW = 60  # 节点时间允许的最大偏差（秒）
MAX_BODY = 8192  # 同步请求体上限
MAX_API_BODY = 65536  # 面板请求体上限
WINDOW = 60  # 被动同步活动：每格 60 秒
ACTIVE_WINDOW = 20  # 主动状态条：每格 20 秒
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
HEALTH = {"ok": "正常", "error": "故障", "skew": "时间不一致"}
KINDS = ("nodes", "targets", "topologies", "external_configurations")
DEFAULT_GROUP = "默认分组"
