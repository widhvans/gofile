import os
import asyncio
import aiohttp
import time
import logging
import psutil
from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pymongo import MongoClient
from config import API_ID, API_HASH, BOT_TOKEN, GOFILE_TOKEN, MONGO_URI, ADMIN_ID
from datetime import datetime, timezone

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Initialize bot and MongoDB
app = Client("gofile_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["gofile_bot"]
users_collection = db["users"]
uploads_collection = db["uploads"]

# Track ongoing tasks
ongoing_tasks = {}

# Check server resources
def check_resources(file_size):
    disk = psutil.disk_usage('/')
    free_disk = disk.free
    free_memory = psutil.virtual_memory().available
    logger.info(f"Resource check: Free disk {free_disk/1024/1024:.2f} MB, Free memory {free_memory/1024/1024:.2f} MB")
    return free_disk > file_size * 2 and free_memory > 512 * 1024 * 1024

# Progress bar
async def progress_bar(current, total, width=20):
    percent = current / total * 100
    filled = int(width * current // total)
    bar = "█" * filled + "—" * (width - filled)
    return f"[{bar}] {percent:.1f}%"

# Custom Telegram download with progress
async def download_file(client, file, file_path, message, user_id, task_id):
    start_time = time.time()
    downloaded = 0
    last_update = 0
    file_size = file.file_size
    
    logger.info(f"Starting Telegram download for user {user_id}: {file.file_name} ({file_size} bytes)")
    
    try:
        async with asyncio.timeout(600):  # 10min timeout
            async def progress(current, total):
                nonlocal downloaded, last_update
                downloaded = current
                current_time = time.time()
                if current_time - last_update >= 1 and task_id in ongoing_tasks:
                    speed = current / (current_time - start_time) / 1024
                    bar = await progress_bar(current, total)
                    logger.info(f"Download progress for user {user_id}: {bar} Speed: {speed:.2f} KB/s")
                    await message.edit_text(f"Downloading from Telegram...\n{bar}\nSpeed: {speed:.2f} KB/s")
                    last_update = current_time
                    await asyncio.sleep(0.1)
            
            await client.download_media(file, file_path, progress=progress)
            if task_id not in ongoing_tasks:
                logger.info(f"Download cancelled for user {user_id}: {file_path}")
                return False
            logger.info(f"Download completed for user {user_id}: {file_path}")
            return True
    except asyncio.TimeoutError:
        logger.error(f"Download timeout after 600s for user {user_id}: {file.file_name}")
        return False
    except FloodWait as e:
        logger.warning(f"FloodWait during download for user {user_id}: waiting {e.x}s")
        await asyncio.sleep(e.x)
        return await download_file(client, file, file_path, message, user_id, task_id)
    except Exception as e:
        logger.error(f"Download error for user {user_id}: {str(e)}")
        return False

async def upload_to_gofile(file_path, message, user_id, task_id):
    url = "https://upload.gofile.io/uploadfile"
    headers = {"Authorization": f"Bearer {GOFILE_TOKEN}"}
    
    file_size = os.path.getsize(file_path)
    start_time = time.time()
    timeout = 900 if file_size > 1024 * 1024 * 1024 else 600
    
    logger.info(f"Starting upload to Gofile for user {user_id}: {file_path} ({file_size} bytes)")
    
    async with aiohttp.ClientSession() as session:
        with open(file_path, "rb") as f:
            form = aiohttp.FormData()
            form.add_field("file", f)
            
            uploaded = 0
            last_update = 0
            try:
                logger.info(f"Connecting to Gofile API for user {user_id}")
                async with session.post(url, data=form, headers=headers, timeout=timeout) as resp:
                    logger.info(f"Gofile API response status for user {user_id}: {resp.status}")
                    if resp.status == 429:
                        wait_time = 2 ** ongoing_tasks[task_id]["retry_count"]
                        if ongoing_tasks[task_id]["retry_count"] < 5:
                            ongoing_tasks[task_id]["retry_count"] += 1
                            logger.warning(f"Rate limit (429) hit for user {user_id}. Retrying in {wait_time}s")
                            await asyncio.sleep(wait_time)
                            return await upload_to_gofile(file_path, message, user_id, task_id)
                        else:
                            logger.error(f"Max retries (5) reached for user {user_id} on rate limit")
                            return None
                    
                    async for chunk in resp.content.iter_chunked(512 * 1024):
                        if task_id not in ongoing_tasks:
                            logger.info(f"Upload cancelled for user {user_id}: {file_path}")
                            return None
                        
                        uploaded += len(chunk)
                        current_time = time.time()
                        if current_time - last_update >= 1:
                            speed = uploaded / (current_time - start_time) / 1024
                            bar = await progress_bar(uploaded, file_size)
                            logger.info(f"Upload progress for user {user_id}: {bar} Speed: {speed:.2f} KB/s")
                            await message.edit_text(f"Uploading...\n{bar}\nSpeed: {speed:.2f} KB/s")
                            last_update = current_time
                            await asyncio.sleep(0.1)
                            
                    response = await resp.json()
                    if response is None:
                        logger.error(f"Null response from Gofile API for user {user_id}")
                        return None
                    if response.get("status") == "ok":
                        logger.info(f"Upload successful for user {user_id}: {response['data']['downloadPage']}")
                        return response["data"]["downloadPage"]
                    else:
                        logger.error(f"Upload failed for user {user_id}: {response}")
                        return None
            except asyncio.TimeoutError:
                logger.error(f"Upload timeout after {timeout}s for user {user_id}: {file_path}")
                return None
            except Exception as e:
                logger.error(f"Upload error for user {user_id}: {str(e)}")
                return None

async def get_sharable_link(content_id, user_id):
    url = f"https://api.gofile.io/contents/{content_id}/directlinks"
    headers = {"Authorization": f"Bearer {GOFILE_TOKEN}"}
    
    logger.info(f"Generating sharable link for content {content_id} by user {user_id}")
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, headers=headers, timeout=30) as resp:
                response = await resp.json()
                if response is None:
                    logger.error(f"Null response for sharable link for content {content_id}")
                    return None
                if response.get("status") == "ok":
                    logger.info(f"Sharable link generated for content {content_id}")
                    return response["data"]["directLink"]
                logger.error(f"Failed to generate sharable link for content {content_id}: {response}")
                return None
        except Exception as e:
            logger.error(f"Sharable link error for user {user_id}: {str(e)}")
            return None

# Commands
@app.on_message(filters.command("start"))
async def start(client, message):
    user_id = message.from_user.id
    logger.info(f"Start command received from user {user_id}")
    
    if users_collection.find_one({"user_id": user_id}) is None:
        users_collection.insert_one({
            "user_id": user_id,
            "username": message.from_user.username,
            "joined": datetime.now(timezone.utc)
        })
        logger.info(f"New user registered: {user_id}")
    
    await message.reply_text(
        "Welcome to Gofile Uploader Bot! 🎉\n"
        "📤 **How to Upload**:\n"
        "1. Send /upload and attach a file as a document.\n"
        "2. Or directly send a file (select 'File' in Telegram, not 'Photo' or 'Video').\n"
        "📋 **View uploads**: /myuploads\n"
        "🔗 **Get sharable link**: /getlink <content_id>\n"
        "🛑 **Cancel upload**: /cancel (for large files)\n"
        "📊 **Check status**: /status\n"
        "⚠️ **Important**: Use documents (<1GB recommended). Photos/videos won't work.\n"
        "ℹ️ Use /help for more info."
    )

@app.on_message(filters.command("help"))
async def help_command(client, message):
    user_id = message.from_user.id
    logger.info(f"Help command received from user {user_id}")
    
    await message.reply_text(
        "Gofile Uploader Bot Help 📖\n"
        "📤 **Uploading Files**:\n"
        "- Use /upload and attach a document.\n"
        "- Or send a file directly (select 'File' in Telegram).\n"
        "- Example: Send a 10MB MP4 by choosing 'File' in the attach menu.\n"
        "- Recommended: Files <1GB for best performance.\n"
        "📋 **View Uploads**: /myuploads\n"
        "🔗 **Get Links**: /getlink <content_id>\n"
        "🛑 **Cancel**: /cancel to stop a download/upload.\n"
        "📊 **Status**: /status to check ongoing tasks.\n"
        "⚠️ **Note**: Documents only. Stable network required for large files."
    )

@app.on_message(filters.command("status"))
async def status_command(client, message):
    user_id = message.from_user.id
    logger.info(f"Status command received from user {user_id}")
    
    tasks = [task_id for task_id in ongoing_tasks if task_id.startswith(str(user_id))]
    if tasks:
        await message.reply_text(f"Ongoing tasks: {len(tasks)}. Use /cancel to stop.")
    else:
        await message.reply_text("No ongoing downloads or uploads.")

@app.on_message(filters.command("upload") | filters.document)
async def upload_file(client, message):
    user_id = message.from_user.id
    if not message.document:
        await message.reply_text("Please send a file as a document with /upload or directly as a file.")
        logger.warning(f"User {user_id} used /upload without a document")
        return
    
    file = message.document
    if file.file_size > 1024 * 1024 * 1024:
        await message.reply_text("File too large (>1GB). Try a smaller file for better performance.")
        logger.error(f"File too large for user {user_id}: {file.file_size} bytes")
        return
    
    logger.info(f"Upload initiated for user {user_id}: {file.file_name} ({file.file_size} bytes)")
    
    if not check_resources(file.file_size):
        await message.reply_text("Server resources low (disk/memory). Try a smaller file or contact admin.")
        logger.error(f"Insufficient resources for user {user_id}: {file.file_size} bytes")
        return
    
    task_id = f"{user_id}_{int(time.time())}"
    ongoing_tasks[task_id] = {"retry_count": 0}
    
    progress_msg = await message.reply_text("Downloading file from Telegram...")
    file_path = f"/tmp/{task_id}_{file.file_name}"
    
    try:
        if not await download_file(client, file, file_path, progress_msg, user_id, task_id):
            await progress_msg.edit_text("Download failed. Check network or try a smaller file.")
            return
        
        await progress_msg.edit_text("Starting upload to Gofile...")
        download_page = await upload_to_gofile(file_path, progress_msg, user_id, task_id)
        
        if download_page:
            content_id = download_page.split("/")[-1]
            sharable_link = await get_sharable_link(content_id, user_id)
            
            uploads_collection.insert_one({
                "user_id": user_id,
                "content_id": content_id,
                "file_name": file.file_name,
                "download_page": download_page,
                "sharable_link": sharable_link,
                "uploaded_at": datetime.now(timezone.utc)
            })
            logger.info(f"Upload recorded in MongoDB for user {user_id}: {content_id}")
            
            await progress_msg.edit_text(
                f"Upload complete! 🎉\n"
                f"File: {file.file_name}\n"
                f"Download Page: {download_page}\n"
                f"Sharable Link: {sharable_link}\n"
                f"Content ID: {content_id}"
            )
        else:
            await progress_msg.edit_text("Upload failed. Check logs or try again.")
    except FloodWait as e:
        logger.warning(f"FloodWait for user {user_id}: waiting {e.x}s")
        await asyncio.sleep(e.x)
        await progress_msg.edit_text("Flood wait triggered. Retrying...")
        await upload_file(client, message)
    except Exception as e:
        logger.error(f"Unexpected error during upload for user {user_id}: {str(e)}")
        await progress_msg.edit_text("An error occurred. Please try again.")
    finally:
        if task_id in ongoing_tasks:
            del ongoing_tasks[task_id]
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.info(f"Temporary file removed: {file_path}")
            except Exception as e:
                logger.error(f"Failed to remove temporary file {file_path}: {str(e)}")

@app.on_message(filters.command("cancel"))
async def cancel_upload(client, message):
    user_id = message.from_user.id
    logger.info(f"Cancel command received from user {user_id}")
    
    cancelled = False
    for task_id in list(ongoing_tasks.keys()):
        if task_id.startswith(str(user_id)):
            del ongoing_tasks[task_id]
            cancelled = True
            logger.info(f"Task cancelled for user {user_id}: {task_id}")
    
    if cancelled:
        await message.reply_text("Ongoing download/upload cancelled.")
    else:
        await message.reply_text("No ongoing tasks to cancel.")

@app.on_message(filters.command("myuploads"))
async def my_uploads(client, message):
    user_id = message.from_user.id
    logger.info(f"MyUploads command received from user {user_id}")
    
    uploads = uploads_collection.find({"user_id": user_id})
    upload_list = list(uploads)
    
    if not upload_list:
        await message.reply_text("No uploads found.")
        logger.info(f"No uploads found for user {user_id}")
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
    
    for i in range(0, len(response), 4000):
        await message.reply_text(response[i:i+4000])
    logger.info(f"Uploads listed for user {user_id}")

@app.on_message(filters.command("getlink"))
async def get_link(client, message):
    user_id = message.from_user.id
    logger.info(f"GetLink command received from user {user_id}")
    
    try:
        content_id = message.text.split()[1]
        upload = uploads_collection.find_one({
            "user_id": user_id,
            "content_id": content_id
        })
        
        if upload:
            await message.reply_text(
                f"Sharable Link: {upload['sharable_link']}\n"
                f"Download Page: {upload['download_page']}"
            )
            logger.info(f"Sharable link provided for user {user_id}: {content_id}")
        else:
            await message.reply_text("Content ID not found or not yours.")
            logger.warning(f"Invalid content ID {content_id} for user {user_id}")
    except IndexError:
        await message.reply_text("Usage: /getlink <content_id>")
        logger.error(f"Invalid /getlink syntax by user {user_id}")

@app.on_message(filters.command("broadcast") & filters.user(ADMIN_ID))
async def broadcast(client, message):
    user_id = message.from_user.id
    logger.info(f"Broadcast command received from admin {user_id}")
    
    if not message.reply_to_message:
        await message.reply_text("Reply to a message to broadcast.")
        logger.warning(f"Broadcast failed: no reply message from admin {user_id}")
        return
    
    users = users_collection.find()
    success_count = 0
    for user in users:
        try:
            await message.reply_to_message.forward(user["user_id"])
            success_count += 1
            await asyncio.sleep(0.1)
        except FloodWait as e:
            logger.warning(f"FloodWait during broadcast: waiting {e.x}s")
            await asyncio.sleep(e.x)
        except Exception as e:
            logger.error(f"Broadcast error for user {user['user_id']}: {str(e)}")
            continue
    
    await message.reply_text(f"Broadcast sent to {success_count} users.")
    logger.info(f"Broadcast completed by admin {user_id}: {success_count} users reached")

@app.on_message(filters.media & ~filters.document)
async def handle_non_document(client, message):
    user_id = message.from_user.id
    logger.warning(f"User {user_id} sent non-document media")
    await message.reply_text(
        "⚠️ Please send the file as a document (select 'File' in Telegram, not 'Photo' or 'Video').\n"
        "Try again or use /help for instructions."
    )

# Run bot
if __name__ == "__main__":
    logger.info("Starting Gofile Uploader Bot")
    app.run()
