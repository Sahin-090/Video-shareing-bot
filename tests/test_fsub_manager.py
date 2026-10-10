"""End-to-end test of the Force Subscribe Manager against a REAL PostgreSQL.
   DATABASE_URL=postgresql://t:t@localhost/tdb python tests/test_fsub_manager.py"""
import asyncio, os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aiogram.types import CallbackQuery, Chat, Message, User
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetChat
from database import Database
from handlers.admin_panel import Panel
from services.force_sub_service import ForceSubService, DuplicateChannel
from services.user_service import UserService
import force_subscribe as F

ADMIN, USER = 1, 2
def ok(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: sys.exit(1)

class Mem:
    def __init__(s, status, is_member=None): s.status, s.is_member = status, is_member

class Chat_:
    def __init__(s, id, title, username=None, type="channel", invite_link=None):
        s.id, s.title, s.username, s.type, s.invite_link = id, title, username, type, invite_link

class FakeBot:
    def __init__(s):
        s.chats = {"@RoyalZone": Chat_(-1001111111111, "The Royal Zone", "RoyalZone"),
                   "@SecondOne": Chat_(-1002222222222, "Second One", "SecondOne"),
                   -1003333333333: Chat_(-1003333333333, "Private Club")}
        s.members = {}      # (chat_id, uid) -> status ; bot id 99
        s.bot_admin = {-1001111111111, -1002222222222, -1003333333333}
        s.fail = False
    async def get_me(s): return types.SimpleNamespace(id=99)
    async def get_chat(s, ref):
        if ref in s.chats: return s.chats[ref]
        raise TelegramBadRequest(GetChat(chat_id=1), "chat not found")
    async def get_chat_member(s, chat, uid):
        if s.fail: raise RuntimeError("x")
        if uid == 99: return Mem("administrator" if chat in s.bot_admin else "left")
        return Mem(s.members.get((chat, uid), "left"))
    async def export_chat_invite_link(s, chat): raise TelegramBadRequest(GetChat(chat_id=1), "no rights")

class FMsg(Message):
    async def answer(self, text, **kw): self.out.append((text, kw.get("reply_markup")))
    async def edit_text(self, text, **kw): self.out.append((text, kw.get("reply_markup")))
class FCb(CallbackQuery):
    async def answer(self, text=None, show_alert=None, **kw): self.alerts.append(text)

def mk_msg(uid, text="x", out=None):
    m = FMsg.model_construct(from_user=User.model_construct(id=uid, is_bot=False),
                             chat=Chat.model_construct(id=uid, type="private"), text=text)
    object.__setattr__(m, "out", out if out is not None else []); return m
def mk_cb(uid, data):
    host = mk_msg(uid)
    cb = FCb.model_construct(from_user=User.model_construct(id=uid, is_bot=False), data=data, message=host)
    object.__setattr__(cb, "alerts", []); object.__setattr__(cb, "out", host.out); return cb
def kbdata(markup): return [b.callback_data for r in markup.inline_keyboard for b in r]
def kburls(markup): return [b.url for r in markup.inline_keyboard for b in r if b.url]

async def main():
    db = Database(os.environ["DATABASE_URL"], 1, 3); await db.connect(); await db.connect()  # schema idempotent
    await db.pool.execute("DELETE FROM force_sub_channels; DELETE FROM bot_settings WHERE key='fsub_seeded'")
    import ui; ui.main_menu_text = lambda: "MENU"; ui.main_menu_kb = lambda s, a: None
    bot = FakeBot(); svc = ForceSubService(db); await svc.load()
    users = UserService(db, 30)
    await svc.seed_default(bot, "@RoyalZone", "https://t.me/RoyalZone", "The Royal Zone")
    ok("seed adds default channel once", [c.title for c in svc.all()] == ["The Royal Zone"])
    await svc.seed_default(bot, "@RoyalZone", "u", "n"); ok("seed not repeated", len(svc.all()) == 1)

    panel = object.__new__(Panel)
    panel.bot, panel.cfg, panel.db, panel.users = bot, types.SimpleNamespace(admin_ids=frozenset({ADMIN})), db, users
    panel.force_sub, panel._fsub_health, panel._scan_msg, panel._tasks = svc, {}, None, set()
    class Ev:
        async def record(*a, **k): pass
    panel.events = Ev()

    # ---- home page + buttons
    text, kb = await panel.page("adm:fsub")
    ok("home has the 6 requested buttons", kbdata(kb) == ["adm:fsub:add","adm:fsub:list","adm:fsub:pick:edit","adm:fsub:pick:rm","adm:fsub:ref","adm:home"]
       and "FORCE SUBSCRIBE MANAGER" in text)
    # ---- add (public channel) via input flow
    await db.pool.execute("INSERT INTO users(user_id) VALUES($1) ON CONFLICT DO NOTHING", ADMIN)
    cb = mk_cb(ADMIN, "adm:fsub:add"); await panel.handle_callback(cb)
    st, d = await users.get_state(ADMIN); ok("add prompts for input", st == "admin_input" and d["kind"] == "fsadd")
    m = mk_msg(ADMIN, "@SecondOne"); await panel.on_input(m)
    ok("public channel added", [c.title for c in svc.all()] == ["The Royal Zone", "Second One"] and "Second One added" in m.out[0][0])
    ok("state cleared after add", (await users.get_state(ADMIN))[0] != "admin_input")
    # duplicate
    await panel.handle_callback(mk_cb(ADMIN, "adm:fsub:add")); m = mk_msg(ADMIN, "https://t.me/SecondOne"); await panel.on_input(m)
    ok("duplicate rejected", len(svc.all()) == 2 and "already" in m.out[0][0])
    # bad inputs / unknown / not admin
    await panel.handle_callback(mk_cb(ADMIN, "adm:fsub:add")); m = mk_msg(ADMIN, "hello world!"); await panel.on_input(m)
    ok("garbage input rejected, still waiting", "Send the channel" in m.out[0][0] and (await users.get_state(ADMIN))[0] == "admin_input")
    m = mk_msg(ADMIN, "@Nobody123"); await panel.on_input(m); ok("unknown channel rejected", "can't find" in m.out[0][0])
    bot.chats["@NoAdmin"] = Chat_(-1004444444444, "No Admin", "NoAdmin")
    m = mk_msg(ADMIN, "@NoAdmin"); await panel.on_input(m); ok("bot-not-admin channel rejected", "not an administrator" in m.out[0][0] and len(svc.all()) == 2)
    # private channel -> needs invite link step
    m = mk_msg(ADMIN, "-1003333333333"); await panel.on_input(m)
    ok("private channel asks for link", (await users.get_state(ADMIN))[1].get("kind") == "fsadd2")
    m = mk_msg(ADMIN, "not a link"); await panel.on_input(m); ok("bad link rejected", len(svc.all()) == 2)
    m = mk_msg(ADMIN, "https://t.me/+AbCdEfGh12"); await panel.on_input(m)
    ok("private channel added with link", len(svc.all()) == 3 and svc.all()[2].url == "https://t.me/+AbCdEfGh12")
    # persistence: new service instance reads the same rows
    svc2 = ForceSubService(db); await svc2.load(); ok("persisted in database", len(svc2.all()) == 3)

    # ---- list / pick / edit pages
    text, kb = await panel.page("adm:fsub:list"); ok("list shows channels", "Second One" in text and "adm:fsub:edit:%d" % svc.all()[1].id in kbdata(kb))
    c2 = svc.all()[1]
    text, kb = await panel.page(f"adm:fsub:edit:{c2.id}"); ok("edit page", "Second One" in text and f"adm:fsub:tgl:{c2.id}" in kbdata(kb))
    text, kb = await panel.page("adm:fsub:pick:edit"); ok("pick edit lists channels", len(kbdata(kb)) == 3 + 2)
    # refresh
    await db.pool.execute("UPDATE force_sub_channels SET title='Renamed in DB' WHERE id=$1", c2.id)
    bot.bot_admin.discard(-1003333333333)
    cb = mk_cb(ADMIN, "adm:fsub:ref"); await panel.handle_callback(cb)
    ok("refresh reloads from DB + health", svc.get(c2.id).title == "Renamed in DB" and panel._fsub_health[-1003333333333] == "🔴" and panel._fsub_health[-1002222222222] == "🟢")
    # toggle
    cb = mk_cb(ADMIN, f"adm:fsub:tgl:{c2.id}"); await panel.handle_callback(cb)
    ok("pause channel", svc.get(c2.id).enabled is False and len(svc.enabled()) == 2)
    cb = mk_cb(ADMIN, f"adm:fsub:tgl:{c2.id}"); await panel.handle_callback(cb); ok("enable channel", svc.get(c2.id).enabled)
    # edit title + link
    await panel.handle_callback(mk_cb(ADMIN, f"adm:fsub:ctitle:{c2.id}")); await panel.on_input(mk_msg(ADMIN, "Second <b>Zone</b>"))
    ok("title edited", svc.get(c2.id).title == "Second <b>Zone</b>")
    text, _ = await panel.page(f"adm:fsub:edit:{c2.id}"); ok("title is HTML-escaped on display", "&lt;b&gt;" in text and "<b>Zone" not in text)
    await panel.handle_callback(mk_cb(ADMIN, f"adm:fsub:clink:{c2.id}")); m = mk_msg(ADMIN, "javascript:alert(1)"); await panel.on_input(m)
    ok("malicious link rejected", svc.get(c2.id).url == "https://t.me/SecondOne")
    await panel.on_input(mk_msg(ADMIN, "https://t.me/NewLink")); ok("link edited", svc.get(c2.id).url == "https://t.me/NewLink")

    # ---- gate with 3 channels
    cfgobj = types.SimpleNamespace(admin_ids=frozenset({ADMIN}))
    gate = F.ForceSubscribeGate(cfgobj, object(), users, F.ForceSubConfig(True, "@RoyalZone", "u", "n"), svc)
    async def run(ev):
        called = []
        async def h(e, d): called.append(1)
        await gate(h, ev, {"bot": bot}); return bool(called)
    ch = svc.all(); a, b, c = ch[0].chat_id, ch[1].chat_id, ch[2].chat_id
    m = mk_msg(USER); ok("non-member blocked (3 channels)", not await run(m) and len(kburls(m.out[0][1])) == 3)
    bot.members = {(a, USER): "member"}; m = mk_msg(USER)
    ok("member of only 1 of 3 blocked, only 2 join buttons", not await run(m) and len(kburls(m.out[0][1])) == 2 and "✅" in m.out[0][0])
    bot.members = {(a, USER): "member", (b, USER): "member", (c, USER): "creator"}
    ok("member of all 3 passes", await run(mk_msg(USER)))
    bot.members[(b, USER)] = "left"; m = mk_msg(USER)
    ok("leaving one channel blocks next message", not await run(m) and "NO LONGER ACTIVE" in m.out[0][0])
    await asyncio.sleep(F._VERIFY_COOLDOWN + .1)
    cbv = mk_cb(USER, F.VERIFY_DATA); await run(cbv); ok("verify while one missing fails", "NOT VERIFIED" in cbv.out[0][0])
    bot.members[(b, USER)] = "member"; await asyncio.sleep(F._VERIFY_COOLDOWN + .1)
    cbv = mk_cb(USER, F.VERIFY_DATA); await run(cbv); ok("rejoin + verify passes", cbv.out[0][0] == "MENU")
    # pause b -> user no longer needs it
    bot.members[(b, USER)] = "left"; await svc.update(c2.id, enabled=False)
    ok("paused channel not required", await run(mk_msg(USER)))
    await svc.update(c2.id, enabled=True)
    # admin adds a NEW channel later -> verified users must join it (and see 'new', not 'left')
    bot.members[(b, USER)] = "member"
    bot.chats["@Fourth"] = Chat_(-1005555555555, "Fourth", "Fourth"); bot.bot_admin.add(-1005555555555)
    await panel.handle_callback(mk_cb(ADMIN, "adm:fsub:add")); await panel.on_input(mk_msg(ADMIN, "@Fourth"))
    m = mk_msg(USER); ok("new channel immediately enforced", not await run(m) and "ACCESS RESTRICTED" in m.out[0][0] and "NO LONGER" not in m.out[0][0])
    # API error -> deny
    bot.fail = True; m = mk_msg(USER); ok("API error denies", not await run(m) and "UNAVAILABLE" in m.out[0][0]); bot.fail = False
    # no enabled channels -> everyone passes
    for x in svc.all(): await svc.update(x.id, enabled=False)
    ok("no enabled channel -> gate open", await run(mk_msg(USER)))

    # ---- remove flow
    text, kb = await panel.page(f"adm:fsub:rm:{c2.id}"); ok("remove asks confirmation", "Are you sure" in text and f"adm:fsub:rmok:{c2.id}" in kbdata(kb))
    ok("forged non-numeric id ignored", (await panel.page("adm:fsub:rm:abc"))[0].startswith("📢"))
    ok("unknown id ignored", (await panel.page("adm:fsub:edit:999999"))[0].startswith("📢"))
    n = len(svc.all()); await panel.handle_callback(mk_cb(ADMIN, f"adm:fsub:rmok:{c2.id}"))
    ok("channel removed", len(svc.all()) == n - 1 and svc.get(c2.id) is None)
    await panel.handle_callback(mk_cb(ADMIN, f"adm:fsub:rmok:{c2.id}")); ok("removing twice is harmless", len(svc.all()) == n - 1)
    # non-admin cannot use the panel callbacks
    cbx = mk_cb(USER, "adm:fsub:rmok:%d" % svc.all()[0].id); before = len(svc.all()); await panel.handle_callback(cbx)
    ok("non-admin cannot remove", len(svc.all()) == before)
    # removed seeded channel is not re-seeded on next start
    for x in svc.all(): await svc.remove(x.id)
    await svc.seed_default(bot, "@RoyalZone", "u", "n"); ok("removed default is not re-seeded", svc.all() == [])
    # home page via the real Panel.p_home needs videos service: just check button wiring in source
    import inspect, handlers.admin_panel as ap
    ok("dashboard has Force Subscribe Manager button", 'callback_data="adm:fsub"' in inspect.getsource(ap.Panel.p_home))
    print("ALL MANAGER TESTS PASSED"); await db.close()
asyncio.run(main())
