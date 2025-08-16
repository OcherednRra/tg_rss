import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Dict, List, Optional, Set, Union

import aiohttp
import feedparser
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from bs4 import BeautifulSoup
from dotenv import load_dotenv


load_dotenv()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_IDS = os.getenv("ADMIN_IDS", "").split(",") 
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))
FEEDS_FILE = "rss_feeds.json"


router = Router()

posted_articles: Set[str] = set()


def load_feeds() -> List[Dict[str, str]]:
    if os.path.exists(FEEDS_FILE):
        try:
            with open(FEEDS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading feeds file: {str(e)}")
    return []


def save_feeds(feeds: List[Dict[str, str]]) -> bool:
    try:
        with open(FEEDS_FILE, "w", encoding="utf-8") as f:
            json.dump(feeds, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"Error saving feeds file: {str(e)}")
        return False


def clean_html(html_text):
    if not html_text:
        return ""
    
    soup = BeautifulSoup(html_text, "html.parser")
    text = soup.get_text(separator=" ", strip=True)
    
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    
    return text


def is_admin(user_id: int) -> bool:
    return str(user_id) in ADMIN_IDS


def is_valid_url(url: str) -> bool:
    pattern = re.compile(
        r'^(https?://)?'
        r'([a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}'
        r'(/[^/\s]*)*$' 
    )
    return bool(pattern.match(url))


class RSSFeedMonitor:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.feeds = load_feeds()
        self.session: Optional[aiohttp.ClientSession] = None
        self.running = False

    async def start(self) -> None:
        if self.running:
            return
            
        self.running = True
        self.session = aiohttp.ClientSession()
        
        while self.running:
            try:
                await self.check_all_feeds()
                await asyncio.sleep(CHECK_INTERVAL)
            except Exception as e:
                logger.error(f"Error in feed monitor: {str(e)}")
                await asyncio.sleep(60) 

    async def stop(self) -> None:
        self.running = False
        if self.session:
            await self.session.close()
            self.session = None

    async def reload_feeds(self) -> None:
        self.feeds = load_feeds()
        logger.info(f"Reloaded {len(self.feeds)} feeds from file")

    async def add_feed(self, url: str, name: str) -> bool:
        for feed in self.feeds:
            if feed["url"] == url:
                return False

        self.feeds.append({"url": url, "name": name})
        
        if save_feeds(self.feeds):
            return True
        return False

    async def remove_feed(self, url: str) -> bool:
        for i, feed in enumerate(self.feeds):
            if feed["url"] == url:
                del self.feeds[i]
                if save_feeds(self.feeds):
                    return True
                break
        return False

    async def check_all_feeds(self) -> None:
        if not self.feeds:
            logger.info("No feeds configured")
            return
            
        for feed_config in self.feeds:
            try:
                logger.info(f"Checking feed: {feed_config['name']}")
                await self.check_feed(feed_config["url"], feed_config["name"])
            except Exception as e:
                logger.error(f"Error checking feed {feed_config['name']}: {str(e)}")

    async def check_feed(self, feed_url: str, feed_name: str) -> None:
        if not self.session:
            self.session = aiohttp.ClientSession()
            
        try:
            async with self.session.get(feed_url, timeout=30) as response:
                if response.status != 200:
                    logger.warning(f"Failed to fetch {feed_name}: HTTP {response.status}")
                    return

                content = await response.text()
                feed = feedparser.parse(content)

                if feed.bozo: 
                    logger.warning(f"Error parsing feed {feed_name}: {feed.bozo_exception}")
                    return

                for entry in reversed(feed.entries[:1]): 
                    entry_id = entry.get("id", entry.get("link", ""))
                    if not entry_id or entry_id in posted_articles:
                        continue

                    success = await self.post_entry(entry, feed_name)
                    if success:
                        posted_articles.add(entry_id)
                        await asyncio.sleep(2)

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error(f"Network error when fetching {feed_name}: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error processing {feed_name}: {str(e)}")

    async def post_entry(self, entry, feed_name: str) -> bool:
        try:
            pub_date = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                pub_date = datetime.fromtimestamp(time.mktime(entry.published_parsed))
            elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                pub_date = datetime.fromtimestamp(time.mktime(entry.updated_parsed))

            date_str = f"\n📅 {pub_date.strftime('%Y-%m-%d %H:%M')}" if pub_date else ""

            title = entry.get("title", "No title")
            link = entry.get("link", "")
            description = entry.get("description", "")
            
            clean_description = clean_html(description)
            
            short_description = clean_description[:200] + "..." if len(clean_description) > 200 else clean_description

            message = (
                f"📢 <b>{feed_name}</b>{date_str}\n\n"
                f"<b>{title}</b>\n\n"
                f"{short_description}\n\n"
                f"<a href='{link}'>Read more</a>"
            )

            channel_id = CHANNEL_ID
            if not channel_id.startswith('@') and not channel_id.lstrip('-').isdigit():
                channel_id = '@' + channel_id

            await self.bot.send_message(
                chat_id=channel_id,
                text=message,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False
            )
            logger.info(f"Posted new article: {title}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to post article: {str(e)}")
            return False


feed_monitor = None

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 Welcome to the RSS Feed Bot!\n\n"
        "I can post updates from RSS feeds to a Telegram channel.\n\n"
        "Available commands:\n"
        "/add [url] [name] - Add a new RSS feed\n"
        "/list - List all configured feeds\n"
        "/remove [url] - Remove a feed\n"
        "/check - Check feeds now\n\n"
        "Note: Admin privileges are required for these commands."
    )


