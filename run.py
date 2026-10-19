import os
import secrets

_secret_key_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".secret_key")

if not os.getenv("FLASK_SECRET_KEY"):
    if os.path.exists(_secret_key_file):
        os.environ["FLASK_SECRET_KEY"] = open(_secret_key_file).read().strip()
    else:
        key = secrets.token_hex(32)
        os.environ["FLASK_SECRET_KEY"] = key
        with open(_secret_key_file, "w") as f:
            f.write(key)

from app import app, socketio

if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", 5000)),
        allow_unsafe_werkzeug=True,
    )
