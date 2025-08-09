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
