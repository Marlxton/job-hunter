# Job Hunter – mobile online deployment

Flat repository layout for easy GitHub mobile upload.

Files: `app.py`, `index.html`, `requirements.txt`, `config.py`, `render.yaml`, `README.md`.

## Render
Set the secret environment variable `JOOBLE_API_KEY` in Render. Do not commit the key to GitHub.

Build: `pip install -r requirements.txt`
Start: `gunicorn app:app --bind 0.0.0.0:$PORT`
