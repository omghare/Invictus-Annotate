"""
Invictus Annotate — Entry Point
Run with: python run.py
Accessible on LAN at: http://<your-ip>:8000
"""
import uvicorn
from app.config import PORT

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
    print(f"  Network: http://{local_ip}:{PORT}")
    print("=" * 55)
    print("  Default admin: admin / admin123")
    print("  (Change password after first login)")
    print("=" * 55)

    uvicorn.run(
        "app.main:socket_app",
        host="0.0.0.0",
        port=PORT,
        reload=False,
        workers=1,          # Single worker required for SQLite WAL
        log_level="info",
        timeout_graceful_shutdown=2,
    )
