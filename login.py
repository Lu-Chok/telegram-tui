from pyrogram import Client

from config import PHONE, API_ID, API_HASH
app = Client(
    name=PHONE,
    phone_number=PHONE,
    api_id=API_ID,
    api_hash=API_HASH
)

app.start()
app.get_me()
print("Successfully logged in")