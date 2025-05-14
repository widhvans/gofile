import os
import asyncio
import aiohttp
import time
from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pymongo import MongoClient
from config import API_ID, API_HASH, BOT_TOKEN, GOFILE_TOKEN, MONGO_URI, ADMIN_ID
from datetime import datetime, timezone

# Initialize bot and MongoDB
app = Client("gofile_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["gofile_bot"]
users_collection = db["users"]
uploads_collection = db["uploads"]

# Helper functions
async def progress_bar(current, total, width=20):
    percent = current / total * 100
    filled = int(width * current // total)
    bar = "█" * filled + "—" * (width - filled)
    return f"[{bar}] {percent:.1f}%"

async def upload_to_gofile(file_path, message):
    url = "https://upload.gofile.io/uploadfile"
    headers = {"Authorization": f"Bearer {GOFILE_TOKEN}"}
    
    file_size = os.path.getsize(file_path)
    start_time = time.time()
    
    async with aiohttp.ClientSession() as session:
        with open(file_path, "rb") as f:
            form = aiohttp.FormData()
            form.add_field("file", f)
            
            # Progress tracking
            uploaded = 0
            last_update = 0
            async with session.post(url, data=form, headers=headers) as resp:
                async for chunk in resp.content.iter_chunked(1024 * 1024):
                    uploaded += len(chunk)
                    current_time = time.time()
                    if current_time - last_update >= 1:  # Update every second
                        speed = uploaded / (current_time - start_time) / 1024  # KB/s
                        bar = await progress_bar(uploaded, file_size)
                        await message.edit_text(
                            f"Uploading...\n{bar}\nSpeed: {speed:.2f} KB/s"
                        )
                        last_update = current_time
                        await asyncio.sleep(0.1)  # Respect Telegram flood limits
                        
                response = await resp.json()
                if response["status"] == "ok":
                    return response["data"]["downloadPage"]
                return None

async def get_sharable_link(content_id):
    url = f"https://api.gofile.io/contents/{content_id}/directlinks"
    headers = {"Authorization": f"Bearer {GOFILE_TOKEN}"}
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers) as resp:
            response = await resp.json()
            if response["status"] == "ok":
                return response["data"]["directLink"]
            return None

# Commands
@app.on_message(filters.command("start"))
async def start(client, message):
    user_id = message.from_user.id
    if users_collection.find_one({"user_id": user_id}) is None:
        users_collection.insert_one({
            "user_id": user_id,
            "username": message.from_user.username,
            "joined": datetime.now(timezone.utc)
        })
    await message.reply_text(
        "Welcome to Gofile Uploader Bot!\n"
        "Upload files with /upload\n"
        "View your uploads with /myuploads\n"
        "Get sharable link with /getlink <content_id>"
    )

@app.on_message(filters.command("upload") & filters.document)
async def upload_file(client, message):
    user_id = message.from_user.id
    file = message.document
    file_path = await client.download_media(file)
    
    progress_msg = await message.reply_text("Starting upload...")
    
    try:
        download_page = await upload_to_gofile(file_path, progress_msg)
        if download_page:
            content_id = download_page.split("/")[-1]
            sharable_link = await get_sharable_link(content_id)
            
            uploads_collection.insert_one({
                "user_id": user_id,
                "content_id": content_id,
                "file_name": file.file_name,
                "download_page": download_page,
                "sharable_link": sharable_link,
                "uploaded_at": datetime.now(timezone.utc)
            })
            
            await progress_msg.edit_text(
                f"Upload complete!\n"
                f"File: {file.file_name}\n"
                f"Download Page: {download_page}\n"
                f"Sharable Link: {sharable_link}\n"
                f"Content ID: {content_id}"
            )
        else:
            await progress_msg.edit_text("Upload failed. Try again.")
    except FloodWait as e:
        await asyncio.sleep(e.x)
        await progress_msg.edit_text("Flood wait triggered. Retrying...")
        await upload_file(client, message)
    finally:
        os.remove(file_path)

@app.on_message(filters.command("myuploads"))
async def my_uploads(client, message):
    user_id = message.from_user.id
    uploads = uploads_collection.find({"user_id": user_id})
    
    upload_list = list(uploads)
    if not upload_list:
        await message.reply_text("No uploads found.")
        return
    
    response = "Your Uploads:\n\n"
    for upload in upload_list:
        response += (
            f"File: {upload['file_name']}\n"
            f"Content ID: {upload['content_id']}\n"
            f"Download Page: {upload['download_page']}\n"
            f"Sharable Link: {upload['sharable_link']}\n"
            f"Uploaded: {upload['uploaded_at'].strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        )
    
    # Split response if too long to avoid Telegram message limits
    for i in range(0, len(response), 4000):
        await message.reply_text(response[i:i+4000])

@app.on_message(filters.command("getlink"))
async def get_link(client, message):
    try:
        content_id = message.text.split()[1]
        upload = uploads_collection.find_one({
            "user_id": message.from_user.id,
            "content_id": content_id
        })
        
        if upload:
            await message.reply_text(
                f"Sharable Link: {upload['sharable_link']}\n"
                f"Download Page: {upload['download_page']}"
            )
        else:
            await message.reply_text("Content ID not found or not yours.")
    except IndexError:
        await message.reply_text("Usage: /getlink <content_id>")

@app.on_message(filters.command("broadcast") & filters.user(ADMIN_ID))
async def broadcast(client, message):
    if not message.reply_to_message:
        await message.reply_text("Reply to a message to broadcast.")
        return
    
    users = users_collection.find()
    success_count = 0
    for user in users:
        try:
            await message.reply_to_message.forward(user["user_id"])
            success_count += 1
            await asyncio.sleep(0.1)  # Avoid flood limits
        except FloodWait as e:
            await asyncio.sleep(e.x)
        except Exception:
            continue
    
    await message.reply_text(f"Broadcast sent to {success_count} users.")

# Run bot
if __name__ == "__main__":
    app.run()
