# Lets "gunicorn run:flask_app" (Render's default command) work as well as "gunicorn app:app"
from app import app as flask_app
