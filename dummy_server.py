import os, http.server, socketserver

PORT = int(os.environ.get("PORT", 10000))

class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

def run_dummy():
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        print(f"🔌 Dummy server listening on port {PORT}")
        httpd.serve_forever()
