import logging
import time
import random
import string
import os
from datetime import datetime, timedelta
from pymongo import MongoClient
from flask import Flask, request
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes
import requests
import threading
import asyncio
from dotenv import load_dotenv

# === Load environment variables ===
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
SHORTNER_API = os.getenv("SHORTNER_API")
FLASK_URL = os.getenv("FLASK_URL")
LIKE_API_URL = os.getenv("LIKE_API_URL")
HOW_TO_VERIFY_URL = os.getenv("HOW_TO_VERIFY_URL")
VIP_ACCESS_URL = os.getenv("VIP_ACCESS_URL")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.isdigit()]

# Constants
DAILY_REQUEST_LIMIT = 1
REQUEST_RESET_HOURS = 20

client = MongoClient(MONGO_URI)
db = client['likebot']
users = db['verifications']
profiles = db['users']
requests = db['requests']

# === Flask App ===
flask_app = Flask(__name__)

@flask_app.route("/verify/<code>")
def verify(code):
    user = users.find_one({"code": code})
    if user and not user.get("verified"):
        users.update_one({"code": code}, {"$set": {"verified": True, "verified_at": datetime.utcnow()}})
        return "✅ Verification successful. Bot will now process your like."
    return "❌ Link expired or already used."

async def check_user_requests(user_id):
    # Admins have unlimited requests
    if user_id in ADMIN_IDS:
        return float('inf')
    
    # Get user's last request time
    user_request = requests.find_one({"user_id": user_id})
    
    if not user_request:
        return DAILY_REQUEST_LIMIT  # New user gets full limit
    
    last_request_time = user_request.get("last_request_time")
    if not last_request_time:
        return DAILY_REQUEST_LIMIT
    
    # Check if reset period has passed
    time_since_last_request = datetime.utcnow() - last_request_time
    if time_since_last_request > timedelta(hours=REQUEST_RESET_HOURS):
        return DAILY_REQUEST_LIMIT
    
    return user_request.get("remaining_requests", DAILY_REQUEST_LIMIT)

async def update_user_requests(user_id):
    # Admins don't need to track requests
    if user_id in ADMIN_IDS:
        return True
    
    # Get current request count
    current_requests = await check_user_requests(user_id)
    
    if current_requests <= 0:
        return False
    
    # Update request count
    requests.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "last_request_time": datetime.utcnow(),
                "remaining_requests": current_requests - 1
            }
        },
        upsert=True
    )
    return True

# === Telegram Bot Commands ===
async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    remaining_requests = await check_user_requests(user_id)
    
    profile = profiles.find_one({"user_id": user_id}) or {}
    vip_expires = profile.get("vip_expires")
    is_vip = vip_expires and datetime.utcnow() < vip_expires
    
    if user_id in ADMIN_IDS:
        await update.message.reply_text("👑 *Admin Status*\n\nYou have unlimited requests and no verification required!", parse_mode='Markdown')
    elif is_vip:
        await update.message.reply_text("🌟 *VIP Status*\n\nYou have unlimited requests and no verification required!", parse_mode='Markdown')
    else:
        await update.message.reply_text(
            f"📊 *Your Request Status*\n\n"
            f"📅 Daily requests left: {remaining_requests}/{DAILY_REQUEST_LIMIT}\n"
            f"⏳ Requests reset every {REQUEST_RESET_HOURS} hours",
            parse_mode='Markdown'
        )

async def like_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    username = update.message.from_user.first_name or "User"
    
    # Check Admin or VIP status
    profile = profiles.find_one({"user_id": user_id}) or {}
    vip_expires = profile.get("vip_expires")
    is_vip = vip_expires and datetime.utcnow() < vip_expires
    is_admin = user_id in ADMIN_IDS
    
    # Check request limit for non-VIP and non-Admin users
    if not is_vip and not is_admin:
        remaining_requests = await check_user_requests(user_id)
        if remaining_requests <= 0:
            await update.message.reply_text("🚫 You have exceeded your daily request limit. Try again tomorrow.", parse_mode='Markdown')
            return

    try:
        args = update.message.text.split()
        region = args[1].lower()
        uid = args[2]
    except:
        await update.message.reply_text("❌ Wrong format. Use: /like <region> <uid>")
        return

    # For Admins and VIPs, process immediately without verification
    if is_admin or is_vip:
        try:
            api_url = LIKE_API_URL.format(uid=uid, region=region)
            api_resp = requests.get(api_url, timeout=10).json()
            
            if api_resp.get("status") == 1:
                player_nickname = api_resp.get("PlayerNickname", "Unknown")
                before = api_resp.get("LikesbeforeCommand", 0)
                after = api_resp.get("LikesafterCommand", 0)
                added = api_resp.get("LikesGivenByAPI", 0)

                result = (
                    f"✅ *Request Processed Successfully*\n\n"
                    f"👤 *Player:* {player_nickname}\n"
                    f"🆔 *UID:* `{uid}`\n"
                    f"👍 *Likes Before:* {before}\n"
                    f"✨ *Likes Added:* {added}\n"
                    f"🇮🇳 *Total Likes Now:* {after}\n"
                    f"⏰ *Processed At:* {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                profiles.update_one({"user_id": user_id}, {"$set": {"last_used": datetime.utcnow()}}, upsert=True)
            elif api_resp.get("status") == 2:
                result = f"❌ Max likes reached for your UID, please provide another UID"
            else:
                result = "❌ *API Error: Unable to process like*"

        except Exception as e:
            result = f"❌ *API Error: Unable to process like*\n\n🆔 *UID:* `{uid}`\n📛 Error: {str(e)}"

        await update.message.reply_text(result, parse_mode='Markdown')
        return

    # For regular users, require verification
    code = ''.join(random.choices(string.ascii_letters + string.digits, k=12))
    short_link = requests.get(
        f"https://shortner.in/api?api={SHORTNER_API}&url={FLASK_URL}/verify/{code}"
    ).json().get("shortenedUrl", f"{FLASK_URL}/verify/{code}")

    users.insert_one({
        "user_id": user_id,
        "uid": uid,
        "region": region,
        "code": code,
        "verified": False,
        "expires_at": datetime.utcnow() + timedelta(minutes=10),
        "chat_id": update.effective_chat.id,
        "message_id": update.message.message_id
    })

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ VERIFY & SEND LIKE ✅", url=short_link)],
        [InlineKeyboardButton("❓ How to Verify ❓", url=HOW_TO_VERIFY_URL)]
    ])

    msg = (
        f"🔒 *Verification Required*\n\n"
        f"🤵 *Hello:* {username}\n"
        f"🆔 *Uid:* `{uid}`\n"
        f"🌍 *Region:* {region}\n\n"
        f"Verify to get 1 more request. This is free\n"
        f"{short_link}\n"
        f"⚠️ Link expires in 10 minutes\n"
        f"*Purchase Vip&No Verify* {VIP_ACCESS_URL}"
    )
    await update.message.reply_text(msg, reply_markup=keyboard, parse_mode='Markdown')

