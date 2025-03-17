from fastapi import FastAPI, Depends, HTTPException, status, Form, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse
import sqlite3
import praw
import os
import smtplib
import requests
import asyncio
import secrets
from dotenv import load_dotenv
from email.mime.text import MIMEText

# Load environment variables
load_dotenv()

app = FastAPI()
security = HTTPBasic()
templates = Jinja2Templates(directory="templates")

# Reddit API Configuration
reddit = praw.Reddit(
    client_id=os.getenv("REDDIT_CLIENT_ID"),
    client_secret=os.getenv("REDDIT_CLIENT_SECRET"),
    user_agent=os.getenv("REDDIT_USER_AGENT"),
)

# Email Configuration
EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT"))
ADMIN_PASS = os.getenv("ADMIN_PASS")
CLIENT_PASS = os.getenv("CLIENT_PASS")
MAILERSEND_API_KEY = os.getenv("MAILERSEND_API_KEY")
EMAIL_FROM = os.getenv("EMAIL_FROM")

# Allowed Users (Set YOUR username & password here)
AUTHORIZED_USERS = {
    "admin": ADMIN_PASS,  
    "client": CLIENT_PASS  
}

def verify_user(credentials: HTTPBasicCredentials = Depends(security)):
    """Verifies if the user is authorized"""
    correct_password = AUTHORIZED_USERS.get(credentials.username)
    if not correct_password or not secrets.compare_digest(correct_password, credentials.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username  

# Database Setup
DB_FILE = "subreddits.db"
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            threshold INTEGER,
            notification_method TEXT,
            notification_target TEXT,
            backup_notification_target TEXT,
            tracking_enabled INTEGER DEFAULT 1
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subreddits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE
        )
    """)
    conn.commit()
    conn.close()
init_db()

# Get Global Settings
def get_global_settings():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Check if settings exist
    cursor.execute("SELECT threshold, notification_method, notification_target, backup_notification_target, tracking_enabled FROM settings LIMIT 1")
    settings = cursor.fetchone()

    if settings is None:
        # Insert default settings if none exist
        cursor.execute("INSERT INTO settings (threshold, notification_method, notification_target, backup_notification_target, tracking_enabled) VALUES (100, 'email', '', '', 0)")
        conn.commit()
        settings = (100, 'email', '', '', 0)  # Return default values

    conn.close()
    return settings

state_cache = {}

async def check_subreddits():
    """Continuously checks subreddits in the background"""
    while True:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM subreddits")
        subreddits = cursor.fetchall()
        threshold, method, target, backup_notification_target, tracking_enabled = get_global_settings()
        conn.close()
        if tracking_enabled == 0:
            print("⚠️ Tracking is disabled!")
            await asyncio.sleep(60)
            continue

        if threshold is None or method is None or target is None:
            print("⚠️ No global notification settings found!")
            await asyncio.sleep(60)
            continue

        notifications = []

        for (subreddit,) in subreddits:
            try:
                sub = reddit.subreddit(subreddit)
                active_users = sub.active_user_count
                previous_state = state_cache.get(subreddit, "below")

                if active_users >= threshold and previous_state == "below":
                    notifications.append(f"🔺 r/{subreddit} now has {active_users} users (Above Threshold)")
                    state_cache[subreddit] = "above"
                elif active_users < threshold and previous_state == "above":
                    notifications.append(f"🔻 r/{subreddit} now has {active_users} users (Below Threshold)")
                    state_cache[subreddit] = "below"

            except Exception as e:
                print(f"⚠️ Error checking r/{subreddit}: {e}")

        if notifications:
            send_email_notification(target, notifications, backup_notification_target)
        
        await asyncio.sleep(60)  

def send_email_notification(email, notifications, backup_notification_target):
    """Send consolidated email using MailerSend API"""
    subject = "Reddit Tracker: Subreddit Activity Summary"
    body = "\n".join(notifications) + "\n\nBest,\nReddit Tracker"

    headers = {
        "Authorization": f"Bearer {MAILERSEND_API_KEY}",
        "Content-Type": "application/json"
    }

    email_body = {
        "from": {"email": EMAIL_FROM, "name": "Reddit Tracker"},
        "to": [{"email": email}],
        "subject": subject,
        "text": body
    }

    response = requests.post("https://api.mailersend.com/v1/email", json=email_body, headers=headers)
    if response.status_code == 202:
        send_discord_notification(backup_notification_target, body)
        print(f"📧 MailerSend Email sent with {len(notifications)} subreddit updates")
    else:
        send_discord_notification(backup_notification_target, body)
        print(f"❌ Failed to send MailerSend email: {response.text}")


def send_discord_notification(webhook_url, body):
    """Send a Discord notification"""
    requests.post(webhook_url, json={"content": body})
    print(f"📨 Discord notification sent")

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, user: str = Depends(verify_user)):
    """Protected Homepage - Displays subreddits & global settings"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Fetch all tracked subreddits
    cursor.execute("SELECT name FROM subreddits")
    subreddits = cursor.fetchall()
    
    # Fetch global settings
    global_settings = get_global_settings()
    
    conn.close()

    return templates.TemplateResponse("index.html", {
        "request": request, 
        "subreddits": subreddits, 
        "global_settings": global_settings
    })


@app.post("/add_subreddit/")
async def add_subreddit(name: str = Form(...), user: str = Depends(verify_user)):
    """Allows the user to add a subreddit via the web UI"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Insert subreddit without individual settings
    cursor.execute("INSERT INTO subreddits (name) VALUES (?)", (name,))
    
    conn.commit()
    conn.close()
    
    return RedirectResponse(url="/", status_code=303)


@app.post("/update_settings/")
async def update_settings(
    threshold: int = Form(...), 
    notification_target: str = Form(...),
    backup_notification_target: str = Form(...), 
    user: str = Depends(verify_user)
):
    """Update Global Notification Settings"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Remove old settings
    cursor.execute("DELETE FROM settings")
    
    # Insert new settings
    cursor.execute(
        "INSERT INTO settings (threshold, notification_method, notification_target, backup_notification_target) VALUES (?, ?, ?, ?)",
        (threshold, 'email', notification_target, backup_notification_target)
    )
    
    conn.commit()
    conn.close()
    
    return RedirectResponse(url="/", status_code=303)


@app.post("/toggle_tracking/")
async def toggle_tracking(user: str = Depends(verify_user)):
    """Toggle tracking on/off"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Ensure there's at least one settings entry
    cursor.execute("SELECT tracking_enabled FROM settings LIMIT 1")
    current_status = cursor.fetchone()

    if current_status is None:
        # If no settings exist, initialize default settings
        cursor.execute("INSERT INTO settings (threshold, notification_method, notification_target, backup_notification_target tracking_enabled) VALUES (100, 'email', '', '', 1)")
        new_status = 1
    else:
        # Toggle the existing status
        new_status = 0 if current_status[0] == 1 else 1
        cursor.execute("UPDATE settings SET tracking_enabled = ?", (new_status,))

    conn.commit()
    conn.close()
    print(f"Tracking toggled to: {new_status}")  # Debugging output

    return RedirectResponse(url="/", status_code=303)


@app.post("/remove_subreddit/")
async def remove_subreddit(name: str = Form(...), user: str = Depends(verify_user)):
    """Allows the user to remove a subreddit via the web UI"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM subreddits WHERE name=?", (name,))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/", status_code=303)


@app.on_event("startup")
async def startup_event():
    """Starts the background task when the API starts"""
    asyncio.create_task(check_subreddits())
