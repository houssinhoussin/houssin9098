import os, threading, http.server, socketserver

# المنفذ الذي تُرسله Render في متغير البيئة PORT
PORT = int(os.environ.get("PORT", 10000))

# خادم HTTP بسيط (لا يعرض شيئًا فعليًا)
class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass  # منع طباعة السجلات غير الضرورية

def run_dummy():
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        print(f"🔌 Dummy server listening on port {PORT}")
        httpd.serve_forever()

# -------------- تشغيل البوت الحقيقي في خيط (Thread) --------------
def run_bot():
    import main  # هذا يستدعي main.py في الجذر ويبدأ TeleBot

threading.Thread(target=run_bot).start()
run_dummy()


@bot.message_handler(commands=['cancel'])
def cancel_cmd(m):
    try:
        for dct in (globals().get('_msg_by_id_pending', {}),
                    globals().get('_disc_new_user_state', {}),
                    globals().get('_admin_manage_user_state', {}),
                    globals().get('_address_state', {}),
                    globals().get('_phone_state', {})):
            try:
                dct.pop(m.from_user.id, None)
            except Exception:
                pass
    except Exception:
        pass
    try:
        bot.reply_to(m, "✅ تم الإلغاء ورجعناك للقائمة الرئيسية.")
    except Exception:
        bot.send_message(m.chat.id, "✅ تم الإلغاء.")