@router.message(Command("add"))
async def cmd_add_feed(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ You don't have permission to use this command.")
        return

    # Parse command arguments
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        await message.answer(
            "⚠️ Usage: /add [url] [name]\n"
            "Example: /add https://example.com/rss 'Example Blog'"
        )
        return

    url = args[1]
    name = args[2]

    if not is_valid_url(url):
        await message.answer("⛔ Invalid URL format. Please provide a valid RSS feed URL.")
        return

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=30) as response:
                if response.status != 200:
                    await message.answer(f"⛔ Failed to fetch the feed: HTTP {response.status}")
                    return

                content = await response.text()
                feed = feedparser.parse(content)

                if feed.bozo:
                    await message.answer(f"⛔ Invalid RSS feed: {feed.bozo_exception}")
                    return

                if not feed.entries:
                    await message.answer("⚠️ Warning: This feed has no entries. It might not be valid.")
    except Exception as e:
        await message.answer(f"⛔ Error validating feed: {str(e)}")
        return

    global feed_monitor
    if await feed_monitor.add_feed(url, name):
        await message.answer(f"✅ Successfully added feed: {name}")
    else:
        await message.answer("⛔ Failed to add feed. It might already exist.")


@router.message(Command("list"))
async def cmd_list_feeds(message: Message):
    """Handler for the /list command."""
    if not is_admin(message.from_user.id):
        await message.answer("⛔ You don't have permission to use this command.")
        return

    global feed_monitor
    feeds = feed_monitor.feeds

    if not feeds:
        await message.answer("No RSS feeds configured.")
        return

    response = "📋 Configured RSS Feeds:\n\n"
    for i, feed in enumerate(feeds, 1):
        response += f"{i}. <b>{feed['name']}</b>\n"
        response += f"   URL: {feed['url']}\n\n"

    await message.answer(response, parse_mode=ParseMode.HTML)


@router.message(Command("remove"))
async def cmd_remove_feed(message: Message):
    """Handler for the /remove command."""
    if not is_admin(message.from_user.id):
        await message.answer("⛔ You don't have permission to use this command.")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "⚠️ Usage: /remove [url]\n"
            "Example: /remove https://example.com/rss\n\n"
            "Use /list to see all feeds and their URLs."
        )
        return

    url = args[1]

    global feed_monitor
    if await feed_monitor.remove_feed(url):
        await message.answer("✅ Successfully removed feed.")
    else:
        await message.answer("⛔ Failed to remove feed. URL not found.")


@router.message(Command("check"))
async def cmd_check_feeds(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("You don't have permission to use this command.")
        return

    await message.answer("Checking feeds now...")
    
    global feed_monitor
    try:
        await feed_monitor.check_all_feeds()
        await message.answer("Finished checking feeds.")
    except Exception as e:
        await message.answer(f"Error checking feeds: {str(e)}")


async def main() -> None:
    if not BOT_TOKEN:
        logger.error("Bot token not configured")
        return
        
    if not CHANNEL_ID:
        logger.error("Channel ID not configured")
        return
        
    if not ADMIN_IDS:
        logger.error("No admin IDs configured")
        return

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    
    dp.include_router(router)
    
    logger.info(f"Starting bot with channel ID: {CHANNEL_ID}")
    logger.info(f"Admin IDs: {ADMIN_IDS}")
    
    try:
        channel_id = CHANNEL_ID
        if not channel_id.startswith('@') and not channel_id.lstrip('-').isdigit():
            channel_id = '@' + channel_id
            
        chat = await bot.get_chat(channel_id)
        logger.info(f"Successfully connected to channel: {chat.title}")
    except Exception as e:
        logger.error(f"Cannot access channel {CHANNEL_ID}: {str(e)}")
        logger.error("Make sure the bot is added as an admin to the channel")
        return
    
    global feed_monitor
    feed_monitor = RSSFeedMonitor(bot)
    
    if not os.path.exists(FEEDS_FILE):
        save_feeds([])
    
    try:
        feed_monitor_task = asyncio.create_task(feed_monitor.start())
        
        logger.info("Starting bot...")
        await dp.start_polling(bot)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Stopping services...")
    finally:
        await feed_monitor.stop()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
