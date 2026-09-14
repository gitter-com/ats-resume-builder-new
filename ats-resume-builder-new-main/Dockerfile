# Official Playwright image already has Chromium + all its system deps
# pre-installed, so we don't need "playwright install" or manual apt-get
# chasing of missing .so files on Render's build machine.
#
# IMPORTANT: this tag's version MUST match the "playwright==X.Y.Z" pin in
# requirements.txt exactly — the image only ships browser binaries for
# its own Playwright version, and a mismatch fails at browser launch time
# with "Executable doesn't exist at ...chrome-headless-shell".
FROM mcr.microsoft.com/playwright/python:v1.62.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p uploads outputs

EXPOSE 10000

# scan.py now runs headless=True, so no virtual display is needed —
# gunicorn can be launched directly. -w 1 is deliberate: app.py keeps
# scan sessions in an in-memory dict, which would go out of sync across
# multiple worker processes.
CMD gunicorn -w 1 -k gthread --threads 4 --timeout 300 --graceful-timeout 300 -b 0.0.0.0:${PORT:-10000} app:app

