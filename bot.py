import os
import asyncio
import aiohttp
import time
import logging
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

# Helper functions
async def progress_bar(current, total, width=20):
    percent = current / total * 100
    filled = int(width * current // total)
    bar = "█" * filled + "—" * (width - filled)
    return f"[{bar}] {percent:.1f}%"

async def upload_to_gofile(file_path, message, retry_count=0, max_retries=3):
    url = "https://upload.gofile.io/uploadfile"
    headers = {"Authorization": f"Bearer {GOFILE_TOKEN}"}
    
    file_size = os.path.getsize(file_path)
    start_time = time.time()
    user_id = message.from_user.id
    
    logger.info(f"Starting upload for user {user_id}: {file_path} ({file_size} bytes)")
    
    async with aiohttp.ClientSession() as session:
        with open(file_path, "rb") as f:
            form = aiohttp.FormData()
            form.add_field("file", f)
            
            # Progress tracking
            uploaded = 0
            last_update = 0
            try:
                async with session.post(url, data=form, headers=headers) as resp:
                    if resp.status == 429:
                        if retry_count < max_retries:
                            wait_time = 2 ** retry_count
                            logger.warning(f"Rate limit hit for user {user_id}. Retrying in {wait_time}s")
                            await asyncio.sleep(wait_time)
                            return await upload_to_gofile(file_path, message, retry_count + 1, max_retries)
                        else:
                            logger.error(f"Max retries reached for user {user_id} on rate limit")
                            return None
                    
                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                        uploaded += len(chunk)
                        current_time = time.time()
                        if current_time - last_update >= 1:
                            speed = uploaded / (current_time - start_time) / 1024
                            bar = await progress_bar(uploaded, file_size)
                            await message.edit_text(
                                f"Uploading...\n{bar}\nSpeed: {speed:.2f} KB/s"
                            )
                            last_update = current_time
                            await asyncio.sleep(0.1)
                            
                    response = await resp.json()
                    if response["status"] == "ok":
                        logger.info(f"Upload successful for user {user_id}: {response['data']['downloadPage']}")
                        return response["data"]["downloadPage"]
                    else:
                        logger.error(f"Upload failed for user {user_id}: {response}")
                        return None
            except Exception as e:
                logger.error(f"Upload error for user {user_id}: {str(e)}")
                return None

async def get_sharable_link(content_id, user_id):
    url = f"https://api.gofile.io/contents/{content_id}/directlinks"
    headers = {"Authorization": f"Bearer {GOFILE_TOKEN}"}
    
    logger.info(f"Generating sharable link for content {content_id} by user {user_id}")
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers) as resp:
            response = await resp.json()
            if response["status"] == "ok":
                logger.info(f"Sharable link generated for content {content_id}")
                return response["data"]["directLink"]
            logger.error(f"Failed to generate sharable link for content {content_id}: {response}")
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
        "Welcome to Gofile Uploader Bot!\n"
        "📤 **Upload a file**: Send a document with /upload or directly send a file.\n"
        "📋 **View uploads**: Use /myuploads to see your files.\n"
        "🔗 **Get sharable link**: Use /getlink <content_id>.\n"
        "Note: Ensure files are sent as documents (not media) for upload."
    )

@app.on_message(filters.command("upload") | filters.document)
async def upload_file(client, message):
    user_id = message.from_user.id
    if not message.document:
        await message.reply_text("Please send a document to upload.")
        logger.warning(f"User {user_id} used /upload without a document")
        return
    
    file = message.document
    logger.info(f"Upload initiated for user {user_id}: {file.file_name} ({file.file_size} bytes)")
    
    file_path = await client.download_media(file)
    progress_msg = await message.reply_text("Starting upload...")
    
    try:
        download_page = await upload_to_gofile(file_path, progress_msg)
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
                f"Upload complete!\n"
                f"File: {file.file_name}\n"
                f"Download Page: {download_page}\n"
                f"Sharable Link: {sharable_link}\n"
                f"Content ID: {content_id}"
            )
        else:
            await progress_msg.edit_text("Upload failed. Try again.")
    except FloodWait as e:
        logger.warning(f"FloodWait for user {user_id}: waiting {e.x}s")
        await asyncio.sleep(e.x)
        await progress_msg.edit_text("Flood wait triggered. Retrying...")
        await upload_file(client, message)
    except Exception as e:
        logger.error(f"Unexpected error during upload for user {user_id}: {str(e)}")
        await progress_msg.edit_text("An error occurred. Please try again.")
    finally:
        try:
            os.remove(file_path)
            logger.info(f"Temporary file removed: {file_path}")
        except Exception as e:
            logger.error(f"Failed to remove temporary file {file_path}: {str(e)}")

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

# Run bot
if __name__ == "__main__":
    logger.info("Starting Gofile Uploader Bot")
    app.run()
