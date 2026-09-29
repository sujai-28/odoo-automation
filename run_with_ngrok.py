import os
import sys
from pyngrok import ngrok

def main():
    print("=" * 55)
    print("  Starting Ngrok Tunnel...")
    print("=" * 55)
    try:
        public_url = ngrok.connect(9000)
        print("=" * 55)
        print(f"  Ngrok Tunnel Active  ->  {public_url}")
        print("=" * 55)
    except Exception as e:
        print("=" * 55)
        print("  WARNING: Failed to start ngrok tunnel:", e)
        print("  Please make sure your ngrok auth token is configured.")
        print("  You can configure it by running: ngrok config add-authtoken <TOKEN>")
        print("=" * 55)
    
    # Now run the app
    from app import app
    print("=" * 55)
    print("  Starting Flask Server on http://localhost:9000")
    print("=" * 55)
    app.run(debug=True, port=9000, use_reloader=False)

if __name__ == "__main__":
    main()
