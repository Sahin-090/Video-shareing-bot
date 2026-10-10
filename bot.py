import asyncio
import logging

from aiogram import Bot, Dispatcher

from config import load_config
from database import Database
from handlers import admin, admin_panel, start, user
from logging_setup import log_event, setup_logging
from middlewares import AccessGate, on_error
from services.bridge_service import BridgeService
from services.cleanup_service import CleanupService
from services.delivery_service import DeliveryService
from services.event_service import EventService
from services.scanner_service import ScannerService
from services.settings_service import SettingsService
from services.user_service import UserService
from services.video_service import VideoService

log = logging.getLogger("bot")


async def main() -> None:
    setup_logging()
    cfg = load_config()

    db = Database(cfg.database_url, cfg.db_pool_min, cfg.db_pool_max)
    await db.connect()

    # Client 1 - Bot API (BOT_TOKEN): /start, user requests, buttons, delivery, admin commands.
    bot = Bot(cfg.bot_token)
    users = UserService(db, cfg.state_ttl_minutes)
    videos = VideoService(db, cfg.old_video_order, cfg.new_video_days)
    # Admin-editable settings live in PostgreSQL (bot on/off, maintenance, texts, buttons ...),
    # so they survive Railway restarts. They are layered over the environment defaults.
    settings = SettingsService(db, cfg)
    await settings.load()
    settings.apply_runtime(videos)
    events = EventService(db, cfg)
    await users.load_banned()
    # Client 2 - Telethon USER client (API_ID + API_HASH + SESSION_STRING): history scanning
    # ONLY. It is created lazily inside ScannerService; the bot token is never used for it.
    scanner = ScannerService(cfg, db, videos, events)
    bridge = BridgeService(bot, videos, cfg)
    delivery = DeliveryService(bot, db, videos, users, cfg, settings, events)
    cleanup = CleanupService(bot, db, events)
    panel = admin_panel.Panel(bot, cfg, db, users, videos, scanner, bridge, settings, events)
    await users.reset_delivering()
    await scanner.reset_stale()   # a scan that was running when Railway restarted is not running any more

    dp = Dispatcher()
    dp.workflow_data.update(cfg=cfg, db=db, users=users, videos=videos, delivery=delivery,
                            scanner=scanner, bridge=bridge, settings=settings, events=events,
                            panel=panel)
    # Bot OFF / maintenance / admin block. Admins always pass.
    gate = AccessGate(cfg, settings, users)
    dp.message.outer_middleware(gate)
    dp.callback_query.outer_middleware(gate)
    dp.errors.register(on_error)      # users never see Python exceptions
    dp.include_router(admin_panel.router)   # /admin dashboard (admin-only, server-side checked)
    dp.include_router(admin.router)   # admin commands; must come before the generic text handler
    dp.include_router(start.router)
    dp.include_router(user.router)

    await bot.get_me()
    log.info("✅ Bot client started")
    await scanner.validate_session()   # logs only fixed, safe messages; bot keeps running either way
    log_event(log, logging.INFO, "bot_starting", admins=len(cfg.admin_ids),
              scanner=scanner.enabled)
    background = asyncio.create_task(scanner.background_loop())
    # Deletes bot-sent videos from users' chats once delete_at passes (restart-safe, DB-backed).
    cleanup_task = asyncio.create_task(cleanup.run())
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        background.cancel()
        cleanup_task.cancel()
        await scanner.close()
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
