"""
Invictus Annotate — Entry Point
Run with: python run.py
Accessible on LAN at: http://<your-ip>:8000   (set HOST=127.0.0.1 in .env for this machine only)
"""
import uvicorn
from app.config import HOST, PORT
from app.logsafe import uvicorn_log_config

if __name__ == "__main__":
    import socket
    hostname = socket.gethostname()
    try:
        local_ip = socket.gethostbyname(hostname)
    except Exception:
        local_ip = "localhost"

    print("=" * 55)
    print("  Invictus Annotate")
    print("=" * 55)
    print(f"  Local:   http://localhost:{PORT}")
    if HOST not in ("127.0.0.1", "localhost"):
        print(f"  Network: http://{local_ip}:{PORT}")
    print("=" * 55)
    print("  First run: sign in as admin / admin123 — you will be")
    print("  asked to choose a new password straight away.")
    print("  After updating the code, restart this server.")
    print("=" * 55)

    uvicorn.run(
        "app.main:socket_app",
        host=HOST,
        port=PORT,
        reload=False,
        workers=1,          # Single worker required for SQLite WAL
        log_level="info",
        log_config=uvicorn_log_config(),
        timeout_graceful_shutdown=2,
    )
