import os
import asyncio
import aiohttp
import time
import logging
import psutil
from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
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

# Determine file extension
def get_file_extension(message):
    if message.document:
        return os.path.splitext(message.document.file_name)[1] or ".bin"
    elif message.video:
        return ".mp4"
    elif message.photo:
        return ".jpg"
    elif message.audio:
        return ".mp3"
    return ".bin"

# Estimate remaining time
def estimate_remaining_time(current, total, speed):
    if speed <= 0:
        return "N/A"
    remaining_bytes = total - current
    remaining_time = remaining_bytes / (speed * 1024)  # Speed in KB/s
    return f"{int(remaining_time)}s"

# Custom Telegram download
async def download_file(client, message, file_path, progress_msg, user_id, task_id):
    start_time = time.time()
    downloaded = 0
    last_update = 0
    
    file_size = (message.document.file_size if message.document else
                 message.video.file_size if message.video else
                 message.photo.file_size if message.photo else
                 message.audio.file_size if message.audio else 0)
    file_name = (message.document.file_name if message.document else
                 f"video_{int(time.time())}{get_file_extension(message)}" if message.video else
                 f"photo_{int(time.time())}{get_file_extension(message)}" if message.photo else
                 f"audio_{int(time.time())}{get_file_extension(message)}" if message.audio else "unknown.bin")
    
    logger.info(f"Starting Telegram download for user {user_id}: {file_path} ({file_size} bytes)")
    
    try:
        async with asyncio.timeout(600):
            async def progress(current, total):
                nonlocal downloaded, last_update
                downloaded = current
                current_time = time.time()
                if current_time - last_update >= 5 and task_id in ongoing_tasks:
                    speed = current / (current_time - start_time) / 1024
                    bar = await progress_bar(current, total)
                    remaining_time = estimate_remaining_time(current, total, speed)
                    interface = (
                        f"══════✦✦✦══════\n"
                        f"📥 **Downloading from Telegram...**\n"
                        f"📜 File: {file_name}\n"
                        f"📏 Size: {total/1024/1024:.2f} MB\n"
                        f"⬇️ Downloaded: {current/1024/1024:.2f} MB\n"
                        f"📊 Progress: {bar}\n"
                        f"🚀 Speed: {speed:.2f} KB/s\n"
                        f"⏳ ETA: {remaining_time}\n"
                        f"══════✦✦✦══════\n"
                    )
                    try:
                        await progress_msg.edit_text(
                            interface,
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("❌ Cancel Upload", callback_data=f"cancel_{task_id}")]
                            ])
                        )
                    except FloodWait as e:
                        logger.warning(f"FloodWait during download update for user {user_id}: waiting {e.x}s")
                        await asyncio.sleep(e.x)
                    last_update = current_time
                    await asyncio.sleep(0.1)
            
            media = (message.document or message.video or message.photo or message.audio)
            await client.download_media(media, file_path, progress=progress)
            if task_id not in ongoing_tasks:
                logger.info(f"Download cancelled for user {user_id}: {file_path}")
                return False, None
            logger.info(f"Download completed for user {user_id}: {file_path}")
            
            # Attach thumbnail if available
            thumbnail = None
            if message.video and message.video.thumbs:
                thumbnail = await client.download_media(message.video.thumbs[0])
            elif message.photo:
                thumbnail = file_path  # Photo itself is the thumbnail
            
            return True, thumbnail
    except asyncio.TimeoutError:
        logger.error(f"Download timeout after 600s for user {user_id}")
        return False, None
    except FloodWait as e:
        logger.warning(f"FloodWait during download for user {user_id}: waiting {e.x}s")
        await asyncio.sleep(e.x)
        return await download_file(client, message, file_path, progress_msg, user_id, task_id)
    except Exception as e:
        logger.error(f"Download error for user {user_id}: {str(e)}")
        return False, None

# Gofile server selection
async def get_gofile_server(user_id):
    url = "https://api.gofile.io/servers"
    headers = {"Authorization": f"Bearer {GOFILE_TOKEN}"}
    
    logger.info(f"Fetching Gofile servers for user {user_id}")
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, timeout=30) as resp:
                response = await resp.json()
                if response is None:
                    logger.error(f"Null response when fetching Gofile servers for user {user_id}")
                    return None
                if response.get("status") == "ok":
                    servers = response["data"]["servers"]
                    for server in servers:
                        if server["zone"] == "na":
                            logger.info(f"Selected Gofile server for user {user_id}: {server['name']}")
                            return server["name"]
                    logger.info(f"Selected first Gofile server for user {user_id}: {servers[0]['name']}")
                    return servers[0]["name"]
                logger.error(f"Failed to fetch Gofile servers for user {user_id}: {response}")
                return None
        except Exception as e:
            logger.error(f"Error fetching Gofile servers for user {user_id}: {str(e)}")
            return None

