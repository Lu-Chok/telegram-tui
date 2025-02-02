import threading
import asyncio
import queue
from pyrogram import Client, filters
import logging
from logging.handlers import RotatingFileHandler
import time
from PIL import Image
import io
import ascii_magic
import re

from config import API_ID, API_HASH, PHONE

# Set up rotating log files (keeps last 5 files, 1MB each)
def setup_logging():
    log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    # Telegram client logger
    client_handler = RotatingFileHandler(
        'telegram_client.log',
        maxBytes=1024*1024,  # 1MB
        backupCount=5
    )
    client_handler.setFormatter(log_formatter)
    
    client_logger = logging.getLogger('telegram')
    client_logger.setLevel(logging.DEBUG)
    client_logger.addHandler(client_handler)
    
    return client_logger

logger = setup_logging()

# Shared queue for sending events to the UI thread
ui_queue = queue.Queue()

class TelegramWorker:
    def __init__(self):
        self.app = None
        self.running = False
        self.thread = None
        self.active_chat_id = None  # Single source of truth for current chat
        self.messages_per_chat = {}
        self.loop = None
        self._initialized = False
        self.messages_loading = {}
        self.MESSAGES_PER_PAGE = 200

    def _add_message_to_chat(self, chat_id, new_message):
        """Helper to add message to chat with deduplication"""
        if chat_id not in self.messages_per_chat:
            self.messages_per_chat[chat_id] = []
        
        # Check if message already exists
        message_exists = any(msg['id'] == new_message['id'] 
                            for msg in self.messages_per_chat[chat_id])
        
        if not message_exists:
            # Add message and notify UI
            self.messages_per_chat[chat_id].append(new_message)
            ui_queue.put({
                "type": "new_message",
                "chat_id": chat_id,
                "message": new_message
            })
            return True
        return False

    async def _process_dialog(self, dialog):
        try:
            chat = dialog.chat
            chat_info = {
                'id': chat.id,
                'title': chat.title or chat.first_name or 'Unknown',
                'messages': [],
                'is_pinned': dialog.is_pinned,
                'type': chat.type.value,
                'unread_mark': dialog.unread_mark,
                'unread_messages_count': dialog.unread_messages_count,
                'unread_mentions_count': dialog.unread_mentions_count,
                # Add basic chat properties that are available directly
                'is_verified': getattr(chat, 'is_verified', False),
                'is_restricted': getattr(chat, 'is_restricted', False),
                'is_scam': getattr(chat, 'is_scam', False),
                'is_fake': getattr(chat, 'is_fake', False),
                'member_count': getattr(chat, 'members_count', None)
            }
            return chat_info
        except Exception as e:
            logger.error(f"Error processing dialog: {e}", exc_info=True)
            return None

    async def start_telegram_client(self):
        logger.info("Starting Telegram client...")
        try:
            self.app = Client(
                name=PHONE,
                phone_number=PHONE,
                api_id=API_ID,
                api_hash=API_HASH
            )

            @self.app.on_message(filters.incoming)
            def handle_new_message(client, message):
                logger.info(f"New message received from chat {message.chat.id}")
                chat_id = message.chat.id
                
                new_message = {
                    'id': message.id,
                    'text': message.text or '[media message]',
                    'timestamp': message.date,
                    'from_user': message.from_user.first_name if message.from_user else 'Unknown',
                    'is_outgoing': message.outgoing
                }
                
                self._add_message_to_chat(chat_id, new_message)

            try:
                logger.info("Starting app...")
                await self.app.start()
                logger.info("App started successfully")
                self._initialized = True

                # Load all chats at once
                logger.info("Loading chats...")
                all_chats = []
                total_loaded = 0
                
                ui_queue.put({
                    "type": "loading_progress",
                    "message": "Loading chats..."
                })
                
                async for dialog in self.app.get_dialogs():
                    try:
                        chat_info = await self._process_dialog(dialog)
                        if chat_info:
                            all_chats.append(chat_info)
                            total_loaded += 1
                            # Send more frequent updates
                            if total_loaded % 5 == 0:  # Update every 5 chats
                                ui_queue.put({
                                    "type": "loading_progress",
                                    "message": f"Loading chats... ({total_loaded} loaded)"
                                })
                    except Exception as e:
                        logger.error(f"Error processing dialog: {e}", exc_info=True)
                        continue

                # Sort chats: pinned first, then rest
                all_chats.sort(key=lambda x: (not x['is_pinned']))
                logger.info(f"Successfully loaded {len(all_chats)} chats")
                
                ui_queue.put({
                    "type": "chats_loaded",
                    "chats": all_chats,
                    "is_initial": True
                })

                # Keep the client running
                self.running = True
                while self.running:
                    await asyncio.sleep(1)

            except asyncio.TimeoutError:
                logger.error("Timeout while initializing Telegram client")
                ui_queue.put({
                    "type": "error",
                    "message": "Timeout while connecting to Telegram"
                })
                raise

        except Exception as e:
            logger.error(f"Error in start_telegram_client: {e}", exc_info=True)
            ui_queue.put({
                "type": "error",
                "message": f"Failed to start Telegram client: {str(e)}"
            })
            raise

    async def download_photo(self, message):
        """Download photo and convert to PIL Image"""
        try:
            if message.photo:
                logger.info(f"Downloading photo from message {message.id}")
                # Get photo dimensions
                photo = message.photo
                logger.info(f"Photo size: {photo.width}x{photo.height}")
                
                # Download the photo directly
                photo_bytes = await message.download(in_memory=True)
                logger.info(f"Downloaded photo bytes: {len(photo_bytes.getvalue())}")
                
                # Convert to PIL Image
                image = Image.open(io.BytesIO(photo_bytes.getvalue()))
                logger.info(f"Converted to PIL Image: {image.size}")
                return image
                
        except Exception as e:
            logger.error(f"Error downloading photo: {e}", exc_info=True)
            return None
        return None

    def _create_ascii_art(self, image, max_width, max_height=None):
        """Create ASCII art version of image"""
        try:
            logger.info(f"Creating ASCII art with dimensions {max_width}x{max_height}")
            
            # Calculate dimensions while preserving aspect ratio
            img_width, img_height = image.size
            aspect_ratio = img_width / img_height
            
            if max_height:
                # Calculate width based on height to preserve aspect ratio
                # Each character is roughly twice as tall as it is wide
                target_width = int(max_height * aspect_ratio * 2)
                # Use the smaller of our calculated width or max_width
                new_w = min(target_width, max_width)
            else:
                new_w = max(40, min(max_width - 4, 120))
            
            logger.info(f"Adjusted width to {new_w} for aspect ratio {aspect_ratio}")
            
            # Create ASCII art with basic settings
            ascii_img_obj = ascii_magic.from_pillow_image(image)
            logger.info("Created ASCII art object")
            
            # Get ASCII with width ratio adjustment
            output = ascii_img_obj.to_ascii(
                columns=new_w,
                width_ratio=1.5  # Adjust for terminal character width/height ratio
            )
            
            # Strip ANSI escape sequences
            ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
            clean_output = ansi_escape.sub('', str(output))
            
            logger.info(f"Generated ASCII output, length: {len(clean_output)}")
            return clean_output
            
        except Exception as e:
            logger.error(f"Error creating ASCII art: {e}", exc_info=True)
            return "[image conversion failed]"

    async def load_chat_history(self, chat_id, limit=200, before_message_id=None):
        """Load chat history with pagination support"""
        # Only load history for active chat
        if not self.app or chat_id != self.active_chat_id or chat_id in self.messages_loading:
            return

        logger.info(f"Loading history for chat {chat_id}" + 
                   (f" before message {before_message_id}" if before_message_id else ""))
        
        try:
            self.messages_loading[chat_id] = True
            messages = []
            
            # Use offset parameter for pagination
            kwargs = {
                'chat_id': chat_id,
                'limit': limit,
                'offset': 0 if before_message_id is None else len(self.messages_per_chat.get(chat_id, []))
            }
            
            async for message in self.app.get_chat_history(**kwargs):
                try:
                    msg_data = {
                        'id': message.id,
                        'text': message.text or '',
                        'timestamp': message.date,
                        'from_user': message.from_user.first_name if message.from_user else 'Unknown',
                        'is_outgoing': message.outgoing,
                        'has_photo': bool(message.photo),  # Just store if message has photo
                        'photo_info': message.photo if message.photo else None,  # Store photo metadata
                        'caption': message.caption
                    }

                    # Set text for photo messages
                    if msg_data['has_photo']:
                        if not msg_data['text']:
                            msg_data['text'] = '📷 Photo'
                            if message.caption:
                                msg_data['text'] += f": {message.caption}"
                            logger.info(f"Set message text to: {msg_data['text']}")

                    messages.append(msg_data)
                    
                except Exception as e:
                    logger.error(f"Error processing message {message.id}: {str(e)}", exc_info=True)
                    continue
            
            if messages and chat_id == self.active_chat_id:
                if chat_id not in self.messages_per_chat:
                    self.messages_per_chat[chat_id] = []
                
                # Messages from get_chat_history come in reverse chronological order (newest first)
                # We need to reverse them to get oldest first
                messages.reverse()
                
                # Add new messages to the existing ones
                if before_message_id:
                    # For older messages, add to beginning since they're older
                    self.messages_per_chat[chat_id] = messages + self.messages_per_chat[chat_id]
                else:
                    # For initial load
                    self.messages_per_chat[chat_id] = messages
                
                logger.info(f"Loaded {len(messages)} messages for chat {chat_id}")
                ui_queue.put({
                    "type": "chat_history_loaded",
                    "chat_id": chat_id,
                    "messages": self.messages_per_chat[chat_id],
                    "is_older_messages": bool(before_message_id)
                })
            
        except Exception as e:
            logger.error(f"Error loading chat history for {chat_id}: {str(e)}", exc_info=True)
            ui_queue.put({
                "type": "error",
                "message": f"Failed to load chat history: {str(e)}"
            })
        finally:
            self.messages_loading.pop(chat_id, None)

    def send_message(self, text):
        if self.active_chat_id and self.app:  # Use active_chat_id instead of current_chat_id
            # Schedule the async send_message in the event loop
            asyncio.run_coroutine_threadsafe(
                self.async_send_message(text),
                self.loop
            )

    async def async_send_message(self, text):
        try:
            sent_message = await self.app.send_message(self.active_chat_id, text)
            
            new_message = {
                'id': sent_message.id,
                'text': sent_message.text,
                'timestamp': sent_message.date,
                'from_user': 'You',
                'is_outgoing': True
            }
            
            self._add_message_to_chat(self.active_chat_id, new_message)
        except Exception as e:
            logger.error(f"Error sending message: {e}")
            ui_queue.put({
                "type": "error",
                "message": f"Failed to send message: {e}"
            })

    def set_current_chat(self, chat_id):
        """Switch to a different chat and load its history"""
        logger.info(f"Setting current chat to {chat_id}")
        
        # Clear previous chat's messages if switching to a different chat
        if self.active_chat_id != chat_id:
            self.messages_per_chat = {}  # Clear message cache
            self.active_chat_id = chat_id
        
        # Schedule chat history loading in the event loop
        asyncio.run_coroutine_threadsafe(
            self.load_chat_history(chat_id),
            self.loop
        )

    def stop(self):
        """Properly stop the worker and cleanup resources"""
        logger.info("Stopping worker...")
        self.running = False
        
        if self.loop and self.loop.is_running():
            try:
                # Schedule app stop in the event loop
                if self.app:
                    future = asyncio.run_coroutine_threadsafe(self.app.stop(), self.loop)
                    try:
                        # Wait with timeout for app to stop
                        future.result(timeout=3)
                    except Exception as e:
                        logger.error(f"Error stopping app: {e}")
                
                # Cancel all pending tasks
                for task in asyncio.all_tasks(self.loop):
                    task.cancel()
                
                # Run loop one last time to process cancellations
                self.loop.call_soon_threadsafe(lambda: asyncio.create_task(self._shutdown()))
                
                # Wait for thread to finish
                if self.thread and self.thread.is_alive():
                    self.thread.join(timeout=5)
                    
            except Exception as e:
                logger.error(f"Error during shutdown: {e}", exc_info=True)
            finally:
                # Ensure loop is closed
                if not self.loop.is_closed():
                    self.loop.stop()
                    self.loop.close()
        
        logger.info("Worker stopped")

    async def _shutdown(self):
        """Clean async shutdown sequence"""
        try:
            # Cancel all tasks
            tasks = [t for t in asyncio.all_tasks(self.loop) if t is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            
            # Wait for all tasks to complete
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            
            # Stop the loop
            self.loop.stop()
        except Exception as e:
            logger.error(f"Error in shutdown sequence: {e}", exc_info=True)

    async def get_message_photo(self, message_data):
        """Fetch photo data for a message on demand"""
        try:
            if message_data.get('has_photo') and message_data.get('photo_info'):
                logger.info(f"Fetching photo for message {message_data['id']}")
                
                # Create a dummy message object that has the photo data
                class DummyMessage:
                    def __init__(self, photo, msg_id, app):
                        self.photo = photo
                        self.id = msg_id
                        self._client = app  # Add reference to Pyrogram client
                    async def download(self, in_memory=True):
                        # Use Pyrogram's file_id to download
                        return await self._client.download_media(
                            self.photo.file_id,
                            in_memory=in_memory
                        )
                
                dummy_msg = DummyMessage(
                    message_data['photo_info'],
                    message_data['id'],
                    self.app  # Pass the Pyrogram client
                )
                image = await self.download_photo(dummy_msg)
                
                if image:
                    logger.info(f"Successfully fetched photo for message {message_data['id']}")
                    return {
                        'type': 'photo',
                        'data': image,
                        'caption': message_data.get('caption', '')
                    }
        except Exception as e:
            logger.error(f"Error fetching photo: {e}", exc_info=True)
        return None

def run_telegram_worker():
    worker = TelegramWorker()
    
    def run_worker():
        try:
            worker.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(worker.loop)
            worker.running = True
            
            try:
                worker.loop.run_until_complete(worker.start_telegram_client())
            except RuntimeError as e:
                if "Event loop stopped before Future completed" in str(e):
                    # This is expected during shutdown
                    logger.debug("Event loop stopped during shutdown")
                else:
                    raise
            except Exception as e:
                logger.error(f"Error in telegram client: {e}", exc_info=True)
                ui_queue.put({
                    "type": "error",
                    "message": f"Telegram client error: {e}"
                })
        finally:
            try:
                # Ensure all resources are cleaned up
                if not worker.loop.is_closed():
                    worker.loop.run_until_complete(worker.loop.shutdown_asyncgens())
                    worker.loop.close()
            except Exception as e:
                logger.error(f"Error during final cleanup: {e}", exc_info=True)

    worker.thread = threading.Thread(target=run_worker, daemon=True)
    worker.thread.start()
    return worker
