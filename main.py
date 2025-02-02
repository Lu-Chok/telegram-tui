import locale
import os
import json
import os.path
import logging
from logging.handlers import RotatingFileHandler
import asyncio
from datetime import datetime, timedelta
import re

# Force UTF-8 encoding
os.environ['LANG'] = 'en_US.UTF-8'
os.environ['LC_ALL'] = 'en_US.UTF-8'
locale.setlocale(locale.LC_ALL, '')

import curses
import time
from telegram_worker import ui_queue, run_telegram_worker
import queue

# Set up rotating log files (keeps last 5 files, 1MB each)
def setup_logging():
    log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    # Main UI logger
    ui_handler = RotatingFileHandler(
        'ui.log',
        maxBytes=1024*1024,  # 1MB
        backupCount=5
    )
    ui_handler.setFormatter(log_formatter)
    
    ui_logger = logging.getLogger('ui')
    ui_logger.setLevel(logging.INFO)
    ui_logger.addHandler(ui_handler)
    
    # Root logger for uncaught exceptions
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.WARNING)
    root_logger.addHandler(ui_handler)
    
    # Suppress Pyrogram logs
    logging.getLogger('pyrogram').setLevel(logging.WARNING)
    logging.getLogger('asyncio').setLevel(logging.WARNING)
    
    return ui_logger

logger = setup_logging()

def draw_sidebar(win, chats, current_idx, ui_state, worker):
    win.erase()
    win.box()
    win.addstr(0, 2, " Chats ")
    max_y, max_x = win.getmaxyx()
    
    # Simplified mode indicator
    mode_str = " Mode: " + ("Favorites" if ui_state.display_mode == 2 else "All")
    win.addstr(0, max_x - len(mode_str) - 1, mode_str)
    
    for idx, chat in enumerate(chats):  # chats is already filtered, so we use it directly
        try:
            y_pos = idx + 1
            is_selected = idx == current_idx
            
            # Determine chat color and indicators
            prefix = ""
            if chat['id'] in ui_state.favorites:
                prefix = "★ "
            elif chat['is_pinned']:
                prefix = "📌 "
            elif chat.get('is_verified', False):
                prefix = "✓ "
            elif chat.get('is_scam', False) or chat.get('is_fake', False):
                prefix = "⚠ "
            
            # Color selection logic
            if chat['is_pinned']:
                color = curses.color_pair(7) | curses.A_BOLD  # White bold for pinned
            elif chat.get('is_restricted', False):
                color = curses.color_pair(1)  # Red for restricted
            elif chat.get('unread_mentions_count', 0) > 0:
                color = curses.color_pair(5)  # Purple for mentions
            elif chat.get('unread_messages_count', 0) > 0:
                color = curses.color_pair(4)  # Blue for unread messages
            elif chat.get('is_muted', False):
                color = curses.color_pair(8)  # Gray for muted
            else:
                color = curses.color_pair(7)  # White for normal
            
            # Add unread counter and member count to title
            title = f"{prefix}{chat['title']}"
            if chat.get('member_count'):
                title = f"{title} ({chat['member_count']})"
            
            unread_count = chat.get('unread_messages_count', 0)
            mentions_count = chat.get('unread_mentions_count', 0)
            
            if mentions_count > 0:
                title = f"{title} [@{mentions_count}]"
            elif unread_count > 0:
                title = f"{title} [{unread_count}]"
            
            if len(title) > max_x - 4:
                title = title[:max_x - 7] + "..."
            
            if is_selected:
                win.attron(curses.A_REVERSE)
            win.attron(color)
            win.addstr(y_pos, 1, f" {title} ".ljust(max_x - 2).encode('utf-8'))
            win.attroff(color)
            if is_selected:
                win.attroff(curses.A_REVERSE)
                
        except curses.error:
            pass

    win.refresh()

def draw_chat_header(win, chat_name):
    win.erase()
    win.box()
    # Display chat name; later, you might display a small image here.
    win.addstr(0, 2, f" {chat_name} ")
    win.refresh()