async def upload_to_gofile(file_path, progress_msg, user_id, task_id, retry_count=0, max_retries=5, server=None):
    base_url = f"https://{server}.gofile.io/uploadfile" if server else "https://upload.gofile.io/uploadfile"
    headers = {"Authorization": f"Bearer {GOFILE_TOKEN}"}
    
    file_size = os.path.getsize(file_path)
    file_name = os.path.basename(file_path).split("_", 2)[-1]
    start_time = time.time()
    timeout = 1200 if file_size > 512 * 1024 * 1024 else 600
    
    logger.info(f"Starting upload to Gofile for user {user_id}: {file_path} ({file_size} bytes) via {base_url}")
    
    async with aiohttp.ClientSession() as session:
        with open(file_path, "rb") as f:
            form = aiohttp.FormData()
            form.add_field("file", f)
            
            uploaded = 0
            last_update = 0
            try:
                logger.info(f"Connecting to Gofile API for user {user_id}")
                async with session.post(base_url, data=form, headers=headers, timeout=timeout) as resp:
                    logger.info(f"Gofile API response status for user {user_id}: {resp.status}")
                    raw_response = await resp.text()
                    if resp.status == 429:
                        wait_time = 2 ** retry_count
                        if retry_count < max_retries:
                            logger.warning(f"Rate limit (429) hit for user {user_id}. Retrying in {wait_time}s")
                            await asyncio.sleep(wait_time)
                            return await upload_to_gofile(file_path, progress_msg, user_id, task_id, retry_count + 1, max_retries, server)
                        else:
                            logger.error(f"Max retries ({max_retries}) reached for user {user_id} on rate limit")
                            return None
                    
                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                        if task_id not in ongoing_tasks:
                            logger.info(f"Upload cancelled for user {user_id}: {file_path}")
                            return None
                        
                        uploaded += len(chunk)
                        current_time = time.time()
                        if current_time - last_update >= 5:
                            speed = uploaded / (current_time - start_time) / 1024
                            bar = await progress_bar(uploaded, file_size)
                            remaining_time = estimate_remaining_time(uploaded, file_size, speed)
                            interface = (
                                f"══════✦✦✦══════\n"
                                f"📤 **Uploading to Gofile...**\n"
                                f"📜 File: {file_name}\n"
                                f"📏 Size: {file_size/1024/1024:.2f} MB\n"
                                f"⬆️ Uploaded: {uploaded/1024/1024:.2f} MB\n"
                                f"📊 Progress: {bar}\n"
                                f"🚀 Speed: {speed:.2f} KB/s\n"
                                f"⏳ ETA: {remaining_time}\n"
                                f"══════✦✦✦══════\n"
                            )
                            try:
                                await progress_msg.edit_text(
                                    interface,
                                    reply_markup=InlineKeyboardMarkup([
                                        [InlineKeyboardButton("❌ Cancel Upload", callback_data=f"cancel_{task_id}")]
                                    ])
                                )
                            except FloodWait as e:
                                logger.warning(f"FloodWait during upload update for user {user_id}: waiting {e.x}s")
                                await asyncio.sleep(e.x)
                            last_update = current_time
                            await asyncio.sleep(0.1)
                            
                    if not raw_response:
                        logger.error(f"Empty response from Gofile API for user {user_id}")
                        if retry_count < max_retries:
                            logger.warning(f"Retrying upload for user {user_id} due to empty response. Attempt {retry_count + 1}/{max_retries}")
                            await asyncio.sleep(2 ** retry_count)
                            return await upload_to_gofile(file_path, progress_msg, user_id, task_id, retry_count + 1, max_retries, server)
                        logger.error(f"Max retries ({max_retries}) reached for user {user_id} on empty response")
                        return None
                    
                    response = await resp.json()
                    if response is None:
                        logger.error(f"Null response from Gofile API for user {user_id}: {raw_response}")
                        if retry_count < max_retries:
                            server = await get_gofile_server(user_id) if not server else None
                            logger.warning(f"Retrying upload for user {user_id} with server {server or 'global'}. Attempt {retry_count + 1}/{max_retries}")
                            await asyncio.sleep(2 ** retry_count)
                            return await upload_to_gofile(file_path, progress_msg, user_id, task_id, retry_count + 1, max_retries, server)
                        logger.error(f"Max retries ({max_retries}) reached for user {user_id} on null response")
                        return None
                    if response.get("status") == "ok":
                        logger.info(f"Upload successful for user {user_id}: {response['data']['downloadPage']}")
                        return response["data"]["downloadPage"]
                    logger.error(f"Upload failed for user {user_id}: {response}")
                    return None
            except asyncio.TimeoutError:
                logger.error(f"Upload timeout after {timeout}s for user {user_id}: {file_path}")
                return None
            except Exception as e:
                logger.error(f"Upload error for user {user_id}: {str(e)}")
                if retry_count < max_retries:
                    server = await get_gofile_server(user_id) if not server else None
                    logger.warning(f"Retrying upload for user {user_id} with server {server or 'global'}. Attempt {retry_count + 1}/{max_retries}")
                    await asyncio.sleep(2 ** retry_count)
                    return await upload_to_gofile(file_path, progress_msg, user_id, task_id, retry_count + 1, max_retries, server)
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
        "🌟 **Welcome to Gofile Uploader Bot!** 🌟\n\n"
        "📤 **Upload Files**:\n"
        "- Send any file (document, video, photo, audio) up to 2GB.\n"
        "- Use /upload or directly send the file.\n\n"
        "📋 **View Uploads**:\n"
        "- Use /myuploads to see your files (paginated).\n\n"
        "🔗 **Get Links**:\n"
        "- Use /getlink <content_id> for sharable links.\n\n"
        "🛑 **Cancel**:\n"
        "- Use /cancel to stop ongoing tasks.\n\n"
        "📊 **Status**:\n"
        "- Use /status to check ongoing tasks.\n\n"
        "🧪 **Test API**:\n"
        "- Use /test to verify Gofile API.\n\n"
        "⚠️ **Note**:\n"
        "- Ensure a stable network for large files.\n"
        "- Use /help for detailed instructions."
    )

