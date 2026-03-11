#!/bin/bash
# Azure App Service startup — single worker (searches run as subprocesses)
gunicorn --bind 0.0.0.0:8000 --timeout 600 --workers 1 dashboard:app
