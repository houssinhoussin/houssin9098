# -*- coding: utf-8 -*-
# handlers/links.py
import os
from telebot import types
from handlers import keyboards

# لو حابب تتحكم عبر .env (اختياري)
_LINK_SITE = os.getenv("LINK_SITE", "https://example.com")
_LINK_FB   = os.getenv("LINK_FACEBOOK", "https://facebook.com/")
_LINK_IG   = os.getenv("LINK_INSTAGRAM", "https://instagram.com/")

# فلاغ القائمة: menu:links
try:
    from services.feature_flags import block_if_disabled
except Exception:
    def block_if_disabled(bot, chat_id, key, label): return False

def register(bot, history):
    @bot.message_handler(func=lambda msg: msg.text == "🌐 صفحتنا")
    def open_links_menu(msg):
        if block_if_disabled(bot, msg.chat.id, "menu:links", "القائمة: صفحتنا/روابط"):
            return
        bot.send_message(msg.chat.id, "اختر الرابط:", reply_markup=keyboards.links_menu())

    @bot.message_handler(func=lambda msg: msg.text == "🌐 موقعنا")
    def site(msg):
        if block_if_disabled(bot, msg.chat.id, "menu:links", "القائمة: صفحتنا/روابط"):
            return
        bot.send_message(msg.chat.id, f"🌐 موقعنا:\n{_LINK_SITE}")

    @bot.message_handler(func=lambda msg: msg.text == "📘 فيس بوك")
    def fb(msg):
        if block_if_disabled(bot, msg.chat.id, "menu:links", "القائمة: صفحتنا/روابط"):
            return
        bot.send_message(msg.chat.id, f"📘 فيسبوك:\n{_LINK_FB}")

    @bot.message_handler(func=lambda msg: msg.text == "📸 إنستغرام")
    def ig(msg):
        if block_if_disabled(bot, msg.chat.id, "menu:links", "القائمة: صفحتنا/روابط"):
            return
        bot.send_message(msg.chat.id, f"📸 إنستغرام:\n{_LINK_IG}")