@app.on_message(filters.command("help"))
async def help_command(client, message):
    user_id = message.from_user.id
    logger.info(f"Help command received from user {user_id}")
    
    await message.reply_text(
        "📖 **Gofile Uploader Bot Help** 📖\n\n"
        "📤 **Uploading Files**:\n"
        "- Send any file (document, video, photo, audio) up to 2GB.\n"
        "- Example: Attach an MP4 and choose 'File', or send a photo directly.\n"
        "- Use /upload or send the file directly.\n\n"
        "📋 **View Uploads**:\n"
        "- /myuploads: See your files with pagination.\n\n"
        "🔗 **Get Links**:\n"
        "- /getlink <content_id>: Get sharable links.\n\n"
        "🛑 **Cancel**:\n"
        "- /cancel: Stop ongoing downloads/uploads.\n\n"
        "📊 **Status**:\n"
        "- /status: Check ongoing tasks.\n\n"
        "🧪 **Test**:\n"
        "- /test: Verify Gofile API with a small file.\n\n"
        "⚠️ **Note**:\n"
        "- Large files (<2GB) need a stable network.\n"
        "- Ensure server resources are sufficient."
    )

@app.on_message(filters.command("test"))
async def test_command(client, message):
    user_id = message.from_user.id
    logger.info(f"Test command received from user {user_id}")
    
    test_file_path = "/tmp/test_file.txt"
    with open(test_file_path, "w") as f:
        f.write("Test file for Gofile API")
    
    task_id = f"{user_id}_{int(time.time())}"
    ongoing_tasks[task_id] = {"retry_count": 0}
    
    progress_msg = await message.reply_text("🧪 **Testing Gofile API with a small file...**")
    
    try:
        download_page = await upload_to_gofile(test_file_path, progress_msg, user_id, task_id)
        if download_page:
            content_id = download_page.split("/")[-1]
            await progress_msg.edit_text(f"✅ **Test Upload Successful!**\nDownload Page: {download_page}")
        else:
            await progress_msg.edit_text("❌ **Test Upload Failed.** Check logs or try again.")
    except Exception as e:
        logger.error(f"Test upload error for user {user_id}: {str(e)}")
        await progress_msg.edit_text("❌ **Test Upload Failed.** Check logs.")
    finally:
        if task_id in ongoing_tasks:
            del ongoing_tasks[task_id]
        if os.path.exists(test_file_path):
            os.remove(test_file_path)
            logger.info(f"Test file removed: {test_file_path}")