async def addvip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("🚫 You are not authorized to use this command.")
        return
    try:
        target_id = int(context.args[0])
        days = int(context.args[1])
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Use: /addvip <user_id> <days>")
        return

    expiration_date = datetime.utcnow() + timedelta(days=days)
    profiles.update_one({"user_id": target_id}, {"$set": {"vip_expires": expiration_date}}, upsert=True)
    await update.message.reply_text(f"✅ VIP access granted to user `{target_id}` for {days} days (until {expiration_date.strftime('%Y-%m-%d %H:%M:%S')})", parse_mode='Markdown')

async def process_verified_likes(app: Application):
    while True:
        pending = users.find({"verified": True, "processed": {"$ne": True}})
        for user in pending:
            uid = user['uid']
            region = user.get('region', 'ind')
            user_id = user['user_id']
            profile = profiles.find_one({"user_id": user_id}) or {}
            vip_expires = profile.get("vip_expires")

            is_vip = vip_expires and datetime.utcnow() < vip_expires
            is_admin = user_id in ADMIN_IDS
            
            # For non-VIP and non-Admin users, check and update request count
            if not is_vip and not is_admin:
                request_updated = await update_user_requests(user_id)
                if not request_updated:
                    result = "🚫 You have exceeded your daily request limit. Try again tomorrow."
                    await app.bot.send_message(
                        chat_id=user['chat_id'],
                        reply_to_message_id=user['message_id'],
                        text=result,
                        parse_mode='Markdown'
                    )
                    users.update_one({"_id": user['_id']}, {"$set": {"processed": True}})
                    continue

            try:
                api_url = LIKE_API_URL.format(uid=uid, region=region)
                api_resp = requests.get(api_url, timeout=10).json()
                
                if api_resp.get("status") == 1:
                    player_nickname = api_resp.get("PlayerNickname", "Unknown")
                    before = api_resp.get("LikesbeforeCommand", 0)
                    after = api_resp.get("LikesafterCommand", 0)
                    added = api_resp.get("LikesGivenByAPI", 0)

                    result = (
                        f"✅ *Request Processed Successfully*\n\n"
                        f"👤 *Player:* {player_nickname}\n"
                        f"🆔 *UID:* `{uid}`\n"
                        f"👍 *Likes Before:* {before}\n"
                        f"✨ *Likes Added:* {added}\n"
                        f"🇮🇳 *Total Likes Now:* {after}\n"
                        f"⏰ *Processed At:* {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                    profiles.update_one({"user_id": user_id}, {"$set": {"last_used": datetime.utcnow()}}, upsert=True)
                elif api_resp.get("status") == 2:
                    result = f"❌ Max likes reached for your UID, please provide another UID"
                else:
                    result = "❌ *API Error: Unable to process like*"

            except Exception as e:
                result = f"❌ *API Error: Unable to process like*\n\n🆔 *UID:* `{uid}`\n📛 Error: {str(e)}"

            await app.bot.send_message(
                chat_id=user['chat_id'],
                reply_to_message_id=user['message_id'],
                text=result,
                parse_mode='Markdown'
            )

            users.update_one({"_id": user['_id']}, {"$set": {"processed": True}})
        await asyncio.sleep(5)

def run_bot():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("like", like_command))
    app.add_handler(CommandHandler("addvip", addvip_command))
    app.add_handler(CommandHandler("check", check_command))

    thread = threading.Thread(target=flask_app.run, kwargs={"host": "0.0.0.0", "port": 5000})
    thread.start()

    asyncio.get_event_loop().create_task(process_verified_likes(app))
    app.run_polling()

if __name__ == '__main__':
    run_bot()