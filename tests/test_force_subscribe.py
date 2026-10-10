"""Run: python tests/test_force_subscribe.py   (no network, fake Bot)."""
import asyncio, os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import GetChatMember
import force_subscribe as F
from config import Config

ADMIN, USER = 1, 2
fs = F.ForceSubConfig(True, "@TheRoyalZone", "https://t.me/TheRoyalZone", "The Royal Zone")
cfg = types.SimpleNamespace(admin_ids=frozenset({ADMIN}))

class Member:
    def __init__(self, status, is_member=None): self.status, self.is_member = status, is_member

class FakeBot:
    def __init__(self): self.result = Member("left")
    async def get_chat_member(self, chat, uid):
        if isinstance(self.result, Exception): raise self.result
        return self.result

class FakeUsers:
    async def upsert(self, u): pass

from aiogram.types import Message, CallbackQuery, User, Chat

class FMsg(Message):
    sent: list = []
    async def answer(self, text, **kw): self.sent.append(text)
    async def edit_text(self, text, **kw): self.sent.append(text)

class FCb(CallbackQuery):
    alerts: list = []
    async def answer(self, text=None, show_alert=None, **kw): self.alerts.append(text)

def mk_message(uid, text="hello"):
    m = FMsg.model_construct(from_user=User.model_construct(id=uid, is_bot=False),
                             chat=Chat.model_construct(id=uid, type="private"), text=text)
    object.__setattr__(m, "sent", [])
    return m

def mk_cb(uid, data):
    host = mk_message(uid)
    cb = FCb.model_construct(from_user=User.model_construct(id=uid, is_bot=False), data=data, message=host)
    object.__setattr__(cb, "alerts", [])
    object.__setattr__(cb, "edits", host.sent)
    return cb

async def run(gate, event, bot):
    called = []
    async def handler(e, d): called.append(1); return "handled"
    await gate(handler, event, {"bot": bot})
    return bool(called)

async def main():
    bot = FakeBot()
    import ui
    ui.main_menu_text = lambda: "MENU"; ui.main_menu_kb = lambda s, a: None
    from services.force_sub_service import Channel
    class Svc:
        def enabled(self): return [Channel(1, -1001, "The Royal Zone", "TheRoyalZone", fs.url, True)]
    gate = F.ForceSubscribeGate(cfg, object(), FakeUsers(), fs, Svc())
    ok = lambda name, cond: print(("PASS " if cond else "FAIL ") + name) or (cond or sys.exit(1))

    m = mk_message(USER); bot.result = Member("left")
    ok("1 non-member blocked", not await run(gate, m, bot) and "ACCESS RESTRICTED" in m.sent[0])

    bot.result = Member("kicked"); m = mk_message(USER)
    ok("1b kicked blocked", not await run(gate, m, bot))
    bot.result = Member("restricted", False); m = mk_message(USER)
    ok("1c restricted is_member=False blocked", not await run(gate, m, bot))

    bot.result = Member("member"); cb = mk_cb(USER, F.VERIFY_DATA)
    await run(gate, cb, bot)
    ok("2 member verifies -> menu shown", cb.edits == ["MENU"])
    for st, im in [("member", None), ("administrator", None), ("creator", None), ("restricted", True)]:
        bot.result = Member(st, im); m = mk_message(USER)
        ok(f"2b status {st} allowed", await run(gate, m, bot))

    bot.result = Member("left"); m = mk_message(USER)
    ok("3 user who left is blocked next message", not await run(gate, m, bot) and "NO LONGER ACTIVE" in m.sent[0])
    cb = mk_cb(USER, "menu")
    ok("3b blocked on button too", not await run(gate, cb, bot))

    await asyncio.sleep(F._VERIFY_COOLDOWN + 0.1)
    cb = mk_cb(USER, F.VERIFY_DATA); await run(gate, cb, bot)
    ok("3c verify while not joined fails", "NOT VERIFIED" in cb.edits[0])
    await asyncio.sleep(F._VERIFY_COOLDOWN + 0.1)
    bot.result = Member("member"); cb = mk_cb(USER, F.VERIFY_DATA); await run(gate, cb, bot)
    m = mk_message(USER)
    ok("4 rejoin + verify regains access", cb.edits == ["MENU"] and await run(gate, m, bot))

    bot.result = Member("left")
    for bad in ["fsub:verify:2", "fsub:verify:" + str(ADMIN), "fsub:ok", "fsub:verified", "menu", "type:new"]:
        cb = mk_cb(USER, bad)
        ok(f"5 forged callback '{bad}' cannot bypass", not await run(gate, cb, bot))

    for exc in [TelegramBadRequest(GetChatMember(chat_id=1, user_id=1), "chat not found"),
                TelegramNetworkError(GetChatMember(chat_id=1, user_id=1), "net"),
                TelegramRetryAfter(GetChatMember(chat_id=1, user_id=1), "flood", 99),
                RuntimeError("boom"), Member("weird-status")]:
        bot.result = exc; m = mk_message(USER)
        ok(f"6 error {type(exc).__name__} denies access", not await run(gate, m, bot) and "UNAVAILABLE" in m.sent[0])

    bot.result = Member("left"); m = mk_message(ADMIN)
    ok("7 admin bypasses", await run(gate, m, bot))
    gate.fs = F.ForceSubConfig(False, "@x", "u", "n")
    ok("7b disabled flag passes everyone", await run(gate, mk_message(USER), bot))
    print("ALL TESTS PASSED")

asyncio.run(main())