def format_message(msg):
    """Format a message with timestamp and sender"""
    timestamp = msg['timestamp']
    sender = msg['from_user']
    text = msg['text']
    
    # Get current date
    today = datetime.now().date()
    msg_date = timestamp.date()
    
    # Format time
    time_str = timestamp.strftime('%H:%M')
    
    # Format date
    if msg_date == today:
        date_str = "Today"
    elif msg_date == today - timedelta(days=1):
        date_str = "Yesterday"
    else:
        date_str = timestamp.strftime('%d.%m.%Y')
    
    # Format: [HH:MM date_str]
    timestamp_str = f"[{time_str} {date_str}]"
    
    # Handle different message types
    if msg.get('is_outgoing'):
        return f"{timestamp_str} → {text}"
    else:
        return f"{timestamp_str} {sender}: {text}"

def draw_messages(win, messages, scroll_position=0):
    """Draw messages with highlight for current scroll position"""
    win.erase()
    win.box()
    max_y, max_x = win.getmaxyx()
    available_lines = max_y - 2  # Space between box borders
    
    # Calculate which messages to show
    total_messages = len(messages)
    start_idx = max(0, total_messages - available_lines - scroll_position)
    end_idx = min(total_messages, start_idx + available_lines)
    visible_messages = messages[start_idx:end_idx]
    
    # Calculate highlighted message index (last visible message)
    highlighted_idx = len(messages) - scroll_position - 1
    
    # Draw messages from top to bottom (oldest to newest)
    current_line = 1  # Start at line 1 to account for top border
    for idx, msg in enumerate(visible_messages):
        try:
            formatted_msg = format_message(msg)
            
            # Check if this is the highlighted message
            is_highlighted = (start_idx + idx) == highlighted_idx
            
            if is_highlighted:
                win.attron(curses.A_REVERSE)
            
            # Handle message wrapping
            lines = formatted_msg.split('\n')
            for line in lines:
                if len(line) > max_x - 2:
                    wrapped_lines = [line[i:i+max_x-3] 
                                   for i in range(0, len(line), max_x-3)]
                    for wrapped in wrapped_lines:
                        if current_line < max_y - 1:
                            win.addstr(current_line, 1, wrapped.encode('utf-8'))
                            current_line += 1
                else:
                    if current_line < max_y - 1:
                        win.addstr(current_line, 1, line.encode('utf-8'))
                        current_line += 1
            
            if is_highlighted:
                win.attroff(curses.A_REVERSE)
                
        except curses.error:
            pass
    
    win.refresh()

def draw_input_box(win, current_input):
    win.erase()
    win.box()
    max_y, max_x = win.getmaxyx()
    
    # Show input prompt
    prompt = "Message: "
    win.addstr(1, 1, prompt)
    
    # Handle long input with scrolling
    input_width = max_x - len(prompt) - 3
    if len(current_input) > input_width:
        visible_input = current_input[-input_width:]
    else:
        visible_input = current_input
    
    try:
        # Ensure proper UTF-8 encoding for display
        visible_input = visible_input.encode('utf-8').decode('utf-8')
        win.addstr(1, len(prompt) + 1, visible_input.encode('utf-8'))
    except curses.error:
        pass
    win.refresh()

def draw_loading_popup(stdscr, message):
    """Draw a centered popup with loading message"""
    height, width = stdscr.getmaxyx()
    
    # Create a small popup
    popup_height = 3
    popup_width = min(width - 4, len(message) + 4)  # Width based on message length
    popup_y = (height - popup_height) // 2
    popup_x = (width - popup_width) // 2
    
    popup = curses.newwin(popup_height, popup_width, popup_y, popup_x)
    popup.box()
    
    try:
        # Center the message in the popup
        y = popup_height // 2
        x = (popup_width - len(message)) // 2
        popup.addstr(y, x, message)
    except curses.error:
        pass
    
    popup.refresh()
    return popup  # Return the popup window so we can keep it around

def draw_status_line(win, ui_state):
    """Draw status line at the bottom of the window"""
    max_y, max_x = win.getmaxyx()
    status = f" Mode: {'Favorites' if ui_state.display_mode == 2 else 'All'} | "
    status += f"Focus: {'Messages' if ui_state.input_focused else 'Chats'} | "
    status += "? for help"
    
    try:
        # Draw status line one row up from bottom
        win.addstr(max_y - 2, 0, status.ljust(max_x)[:max_x], curses.A_REVERSE)
        # Add empty line below for spacing
        win.addstr(max_y - 1, 0, " " * max_x)
    except curses.error:
        pass

