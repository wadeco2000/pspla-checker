#!/bin/bash
# Azure App Service startup — single worker (searches run as subprocesses)
pip install -r requirements.txt 2>/dev/null
gunicorn --bind 0.0.0.0:8000 --timeout 600 --workers 1 --preload dashboard:app
