Gofile Uploader Telegram Bot
A Python-based Telegram bot for uploading files to Gofile.io with MongoDB integration, user management, and broadcast functionality.
Features

Upload files to Gofile with progress bar and speed display
View all your uploads with /myuploads
Generate sharable links with /getlink 
MongoDB for user and upload tracking
Broadcast messages to all users with /broadcast (admin only)
Handles Telegram flood limits
Sleek interface with loading animations

Setup

Clone the repository:git clone <your-repo-url>
cd gofile-bot


Install dependencies:pip install -r requirements.txt


Configure config.py with your credentials:
API_ID, API_HASH: From my.telegram.org
BOT_TOKEN: From @BotFather
GOFILE_TOKEN: From Gofile.io
MONGO_URI: Your MongoDB connection string
ADMIN_ID: Your Telegram user ID


Run the bot:python bot.py



Commands

/start: Welcome message and bot info
/upload: Upload a file (send as document)
/myuploads: List all your uploaded files
/getlink <content_id>: Get sharable link for a specific upload
/broadcast: Broadcast a message to all users (admin only)

Notes

Ensure your Gofile API token is valid.
MongoDB is required for user and upload storage.
The bot respects Telegram's flood limits to avoid bans.
Uploads are stored in MongoDB with content ID, file name, and links.

License
MIT License