class UIState:
    def __init__(self):
        self.display_mode = 1  # 1 for All, 2 for Favorites
        self.favorites = set()
        self.filtered_chat_idx = 0
        self.input_focused = False  # Track input focus in UIState
        self.load_favorites()
    
    def load_favorites(self):
        try:
            if os.path.exists('favorites.json'):
                with open('favorites.json', 'r') as f:
                    self.favorites = set(json.load(f))
                logger.info(f"Loaded {len(self.favorites)} favorites")
        except Exception as e:
            logger.error(f"Failed to load favorites: {e}")
            self.favorites = set()
    
    def save_favorites(self):
        try:
            with open('favorites.json', 'w') as f:
                json.dump(list(self.favorites), f)
            logger.info(f"Saved {len(self.favorites)} favorites")
        except Exception as e:
            logger.error(f"Failed to save favorites: {e}")
    
    def add_favorite(self, chat_id):
        if chat_id not in self.favorites:
            self.favorites.add(chat_id)
            self.save_favorites()
            return True
        return False
    
    def remove_favorite(self, chat_id):
        if chat_id in self.favorites:
            self.favorites.discard(chat_id)
            self.save_favorites()
            return True
        return False
    
    def filter_chats(self, chats):
        filtered = []
        if self.display_mode == 2:  # Favorites
            filtered = [c for c in chats if c['id'] in self.favorites]
        else:  # Mode 1: Show all
            filtered = chats
        
        # Adjust current index if needed
        if filtered and self.filtered_chat_idx >= len(filtered):
            self.filtered_chat_idx = len(filtered) - 1
        elif not filtered:
            self.filtered_chat_idx = 0
            
        return filtered

def show_help_popup(stdscr):
    height, width = stdscr.getmaxyx()
    # Create a centered popup
    popup_height = 12
    popup_width = 50
    popup_y = (height - popup_height) // 2
    popup_x = (width - popup_width) // 2
    
    popup = curses.newwin(popup_height, popup_width, popup_y, popup_x)
    popup.box()
    popup.addstr(0, 2, " Keyboard Shortcuts ")
    
    shortcuts = [
        ("Shift + ↑/↓", "Navigate between chats"),
        ("Shift + [/]", "Toggle All/Favorites mode"),
        ("Shift + =", "Add to favorites"),
        ("Shift + -", "Remove from favorites"),
        ("↑/↓", "Scroll messages"),
        ("Tab", "Toggle input focus"),
        ("Esc", "Clear input"),
        ("Enter", "Send message"),
        ("Ctrl + C", "Exit application"),
        ("?", "Show this help"),
    ]
    
    for idx, (key, desc) in enumerate(shortcuts, 1):
        popup.addstr(idx, 2, f"{key:<15} - {desc}")
    
    popup.addstr(popup_height - 2, 2, "Press any key to close")
    popup.refresh()
    popup.getch()

def parse_ansi(text):
    """Parse ANSI escape codes and return list of (color_code, segment) tuples"""
    ansi_escape = re.compile(r'\033\[(\d+(?:;\d+)*)m')
    segments = []
    last_end = 0
    current_code = 0
    
    for match in ansi_escape.finditer(text):
        start, end = match.span()
        # Add text before the escape sequence
        if start > last_end:
            segments.append((current_code, text[last_end:start]))
        
        # Parse color code (handle both simple and complex codes)
        code_str = match.group(1)
        if ';' in code_str and code_str.startswith('38;5;'):
            # Handle 256-color codes
            current_code = int(code_str.split(';')[-1])
        else:
            current_code = int(code_str)
        
        last_end = end
    
    # Add remaining text
    if last_end < len(text):
        segments.append((current_code, text[last_end:]))
    return segments

def display_ansi_text(window, text, color_map, y, x):
    """Draw text with ANSI colors at specified position"""
    segments = parse_ansi(text)
    current_x = x
    
    for code, segment in segments:
        if code == 0:  # Reset
            window.attrset(0)
        elif code in color_map:
            window.attron(curses.color_pair(color_map[code]))
        
        try:
            window.addstr(y, current_x, segment)
            current_x += len(segment)
        except curses.error:
            pass  # Handle edge of window gracefully

