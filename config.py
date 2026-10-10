import os
import re
from dataclasses import dataclass


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw and raw.strip() else default


def _order(name: str, default: str) -> str:
    value = (os.getenv(name) or default).strip().lower()
    if value not in ("asc", "desc", "random"):
        raise RuntimeError(f"{name} must be asc, desc or random")
    return value


def normalize_channel_id(raw: str) -> int | None:
    """Accept -1001234567890 (Bot API form) or the bare 1234567890 and return the -100 form."""
    raw = raw.strip()
    if not raw:
        return None
    value = int(raw)
    if value > 0:
        # 1003992837415 is a -1003992837415 whose minus sign got lost; 3992837415 is the bare id
        value = -value if str(value).startswith("100") and len(str(value)) >= 13 else int(f"-100{value}")
    return value


def parse_channel_ids(*raws: str | None) -> tuple[int, ...]:
    """Every value may hold one id or a comma/space/semicolon separated list. Order is kept,
    duplicates (also -100 vs bare form of the same channel) are dropped."""
    out: list[int] = []
    for raw in raws:
        for part in re.split(r"[,;\s]+", (raw or "").strip()):
            if not part:
                continue
            value = normalize_channel_id(part)
            if value is not None and value not in out:
                out.append(value)
    return tuple(out)


@dataclass
class Config:   # mutable on purpose: SettingsService applies the admin's saved overrides at runtime
    bot_token: str
    database_url: str
    admin_ids: frozenset[int]
    source_channel_ids: tuple[int, ...]   # one or more source channels (first = primary)
    channel_auto_category: str | None
    max_videos_per_request: int
    old_video_order: str
    send_delay: float
    protect_content: bool
    state_ttl_minutes: int
    db_pool_min: int
    db_pool_max: int
    # --- MTProto scanner (Telethon). All three are needed to enable it. ---
    api_id: int | None
    api_hash: str | None
    session_string: str | None
    scan_batch_size: int
    scan_progress_every: int
    auto_scan_minutes: int
    new_video_days: int
    auto_bridge: bool = False          # IGNORED: the bridge never runs automatically any more (use /bridge)
    bridge_chat_id: int | None = None  # private chat the bridge uses (default: the admin's chat)
    rate_limit_requests: int = 5       # max video requests per user ...
    rate_limit_window: int = 60        # ... within this many seconds
    auto_delete_seconds: int = 120     # bot-sent videos disappear from the user's chat after this
    # --- Developer profile (defaults; the admin can override them in the panel) ---
    developer_name: str = "Sahin"
    developer_website: str = ""
    developer_instagram: str = ""
    developer_telegram: str = ""
    developer_github: str = ""
    developer_bio: str = ""
    new_video_order: str = "desc"      # desc = newest first | asc | random
    delivery_batch_size: int = 10      # short extra pause after every N videos (flood-friendly)

    @property
    def source_channel_id(self) -> int | None:   # the primary (first) channel
        return self.source_channel_ids[0] if self.source_channel_ids else None

    @property
    def private_channel_id(self) -> int | None:  # backwards-compatible name
        return self.source_channel_id

    @property
    def mtproto_enabled(self) -> bool:
        return bool(self.api_id and self.api_hash and self.session_string
                    and self.source_channel_id is not None)


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip()
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is not set")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")

    admin_ids = frozenset(
        int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
    )
    # SOURCE_CHANNEL_IDS (comma separated) is the multi-channel form. SOURCE_CHANNEL_ID (one id,
    # or also a list) and PRIVATE_CHANNEL_ID keep old deployments working.
    channel_ids = parse_channel_ids(os.getenv("SOURCE_CHANNEL_ID") or os.getenv("PRIVATE_CHANNEL_ID"),
                                    os.getenv("SOURCE_CHANNEL_IDS"))
    raw_auto = os.getenv("CHANNEL_AUTO_CATEGORY", "").strip().lower()
    if raw_auto in ("off", "none", "false"):
        auto_cat = None
    elif raw_auto:
        auto_cat = raw_auto
    else:
        auto_cat = "new" if channel_ids else None
    if auto_cat not in (None, "new", "old"):
        raise RuntimeError("CHANNEL_AUTO_CATEGORY must be 'new', 'old' or empty")
    order = os.getenv("OLD_VIDEO_ORDER", "asc").strip().lower()
    if order not in ("asc", "desc", "random"):
        raise RuntimeError("OLD_VIDEO_ORDER must be asc, desc or random")

    api_id_raw = os.getenv("API_ID", "").strip()

    return Config(
        bot_token=token,
        database_url=dsn,
        admin_ids=admin_ids,
        source_channel_ids=channel_ids,
        channel_auto_category=auto_cat,
        max_videos_per_request=max(1, _int("MAX_VIDEOS_PER_REQUEST", 50)),
        old_video_order=order,
        send_delay=float(os.getenv("SEND_DELAY", "0.3") or 0.3),
        protect_content=_bool(os.getenv("PROTECT_CONTENT")),
        state_ttl_minutes=max(1, _int("STATE_TTL_MINUTES", 30)),
        db_pool_min=max(1, _int("DB_POOL_MIN", 1)),
        db_pool_max=max(1, _int("DB_POOL_MAX", 10)),
        api_id=int(api_id_raw) if api_id_raw else None,
        api_hash=os.getenv("API_HASH", "").strip() or None,
        session_string=os.getenv("SESSION_STRING", "").strip() or None,
        scan_batch_size=max(1, _int("SCAN_BATCH_SIZE", 100)),
        scan_progress_every=max(1, _int("SCAN_PROGRESS_EVERY", 500)),
        auto_scan_minutes=max(0, _int("AUTO_SCAN_MINUTES", 10)),
        new_video_days=max(1, _int("NEW_VIDEO_DAYS", 30)),
        auto_bridge=False,   # AUTO_BRIDGE is ignored: scans must never forward/delete videos
        bridge_chat_id=int(os.getenv("BRIDGE_CHAT_ID", "").strip() or 0) or None,
        rate_limit_requests=max(1, _int("RATE_LIMIT_REQUESTS", 5)),
        rate_limit_window=max(1, _int("RATE_LIMIT_WINDOW", 60)),
        auto_delete_seconds=max(5, _int("AUTO_DELETE_SECONDS", 120)),
        developer_name=os.getenv("DEVELOPER_NAME", "").strip() or "Sahin",
        developer_website=os.getenv("DEVELOPER_WEBSITE", "").strip(),
        developer_instagram=os.getenv("DEVELOPER_INSTAGRAM", "").strip(),
        developer_telegram=os.getenv("DEVELOPER_TELEGRAM", "").strip(),
        developer_github=os.getenv("DEVELOPER_GITHUB", "").strip(),
        developer_bio=os.getenv("DEVELOPER_BIO", "").strip(),
        new_video_order=_order("NEW_VIDEO_ORDER", "desc"),
        delivery_batch_size=min(50, max(1, _int("DELIVERY_BATCH_SIZE", 10))),
    )