@app.on_message(filters.command("status"))
async def status_command(client, message):
    user_id = message.from_user.id
    logger.info(f"Status command received from user {user_id}")
    
    tasks = [task_id for task_id in ongoing_tasks if task_id.startswith(str(user_id))]
    if tasks:
        await message.reply_text(f"📊 **Ongoing Tasks**: {len(tasks)}\nUse /cancel to stop.")
    else:
        await message.reply_text("✅ **No Ongoing Tasks**")

@app.on_message(filters.command("upload") | filters.media)
async def upload_file(client, message):
    user_id = message.from_user.id
    if not (message.document or message.video or message.photo or message.audio):
        await message.reply_text("📎 **Please send a file (document, video, photo, audio) to upload.**")
        logger.warning(f"User {user_id} used /upload without a file")
        return
    
    file_size = (message.document.file_size if message.document else
                 message.video.file_size if message.video else
                 message.photo.file_size if message.photo else
                 message.audio.file_size if message.audio else 0)
    file_name = (message.document.file_name if message.document else
                 f"video_{int(time.time())}{get_file_extension(message)}" if message.video else
                 f"photo_{int(time.time())}{get_file_extension(message)}" if message.photo else
                 f"audio_{int(time.time())}{get_file_extension(message)}" if message.audio else "unknown.bin")
    
    if file_size > 2 * 1024 * 1024 * 1024:  # 2GB limit
        await message.reply_text("📏 **File too large (>2GB).** Telegram limit is 2GB for bots.")
        logger.error(f"File too large for user {user_id}: {file_size} bytes")
        return
    
    logger.info(f"Upload initiated for user {user_id}: {file_name} ({file_size} bytes)")
    
    if not check_resources(file_size):
        await message.reply_text("⚠️ **Server resources low (disk/memory).** Try a smaller file or contact admin.")
        logger.error(f"Insufficient resources for user {user_id}: {file_size} bytes")
        return
    
    task_id = f"{user_id}_{int(time.time())}"
    ongoing_tasks[task_id] = {"retry_count": 0}
    
    progress_msg = await message.reply_text("📥 **Downloading from Telegram...**")
    file_path = f"/tmp/{task_id}_{file_name}"
    
    try:
        success, thumbnail = await download_file(client, message, file_path, progress_msg, user_id, task_id)
        if not success:
            await progress_msg.edit_text("❌ **Download Failed.** Check network or try a smaller file.")
            return
        
        await progress_msg.edit_text("📤 **Starting upload to Gofile...**")
        download_page = await upload_to_gofile(file_path, progress_msg, user_id, task_id)
        
        if download_page:
            content_id = download_page.split("/")[-1]
            sharable_link = await get_sharable_link(content_id, user_id)
            
            uploads_collection.insert_one({
                "user_id": user_id,
                "content_id": content_id,
                "file_name": file_name,
                "file_size": file_size,
                "download_page": download_page,
                "sharable_link": sharable_link,
                "uploaded_at": datetime.now(timezone.utc)
            })
            logger.info(f"Upload recorded in MongoDB for user {user_id}: {content_id}")
            
            interface = (
                f"══════✦✦✦══════\n"
                f"✅ **Upload Complete!** 🎉\n"
                f"📜 **File**: {file_name}\n"
                f"📏 **Size**: {file_size/1024/1024:.2f} MB\n"
                f"🌐 **Download Page**: {download_page}\n"
                f"🔗 **Sharable Link**: {sharable_link}\n"
                f"🆔 **Content ID**: {content_id}\n"
                f"══════✦✦✦══════\n"
            )
            if thumbnail:
                await progress_msg.delete()
                progress_msg = await message.reply_photo(
                    photo=thumbnail,
                    caption=interface,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📲 Click to Share", url=sharable_link)]
                    ]),
                    disable_web_page_preview=True
                )
                os.remove(thumbnail)
            else:
                await progress_msg.edit_text(
                    interface,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📲 Click to Share", url=sharable_link)]
                    ]),
                    disable_web_page_preview=True
                )
    except FloodWait as e:
        logger.warning(f"FloodWait for user {user_id}: waiting {e.x}s")
        await asyncio.sleep(e.x)
        await progress_msg.edit_text("⏳ **Flood wait triggered. Retrying...**")
        await upload_file(client, message)
    except Exception as e:
        logger.error(f"Unexpected error during upload for user {user_id}: {str(e)}")
        await progress_msg.edit_text("❌ **An Error Occurred.** Please try again.")
    finally:
        if task_id in ongoing_tasks:
            del ongoing_tasks[task_id]
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.info(f"Temporary file removed: {file_path}")
            except Exception as e:
                logger.error(f"Failed to remove temporary file {file_path}: {str(e)}")

