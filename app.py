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

# Allowed Users (Set YOUR username & password here)
AUTHORIZED_USERS = {
    "admin": "your_password_here",  
    "client": "client_password_here"  
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
        CREATE TABLE IF NOT EXISTS subreddits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            threshold INTEGER,
            notification_method TEXT,
            notification_target TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

# State Tracking
state_cache = {}

async def check_subreddits():
    """Continuously checks subreddits in the background"""
    while True:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT name, threshold, notification_method, notification_target FROM subreddits")
        subreddits = cursor.fetchall()
        conn.close()

        for subreddit, threshold, method, target in subreddits:
            try:
                sub = reddit.subreddit(subreddit)
                active_users = sub.active_user_count
                previous_state = state_cache.get(subreddit, "below")

                if active_users >= threshold and previous_state == "below":
                    send_notification(method, target, subreddit, active_users, "above")
                    state_cache[subreddit] = "above"

                elif active_users < threshold and previous_state == "above":
                    send_notification(method, target, subreddit, active_users, "below")
                    state_cache[subreddit] = "below"

            except Exception as e:
                print(f"⚠️ Error checking r/{subreddit}: {e}")

        await asyncio.sleep(60)  

def send_notification(method, target, subreddit, active_users, status):
    """Send email or Discord notifications"""
    if method == "email":
        send_email_notification(target, subreddit, active_users, status)
    elif method == "discord":
        send_discord_notification(target, subreddit, active_users, status)

def send_email_notification(email, subreddit, active_users, status):
    """Send email alerts"""
    status_text = "🔺 Above" if status == "above" else "🔻 Below"
    subject = f"{status_text} Threshold Alert: {subreddit} has {active_users} users!"
    
    body = f"Hello,\n\nr/{subreddit} now has {active_users} active users.\n\n"
    body += f"Check it out: https://www.reddit.com/r/{subreddit}/\n\nBest,\nReddit Tracker"

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = EMAIL_SENDER
    msg["To"] = email

    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, email, msg.as_string())
        server.quit()
        print(f"📧 Email sent to {email} for r/{subreddit} ({active_users} users) - {status_text}")
    except Exception as e:
        print(f"❌ Failed to send email: {e}")

def send_discord_notification(webhook_url, subreddit, active_users, status):
    """Send a Discord notification"""
    status_text = "🔺 Above" if status == "above" else "🔻 Below"
    message = f"{status_text} Threshold Alert!\n**r/{subreddit}** now has **{active_users}** users!\n[Check it out](https://www.reddit.com/r/{subreddit}/)"
    requests.post(webhook_url, json={"content": message})
    print(f"📨 Discord notification sent for r/{subreddit} ({active_users} users) - {status_text}")

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, user: str = Depends(verify_user)):
    """Protected Homepage - Only accessible if logged in"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT name, threshold, notification_method, notification_target FROM subreddits")
    subreddits = cursor.fetchall()
    conn.close()

    return templates.TemplateResponse("index.html", {"request": request, "subreddits": subreddits})

@app.post("/add_subreddit/")
async def add_subreddit(name: str = Form(...), threshold: int = Form(...), notification_method: str = Form(...), notification_target: str = Form(...), user: str = Depends(verify_user)):
    """Allows the user to add a subreddit via the web UI"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO subreddits (name, threshold, notification_method, notification_target) VALUES (?, ?, ?, ?)",
        (name, threshold, notification_method, notification_target)
    )
    conn.commit()
    conn.close()
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