def show_message_preview(stdscr, message, telegram_worker):
    """Show popup with message content"""
    logger.info("Opening message preview")
    height, width = stdscr.getmaxyx()
    popup_height = min(height - 8, 30)
    popup_width = min(width - 4, 80)
    
    logger.info(f"Popup dimensions: {popup_width}x{popup_height}")
    
    start_y = (height - popup_height) // 2
    start_x = (width - popup_width) // 2
    
    popup = curses.newwin(popup_height, popup_width, start_y, start_x)
    popup.box()
    
    try:
        current_line = 1
        # Draw header
        timestamp = message['timestamp']
        today = datetime.now().date()
        msg_date = timestamp.date()
        
        time_str = timestamp.strftime('%H:%M')
        if msg_date == today:
            date_str = "Today"
        elif msg_date == today - timedelta(days=1):
            date_str = "Yesterday"
        else:
            date_str = timestamp.strftime('%d.%m.%Y')
            
        timestamp_str = f"[{time_str} {date_str}]"
        header = f"{timestamp_str} {message['from_user']}:"
        
        popup.addstr(current_line, 2, header[:popup_width-4])
        current_line += 1
        
        # Handle photo content
        if message.get('has_photo'):
            logger.info("Message has photo, fetching...")
            photo_data = asyncio.run_coroutine_threadsafe(
                telegram_worker.get_message_photo(message),
                telegram_worker.loop
            ).result()
            
            if photo_data:
                logger.info("Got photo data, creating ASCII art")
                image = photo_data['data']
                # Calculate available space for the image
                available_height = popup_height - current_line - 3  # Leave space for caption
                ascii_art = telegram_worker._create_ascii_art(
                    image, 
                    popup_width-4,
                    available_height
                )
                
                # Draw ASCII art
                art_lines = ascii_art.split('\n')
                logger.info(f"ASCII art has {len(art_lines)} lines")
                for line in art_lines:
                    if current_line < popup_height - 1:
                        popup.addstr(current_line, 2, line)
                        current_line += 1
                
                # Draw caption if exists
                if photo_data.get('caption'):
                    current_line += 1
                    popup.addstr(current_line, 2, f"Caption: {photo_data['caption']}")
                    current_line += 1
        
        # Draw message text
        text = message['text'] or '[empty message]'
        if text and text != '📷 Photo':
            current_line += 1
            text_lines = text.split('\n')
            logger.info(f"Message text has {len(text_lines)} lines")
            for line in text_lines:
                if len(line) > popup_width - 4:
                    wrapped = [line[i:i+popup_width-4] 
                             for i in range(0, len(line), popup_width-4)]
                    for wrapped_line in wrapped:
                        if current_line < popup_height - 1:
                            popup.addstr(current_line, 2, wrapped_line)
                            current_line += 1
                else:
                    if current_line < popup_height - 1:
                        popup.addstr(current_line, 2, line)
                        current_line += 1
                        
    except Exception as e:
        logger.error(f"Error in message preview: {e}", exc_info=True)
    
    popup.refresh()
    
    # Wait for ESC key
    while True:
        try:
            key = stdscr.getch()
            if key == 27:  # ESC
                break
        except curses.error:
            continue

