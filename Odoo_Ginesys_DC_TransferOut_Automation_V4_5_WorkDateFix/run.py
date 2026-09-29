import os
import threading
import time
import webbrowser

from waitress import serve

from app import app
from config_loader import ENV_FILE, load_config

# load_config() reloads the exact project-root .env with override=True.
try:
    startup_config = load_config()
    config_status = "VALID"
except Exception as exc:
    startup_config = None
    config_status = f"ERROR: {exc}"

host = os.getenv("HOST", "127.0.0.1")
port = int(os.getenv("PORT", "5064"))


def open_browser():
    time.sleep(1.2)
    webbrowser.open(f"http://localhost:{port}")


if __name__ == "__main__":
    threading.Thread(target=open_browser, daemon=True).start()
    print("=" * 66)
    print("Odoo -> Ginesys DC + Transfer Out Automation - V4.5 Multi-user WebAPI")
    print("=" * 66)
    print(f"Project .env : {ENV_FILE}")
    print(f"Configuration: {config_status}")
    print("Ginesys auth : Managed per user in the Admin Portal")
    print(f"Portal       : http://localhost:{port}")
    print("Press CTRL+C to stop.")
    print("=" * 66)
    serve(app, host=host, port=port, threads=8)