@app.on_callback_query(filters.regex(r"cancel_(\d+_\d+)"))
async def cancel_callback(client, callback_query):
    user_id = callback_query.from_user.id
    task_id = callback_query.data.split("_", 1)[1]
    logger.info(f"Cancel callback received from user {user_id} for task {task_id}")
    
    if task_id in ongoing_tasks and task_id.startswith(str(user_id)):
        del ongoing_tasks[task_id]
        await callback_query.message.edit_text("✅ **Upload Cancelled.**")
        logger.info(f"Task {task_id} cancelled for user {user_id}")
    else:
        await callback_query.message.edit_text("❌ **Task not found or already completed.**")
    await callback_query.answer()

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
        await message.reply_text("✅ **Ongoing download/upload cancelled.**")
    else:
        await message.reply_text("❌ **No ongoing tasks to cancel.**")

@app.on_message(filters.command("myuploads"))
async def my_uploads(client, message):
    user_id = message.from_user.id
    logger.info(f"MyUploads command received from user {user_id}")
    
    uploads = list(uploads_collection.find({"user_id": user_id}).sort("uploaded_at", -1))
    
    if not uploads:
        await message.reply_text("📭 **No uploads found.**")
        logger.info(f"No uploads found for user {user_id}")
        return
    
    page = 1
    per_page = 5
    total_pages = (len(uploads) + per_page - 1) // per_page
    
    await show_uploads_page(client, message, uploads, page, per_page, total_pages, user_id)

async def show_uploads_page(client, message, uploads, page, per_page, total_pages, user_id):
    start = (page - 1) * per_page
    end = start + per_page
    uploads_page = uploads[start:end]
    
    response = f"📚 **Your Uploads** (Page {page}/{total_pages})\n\n"
    for idx, upload in enumerate(uploads_page, start + 1):
        file_size = upload.get('file_size', 0)  # Fallback for missing file_size
        response += (
            f"📄 **{idx}. {upload['file_name']}**\n"
            f"📏 Size: {file_size/1024/1024:.2f} MB\n"
            f"🆔 Content ID: {upload['content_id']}\n"
            f"🌐 Download: [Link]({upload['download_page']})\n"
            f"🔗 Share: [Link]({upload['sharable_link']})\n"
            f"📅 Uploaded: {upload['uploaded_at'].strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        )
    
    buttons = []
    if page > 1:
        buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"myuploads_{page-1}"))
    if page < total_pages:
        buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"myuploads_{page+1}"))
    
    reply_markup = InlineKeyboardMarkup([buttons]) if buttons else None
    await message.reply_text(response, reply_markup=reply_markup, disable_web_page_preview=True)
    logger.info(f"Uploads page {page} displayed for user {user_id}")

@app.on_callback_query(filters.regex(r"myuploads_(\d+)"))
async def my_uploads_callback(client, callback_query):
    page = int(callback_query.data.split("_")[1])
    user_id = callback_query.from_user.id
    logger.info(f"MyUploads page {page} requested by user {user_id}")
    
    uploads = list(uploads_collection.find({"user_id": user_id}).sort("uploaded_at", -1))
    per_page = 5
    total_pages = (len(uploads) + per_page - 1) // per_page
    
    await callback_query.message.delete()
    await show_uploads_page(client, callback_query.message, uploads, page, per_page, total_pages, user_id)
    await callback_query.answer()

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
                f"🔗 **Sharable Link**: {upload['sharable_link']}\n"
                f"🌐 **Download Page**: {upload['download_page']}"
            )
            logger.info(f"Sharable link provided for user {user_id}: {content_id}")
        else:
            await message.reply_text("❌ **Content ID not found or not yours.**")
            logger.warning(f"Invalid content ID {content_id} for user {user_id}")
    except IndexError:
        await message.reply_text("📋 **Usage**: /getlink <content_id>")
        logger.error(f"Invalid /getlink syntax by user {user_id}")

@app.on_message(filters.command("broadcast") & filters.user(ADMIN_ID))
async def broadcast(client, message):
    user_id = message.from_user.id
    logger.info(f"Broadcast command received from admin {user_id}")
    
    if not message.reply_to_message:
        await message.reply_text("📢 **Reply to a message to broadcast.**")
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
    
    await message.reply_text(f"📢 **Broadcast sent to {success_count} users.**")
    logger.info(f"Broadcast completed by admin {user_id}: {success_count} users reached")

# Run bot
if __name__ == "__main__":
    logger.info("Starting Gofile Uploader Bot")
    app.run()
