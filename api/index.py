"""
Vercel serverless entry point.

Vercel routes all requests to api/index.py when vercel.json rewrites to /api/index.
This file imports the FastAPI app object and Vercel's Python runtime serves it via ASGI.
"""
import sys
import os

# Ensure the project root is on the path so news_app imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news_app.main import app  # noqa: F401 — Vercel picks up `app` by name
