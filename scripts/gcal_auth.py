"""
One-time Google sign-in for the re-census calendar reminders.

Needs gcal_credentials.json (OAuth client, type "Desktop app") in the app folder.
Opens a browser, then saves gcal_token.json next to it.

Usage:
    python scripts/gcal_auth.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import gcal_reminder

if __name__ == '__main__':
    path = gcal_reminder.authorize_interactive()
    print(f'Token saved: {path}')