def main(stdscr):
    logger.info("Starting UI...")
    # Remove the locale setup here since we did it at the top
    # Enable proper Unicode support in curses
    curses.meta(1)  # Enable 8-bit input
    stdscr.encoding = 'utf-8'
    
    # Initialize colors
    curses.start_color()
    curses.use_default_colors()
    for i in range(curses.COLORS):
        curses.init_pair(i + 1, i, -1)
    
    # Ensure we have our specific colors defined
    if curses.COLORS >= 256:
        curses.init_pair(4, 12, -1)  # Bright blue for unread messages
        curses.init_pair(5, 13, -1)  # Bright purple for mentions
        curses.init_pair(7, 7, -1)   # White for normal
        curses.init_pair(8, 8, -1)   # Gray for muted

    curses.curs_set(1)
    stdscr.nodelay(True)  # make getch non-blocking
    
    # Enable mouse events
    curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)

    # Create windows with adjusted height for status bar
    height, width = stdscr.getmaxyx()
    sidebar_width = width // 4
    chat_area_width = width - sidebar_width

    # Adjust window heights to account for status line
    sidebar_win = curses.newwin(height - 2, sidebar_width, 0, 0)  # -2 for status line
    chat_header_win = curses.newwin(3, chat_area_width, 0, sidebar_width)
    chat_messages_win = curses.newwin(height - 8, chat_area_width, 3, sidebar_width)  # -8 for header, input, and status
    input_win = curses.newwin(3, chat_area_width, height - 5, sidebar_width)  # -5 to be above status line

    # Initialize state
    chats = []
    messages_by_chat = {}
    current_input = ""
    scroll_position = 0
    ui_state = UIState()

    # Start the Telegram worker thread
    telegram_worker = run_telegram_worker()

    # Add state for chat loading
    is_loading_chats = False

    # Add state for message loading
    SCROLL_THRESHOLD = 10  # Load more messages when this close to the top

    # Add loading popup tracking
    loading_popup = None

    def get_current_chat():
        """Get currently selected chat info"""
        filtered_chats = ui_state.filter_chats(chats)
        if filtered_chats and 0 <= ui_state.filtered_chat_idx < len(filtered_chats):
            return filtered_chats[ui_state.filtered_chat_idx]
        return None

    def get_current_chat_id():
        """Get currently selected chat ID"""
        current_chat = get_current_chat()
        if current_chat:
            return current_chat['id']
        return None

    # Show initial loading message using the new popup
    loading_popup = draw_loading_popup(stdscr, "Connecting to Telegram...")

    try:
        while True:
            try:
                event = ui_queue.get_nowait()
                logger.debug(f"Received event: {event['type']}")
                
                try:
                    if event["type"] == "loading_progress":
                        # Update existing loading popup
                        loading_popup = draw_loading_popup(stdscr, event["message"])
                    elif event["type"] == "new_message":
                        logger.info(f"New message in chat {event['chat_id']}")
                        chat_id = event.get("chat_id")
                        message = event.get("message")
                        if chat_id is not None and message is not None:
                            if chat_id not in messages_by_chat:
                                messages_by_chat[chat_id] = []
                            # Add the message to our local cache
                            messages_by_chat[chat_id].append(message)
                            # Auto-scroll to bottom for new messages in current chat
                            if chat_id == get_current_chat_id():
                                scroll_position = 0
                        else:
                            logger.error("Invalid message event format")
                    
                    elif event["type"] == "error":
                        logger.error(f"Error event received: {event.get('message', 'Unknown error')}")
                        # Show errors in current chat
                        chat_id = get_current_chat_id()
                        if chat_id:
                            if chat_id not in messages_by_chat:
                                messages_by_chat[chat_id] = []
                            messages_by_chat[chat_id].append({
                                'text': f"Error: {event.get('message', 'Unknown error')}",
                                'timestamp': datetime.now(),
                                'from_user': 'System',
                                'is_outgoing': False
                            })
                    
                    elif event["type"] == "chats_loaded":
                        # Clear loading popup when done
                        if loading_popup:
                            loading_popup = None
                            stdscr.touchwin()  # Force full redraw
                            stdscr.refresh()
                        chats = event["chats"]
                        logger.info(f"Loaded {len(chats)} chats")
                        if chats:
                            telegram_worker.set_current_chat(chats[0]['id'])
                    
                    elif event["type"] == "chat_history_loaded":
                        chat_id = event.get("chat_id")
                        messages = event.get("messages", [])
                        is_older_messages = event.get("is_older_messages", False)
                        
                        if chat_id is not None:
                            logger.info(f"Loaded history for chat {chat_id}: {len(messages)} messages")
                            messages_by_chat[chat_id] = messages
                            
                            # Adjust scroll position when loading older messages
                            if is_older_messages:
                                scroll_position = max(0, scroll_position + len(messages))
                        else:
                            logger.error("Invalid chat history event format")
                    
                except Exception as e:
                    logger.error(f"Error processing event {event['type']}: {str(e)}", exc_info=True)
                    
            except queue.Empty:
                pass
            except Exception as e:
                logger.error(f"Error in main event loop: {str(e)}", exc_info=True)

            # Get current chat info
            current_chat = get_current_chat()
            current_messages = []
            current_chat_title = "No chat selected"
            
            if current_chat:
                current_chat_title = current_chat['title']
                current_messages = messages_by_chat.get(current_chat['id'], [])

            # Redraw all windows
            filtered_chats = ui_state.filter_chats(chats)
            draw_sidebar(sidebar_win, filtered_chats, ui_state.filtered_chat_idx, ui_state, telegram_worker)
            draw_chat_header(chat_header_win, current_chat_title)
            draw_messages(chat_messages_win, current_messages, scroll_position)
            draw_input_box(input_win, current_input)
            draw_status_line(stdscr, ui_state)  # Add status line

            # Handle user input
            try:
                key = stdscr.getch()
            except KeyboardInterrupt:
                break

            if key == curses.ERR:
                time.sleep(0.1)
                continue

            # Handle key presses
            if key == ord('?'):  # Show help
                show_help_popup(stdscr)
            elif key == 9:  # Tab key - toggle input mode
                ui_state.input_focused = not ui_state.input_focused
                curses.curs_set(1 if ui_state.input_focused else 0)
            elif key == 27:  # ESC key - only clear input
                if ui_state.input_focused:
                    current_input = ""
            elif key == 10:  # Enter key
                if ui_state.input_focused:
                    if current_input.strip() and current_chat:
                        try:
                            telegram_worker.send_message(current_input)
                            current_input = ""
                            scroll_position = 0
                        except Exception as e:
                            logger.error(f"Failed to send message: {e}")
                elif key == 27:  # Alt/Option key sequence starts with ESC
                    # Wait briefly for next character
                    stdscr.nodelay(False)  # Temporarily make getch blocking
                    next_key = stdscr.getch()
                    stdscr.nodelay(True)  # Restore non-blocking
                    
                    if next_key == 10:  # Alt/Option + Enter
                        if current_messages:
                            cursor_pos = len(current_messages) - scroll_position - 1
                            if 0 <= cursor_pos < len(current_messages):
                                show_message_preview(stdscr, current_messages[cursor_pos], telegram_worker)
            elif key == ord('§'):  # Section symbol key
                if current_messages:
                    cursor_pos = len(current_messages) - scroll_position - 1
                    if 0 <= cursor_pos < len(current_messages):
                        show_message_preview(stdscr, current_messages[cursor_pos], telegram_worker)
            elif ui_state.input_focused:
                # Handle input mode keys
                if key == curses.KEY_BACKSPACE or key == 127 or key == 263:
                    current_input = current_input[:-1]
                elif key in (10, 13):  # Enter key
                    if current_input.strip() and current_chat:
                        try:
                            telegram_worker.send_message(current_input)
                            current_input = ""
                            scroll_position = 0
                        except Exception as e:
                            logger.error(f"Failed to send message: {e}")
                elif key > 0:
                    try:
                        # Handle multi-byte characters
                        if key <= 127:
                            char = chr(key)
                            if char.isprintable():
                                current_input += char
                        else:
                            # Handle multi-byte input
                            buf = []
                            buf.append(key)
                            while True:
                                try:
                                    key = stdscr.getch()
                                    if key == -1:
                                        break
                                    buf.append(key)
                                    # Try to decode the buffer
                                    char = bytes(buf).decode('utf-8')
                                    current_input += char
                                    break
                                except UnicodeDecodeError:
                                    continue
                                except Exception:
                                    break
                    except Exception as e:
                        logger.error(f"Input error: {e}")
            else:
                # Handle navigation mode keys
                if key == ord('{') or key == ord('}'):  # Mode switch
                    ui_state.display_mode = 3 - ui_state.display_mode
                    logger.info(f"Switched to {'Favorites' if ui_state.display_mode == 2 else 'All'} mode")
                elif key == ord('+'):  # Add to favorites
                    chat_id = get_current_chat_id()
                    if chat_id and ui_state.add_favorite(chat_id):
                        logger.info(f"Added chat {chat_id} to favorites")
                elif key == ord('_'):  # Remove from favorites
                    chat_id = get_current_chat_id()
                    if chat_id and ui_state.remove_favorite(chat_id):
                        logger.info(f"Removed chat {chat_id} from favorites")
                elif key == 337:  # Shift + Up
                    filtered_chats = ui_state.filter_chats(chats)
                    if filtered_chats:
                        prev_idx = ui_state.filtered_chat_idx
                        ui_state.filtered_chat_idx = (ui_state.filtered_chat_idx - 1) % len(filtered_chats)
                        if prev_idx != ui_state.filtered_chat_idx:
                            chat_id = filtered_chats[ui_state.filtered_chat_idx]['id']
                            telegram_worker.set_current_chat(chat_id)
                            scroll_position = 0
                            logger.debug(f"Navigated to chat: {chat_id}")
                elif key == 336:  # Shift + Down
                    filtered_chats = ui_state.filter_chats(chats)
                    if filtered_chats:
                        prev_idx = ui_state.filtered_chat_idx
                        ui_state.filtered_chat_idx = (ui_state.filtered_chat_idx + 1) % len(filtered_chats)
                        if prev_idx != ui_state.filtered_chat_idx:
                            chat_id = filtered_chats[ui_state.filtered_chat_idx]['id']
                            telegram_worker.set_current_chat(chat_id)
                            scroll_position = 0
                            logger.debug(f"Navigated to chat: {chat_id}")
                elif key == curses.KEY_UP:  # Up arrow - always scroll messages up to see older messages
                    if len(current_messages) > 0:
                        # Calculate maximum scroll position
                        max_scroll = max(0, len(current_messages) - (height - 8))
                        new_scroll = min(scroll_position + 1, max_scroll)
                        
                        # Check if we need to load more messages
                        if new_scroll >= max_scroll - SCROLL_THRESHOLD:
                            chat_id = get_current_chat_id()
                            if chat_id and current_messages:
                                oldest_message_id = current_messages[0]['id']
                                asyncio.run_coroutine_threadsafe(
                                    telegram_worker.load_chat_history(
                                        chat_id, 
                                        limit=200,  # Increased from 50 to 200
                                        before_message_id=oldest_message_id
                                    ),
                                    telegram_worker.loop
                                )
                        
                        scroll_position = new_scroll
                elif key == curses.KEY_DOWN:  # Down arrow - always scroll messages down to see newer messages
                    if len(current_messages) > 0:
                        scroll_position = max(0, scroll_position - 1)
                elif key == curses.KEY_MOUSE:  # Mouse scroll - always controls message history
                    try:
                        _, _, _, _, ms_id = curses.getmouse()
                        if ms_id & 0x40000:  # Scroll up - show older messages (wheel up)
                            if len(current_messages) > 0:
                                # Calculate maximum scroll position
                                max_scroll = max(0, len(current_messages) - (height - 8))
                                new_scroll = min(scroll_position + 3, max_scroll)
                                
                                # Check if we need to load more messages
                                if new_scroll >= max_scroll - SCROLL_THRESHOLD:
                                    chat_id = get_current_chat_id()
                                    if chat_id and current_messages:
                                        oldest_message_id = current_messages[0]['id']
                                        asyncio.run_coroutine_threadsafe(
                                            telegram_worker.load_chat_history(
                                                chat_id, 
                                                limit=200,  # Increased from 50 to 200
                                                before_message_id=oldest_message_id
                                            ),
                                            telegram_worker.loop
                                        )
                                
                                scroll_position = new_scroll
                        elif ms_id & 0x80000:  # Scroll down - show newer messages (wheel down)
                            if len(current_messages) > 0:
                                scroll_position = max(0, scroll_position - 3)
                    except curses.error:
                        pass

            # Update cursor visibility based on input focus
            curses.curs_set(1 if ui_state.input_focused else 0)

    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt, shutting down...")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
    finally:
        # Clean shutdown
        try:
            logger.info("Stopping telegram worker...")
            if telegram_worker:
                telegram_worker.stop()
            logger.info("Cleanup complete")
        except Exception as e:
            logger.error(f"Error during cleanup: {e}", exc_info=True)

if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
