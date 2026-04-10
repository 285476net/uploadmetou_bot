import telebot
import os
import re
import threading
from flask import Flask
from threading import Thread, Timer
from telebot.types import BotCommand
from telebot.types import BotCommand, InlineKeyboardMarkup, InlineKeyboardButton
import time
from pymongo import MongoClient
import requests

# ==========================================
# CONFIGURATION & DATABASE CONNECTION
# ==========================================
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID'))
MONGO_URL = os.getenv('MONGO_URL')

client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
db = client['telegram_bot_db']
config_col = db['settings']    
backup_logs = db['backup_logs']

bot = telebot.TeleBot(BOT_TOKEN)

# ==========================================
# DATABASE HELPER FUNCTIONS
# ==========================================

def get_user_config(user_id):
    """User တစ်ဦးချင်းစီအတွက် Setting ခွဲထုတ်ရန် (Multi-Channel Support)"""
    data = config_col.find_one({"_id": str(user_id)})
    if not data:
        new_data = {
            "_id": str(user_id),
            "channels": {}, # Dictionary အနေဖြင့် သိမ်းပါမည်
            "authorized_users": [ADMIN_ID],
        }
        config_col.insert_one(new_data)
        return new_data
    
    # ⚠️ အရင်က channel_id တစ်ခုတည်းသိမ်းခဲ့တဲ့ User ဟောင်းတွေအတွက် Auto ပြောင်းပေးမယ့်စနစ်
    if "channels" not in data:
        old_channel = data.get("channel_id")
        old_caption = data.get("custom_caption", "")
        channels_dict = {}
        if old_channel:
            channels_dict[old_channel] = {"name": "My Channel", "caption": old_caption}
        config_col.update_one({"_id": str(user_id)}, {"$set": {"channels": channels_dict}})
        data["channels"] = channels_dict

    return data
    
def update_user_setting(user_id, field, value):
    config_col.update_one({"_id": str(user_id)}, {"$set": {field: value}}, upsert=True)

authorized_cache = {}
cache_lock = threading.Lock()

def load_authorized_users():
    global authorized_cache
    admin_cfg = get_user_config(ADMIN_ID)
    users = admin_cfg.get('authorized_users', {})
    
    # ⚠️ အရင်က List ပုံစံနဲ့ သိမ်းထားတဲ့ Data အဟောင်းတွေရှိခဲ့ရင် Dictionary ပြောင်းပေးမယ့်စနစ်
    if isinstance(users, list):
        users_dict = {}
        for uid in users:
            users_dict[str(uid)] = None 
        config_col.update_one({"_id": str(ADMIN_ID)}, {"$set": {"authorized_users": users_dict}})
        users = users_dict

    with cache_lock: # တစ်ပြိုင်နက်တည်း Write လုပ်ခြင်းကို ကာကွယ်ရန်
        authorized_cache = {int(k): v for k, v in users.items()}
        authorized_cache[ADMIN_ID] = None 
    print(f"✅ Loaded {len(authorized_cache)} authorized users to cache.")

# ==========================================
# BACKUP LOGIC (WITH USER_ID)
# ==========================================

def is_already_backed_up(user_id, source_chat_id, target_chat_id, message_id):
    return backup_logs.find_one({
        "user_id": str(user_id),
        "source_chat": str(source_chat_id), 
        "target_chat": str(target_chat_id), 
        "msg_id": message_id
    })

def log_backup(user_id, source_chat_id, target_chat_id, message_id):
    backup_logs.insert_one({
        "user_id": str(user_id),
        "source_chat": str(source_chat_id), 
        "target_chat": str(target_chat_id), 
        "msg_id": message_id, 
        "timestamp": time.time()
    })

@bot.message_handler(commands=['backup'])
def start_backup(message):
    user_id = message.from_user.id
    if not is_authorized(user_id): return

    try:
        parts = message.text.split()
        if len(parts) < 5:
            bot.reply_to(message, "⚠️ Usage: `/backup [SourceID] [TargetID] [StartID] [EndID]`")
            return

        source_chat = parts[1]
        target_chat = parts[2]
        start_id = int(parts[3])
        end_id = int(parts[4])

        status_msg = bot.reply_to(message, "🚀 Backup Process စတင်ပါပြီ...")
        
        success_count = 0
        fail_count = 0
        skip_count = 0
        failed_ids = []

        # 🌟 Multi-Channel ထဲက Target Channel ၏ Caption ကို ဆွဲထုတ်ခြင်း
        cfg = get_user_config(user_id)
        channels = cfg.get('channels', {})
        
        custom_caption = None
        if target_chat in channels and channels[target_chat].get('caption'):
            custom_caption = channels[target_chat]['caption']

        for msg_id in range(start_id, end_id + 1):
            if is_already_backed_up(user_id, source_chat, target_chat, msg_id):
                skip_count += 1
                continue

            success = False
            for attempt in range(3):
                try:
                    if custom_caption:
                        # Caption သတ်မှတ်ထားလျှင် အသစ်ဖြင့် အစားထိုးမည်
                        bot.copy_message(
                            chat_id=target_chat,
                            from_chat_id=source_chat,
                            message_id=msg_id,
                            caption=custom_caption
                        )
                    else:
                        # မသတ်မှတ်ထားလျှင် မူရင်းအတိုင်း အတိအကျ ကူးမည်
                        bot.copy_message(
                            chat_id=target_chat,
                            from_chat_id=source_chat,
                            message_id=msg_id
                        )
                        
                    log_backup(user_id, source_chat, target_chat, msg_id)
                    success_count += 1
                    success = True
                    break 
                except Exception as e:
                    if attempt < 2:
                        time.sleep(5) 
                    else:
                        fail_count += 1
                        failed_ids.append(str(msg_id))

            if success:
                time.sleep(2.5) # Telegram Limit မထိအောင် နားခြင်း
            
            if (success_count + skip_count + fail_count) % 5 == 0:
                try:
                    bot.edit_message_text(
                        f"🔄 Progress: {msg_id - start_id + 1}/{end_id - start_id + 1}\n✅ Done: {success_count} | ⏭ Skip: {skip_count}",
                        chat_id=message.chat.id,
                        message_id=status_msg.message_id
                    )
                except: pass

        final_text = (
            f"📊 **Backup Result**\n"
            f"✅ Success: {success_count}\n"
            f"⏭ Skipped (Dup): {skip_count}\n"
            f"❌ Failed: {fail_count}"
        )
        bot.send_message(message.chat.id, final_text, parse_mode="Markdown")
        
        if failed_ids:
            bot.send_message(message.chat.id, f"⚠️ **Error IDs:** `{', '.join(failed_ids[:30])}`")

    except Exception as e:
        bot.reply_to(message, f"❌ Backup Error: {e}")
        
@bot.message_handler(commands=['clearlogs'])
def clear_backup_logs(message):
    user_id = message.from_user.id
    parts = message.text.split()
    
    # Admin က ID ပါတွဲပို့ရင် အဲ့ဒီ user ရဲ့ log ကိုပဲဖျက်မယ်
    if user_id == ADMIN_ID:
        if len(parts) == 2:
            target_uid = parts[1]
            backup_logs.delete_many({"user_id": str(target_uid)})
            bot.reply_to(message, f"🗑 Backup logs for User `{target_uid}` cleared.")
        else:
            # ID မပါရင် မူရင်းအတိုင်း log အားလုံးကို ဖျက်မယ်
            backup_logs.delete_many({})
            bot.reply_to(message, "🗑 All backup logs have been cleared.")
    elif is_authorized(user_id):
        # User ဆိုရင် ကိုယ့် log ကိုပဲ ဖျက်မယ်
        backup_logs.delete_many({"user_id": str(user_id)})
        bot.reply_to(message, "🗑 Your backup logs have been cleared.")

# WEB SERVER & SELF PING
# ==========================================
app = Flask('')

@app.route('/')
def home():
    return "Bot is Running! 🤖"

def run_http():
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 8080)))

def ping_self():
    """Bot မအိပ်သွားအောင် 10 မိနစ်တစ်ခါ ကိုယ့် URL ကိုယ်ပြန် Ping မယ့် Function"""
    while True:
        time.sleep(600)  # 600 စက္ကန့် (၁၀ မိနစ်) စောင့်မယ်
        try:
            # Render ကပေးတဲ့ URL ကို အလိုအလျောက်ယူပါမယ်
            base_url = os.getenv('RENDER_EXTERNAL_URL') 
            
            # အကယ်၍ Render မဟုတ်ဘဲ တခြားနေရာတင်ရင် ကိုယ့် App URL ကို " " ကြားထဲထည့်ပေးပါ
            if not base_url:
                base_url = "YOUR_APP_URL_HERE" 

            if base_url and "http" in base_url:
                r = requests.get(base_url)
                print(f"🔄 Self Ping Status: {r.status_code}")
        except Exception as e:
            print(f"⚠️ Ping Error: {e}")

def check_expired_users():
    while True:
        time.sleep(60)
        current_time = time.time()
        expired_users = []
        
        # ၁။ သက်တမ်းကုန်ဆုံးသူများကို ရှာဖွေခြင်း (Read Lock)
        with cache_lock:
            for uid, expiry in authorized_cache.items():
                if expiry is not None and current_time > expiry:
                    expired_users.append(uid)
        
        # ၂။ ရှာတွေ့သူများကို ဖယ်ထုတ်ခြင်း (Write Lock)
        for uid in expired_users:
            with cache_lock:
                if uid in authorized_cache:
                    del authorized_cache[uid]
            
            # Database Update နှင့် Message ပို့ခြင်း (Lock အပြင်ဘက်မှာ လုပ်ရပါမယ်)
            config_col.update_one(
                {"_id": str(ADMIN_ID)}, 
                {"$unset": {f"authorized_users.{uid}": ""}}
            )
            
            try:
                bot.send_message(
                    uid, 
                    "⚠️ **အသိပေးချက်**\n\nသင်၏ Bot အသုံးပြုခွင့် သက်တမ်းကုန်ဆုံးသွားပါပြီ။", 
                    parse_mode="Markdown"
                )
                bot.send_message(ADMIN_ID, f"🔄 User ID `{uid}` ကို Auto Unauth လုပ်လိုက်ပါပြီ။")
            except Exception as e:
                print(f"Error notifying {uid}: {e}")
                
def keep_alive():
    # Server Run ဖို့ Thread
    t_server = Thread(target=run_http)
    t_server.start()
    
    # Ping လုပ်ဖို့ Thread
    t_ping = Thread(target=ping_self)
    t_ping.start()
    
    # Expiry Check လုပ်ဖို့ Thread (ယခုအသစ်ထည့်ထားသောစနစ်)
    t_expiry = Thread(target=check_expired_users)
    t_expiry.start()
# ==========================================
# ADMIN & AUTH COMMANDS (Original Flow)
# ==========================================

def is_authorized(user_id):
    with cache_lock: # ဖတ်နေတုန်း တခြား Thread က ပြင်လို့မရအောင် Lock ချထားမယ်
        if user_id not in authorized_cache:
            return False
        expiry = authorized_cache[user_id]
        if expiry is not None and time.time() > expiry:
            return False
        return True

# REFERRAL SYSTEM LOGIC (COIN SYSTEM)
# ==========================================
def process_referral(new_user_id, inviter_id, new_user_name):
    if new_user_id == inviter_id: return
    if not is_authorized(inviter_id): return

    new_user_data = get_user_config(new_user_id)
    if is_authorized(new_user_id) or new_user_data.get('invited_by'): return 
        
    # (က) ဘယ်သူဖိတ်လိုက်တယ်ဆိုတာ မှတ်မည်
    update_user_setting(new_user_id, "invited_by", inviter_id)
    
    # (ခ) ဖိတ်ခေါ်သူကို ရက်မပေးတော့ဘဲ 10 Coins 🪙 ပေးမည်
    config_col.update_one({"_id": str(inviter_id)}, {"$inc": {"coins": 10, "referral_count": 1}}, upsert=True)
    
    try:
        bot.send_message(
            inviter_id, 
            f"🎉 **ဂုဏ်ယူပါတယ်!**\n\n👤 {new_user_name} က သင့် Link မှတစ်ဆင့် ဝင်ရောက်လာတဲ့အတွက် **10 Coins 🪙** ရရှိပါပြီ။\n(50 Coins ပြည့်တိုင်း ၁ ရက် အခမဲ့သုံးစွဲခွင့်ရရှိပါမည်။)",
            parse_mode="Markdown"
        )
    except: pass
            
    new_user_expiry = time.time() + (1 * 86400)
    with cache_lock:
        authorized_cache[new_user_id] = new_user_expiry
    config_col.update_one(
        {"_id": str(ADMIN_ID)}, 
        {"$set": {f"authorized_users.{new_user_id}": new_user_expiry}},
        upsert=True
    )
    try:
        bot.send_message(
            new_user_id,
            "🎁 **Welcome Bonus!**\n\nသူငယ်ချင်း၏ ဖိတ်ခေါ်မှုဖြင့် ဝင်ရောက်လာတဲ့အတွက် အခမဲ့ **၁ ရက်** စမ်းသပ်အသုံးပြုခွင့် ရရှိပါသည်။ ဆက်လက်အသုံးပြုလိုပါက Admin ထံ ဆက်သွယ်ပါ။",
            parse_mode="Markdown"
        )
    except: pass

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    first_name = message.from_user.first_name
    
    # --- Referral Link မှ ဝင်လာခြင်းရှိမရှိ စစ်ဆေးခြင်း ---
    parts = message.text.split()
    if len(parts) > 1 and parts[1].startswith('ref_'):
        try:
            inviter_id = int(parts[1].replace('ref_', ''))
            process_referral(user_id, inviter_id, first_name)
        except ValueError:
            pass 
    # ----------------------------------------------------

    welcome_text = f"Hello, {first_name}!\n\n"
    welcome_text += "I am a bot designed to easily copy, manage, and back up files across Telegram Channels and Groups.\n\n"
    
    if is_authorized(user_id):
        welcome_text += "✅You are authorized to use this bot.\n\n"
        welcome_text += "You can tap the Menu button next to the text input area to explore all available commands.\n\n"
    else:
        welcome_text += "⚠️ You don't have permission to use this bot yet.\n\n"
        welcome_text += f"If you would like to get access, please contact the Admin @moviestoreadmin and send them your User ID: {user_id}\n\n"
        welcome_text += "Powered by @moviesbydatahouse"
        
    bot.reply_to(message, welcome_text, parse_mode="Markdown")

@bot.message_handler(commands=['invite', 'referral'])
def send_invite_link(message):
    user_id = message.from_user.id
    if not is_authorized(user_id):
        bot.reply_to(message, "⚠️ ဤ Command ကိုအသုံးပြုရန် Bot ကို အသုံးပြုခွင့် ရရှိထားရန် လိုအပ်ပါသည်။")
        return
        
    bot_info = bot.get_me()
    bot_username = bot_info.username
    invite_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
    
    user_data = get_user_config(user_id)
    ref_count = user_data.get('referral_count', 0)
    coins = user_data.get('coins', 0) # လက်ကျန် Coin ကို ဆွဲထုတ်ခြင်း
    
    text = (
        f"🎁 **သူငယ်ချင်းကို ဖိတ်ခေါ်ပြီး Coins 🪙 စုဆောင်းပါ**\n\n"
        f"သင့် Link မှတစ်ဆင့် သူငယ်ချင်းတစ်ယောက် ဝင်ရောက်တိုင်း **10 Coins 🪙** ရရှိပါမည်။\n"
        f"Coins 50 ပြည့်တိုင်း အခမဲ့ ၁ ရက် ပြန်လည်လဲလှယ်နိုင်ပါသည်။ (လဲလှယ်ရန် `/redeem` ကိုနှိပ်ပါ)\n\n"
        f"🔗 **သင့်ရဲ့ Invite Link:**\n`{invite_link}`\n\n"
        f"👥 ဖိတ်ခေါ်ထားသူ: **{ref_count}** ယောက်\n"
        f"🪙 သင့်လက်ကျန် Coins: **{coins} Coins**"
    )
    bot.reply_to(message, text, parse_mode="Markdown")

@bot.message_handler(commands=['redeem'])
def redeem_coins(message):
    user_id = message.from_user.id
    if not is_authorized(user_id): return
    
    user_data = get_user_config(user_id)
    coins = user_data.get('coins', 0)
    cost_per_day = 50 # ၁ ရက်စာအတွက် လိုအပ်သော Coin အရေအတွက်
    
    if coins < cost_per_day:
        bot.reply_to(message, f"⚠️ သင့်တွင် Coins အလုံအလောက်မရှိပါ။\n\n၁ ရက် လဲလှယ်ရန် အနည်းဆုံး **{cost_per_day} Coins 🪙** လိုအပ်ပါသည်။\nလက်ကျန်: **{coins} Coins**")
        return
        
    # ဘယ်နှရက် လဲလို့ရမလဲ တွက်ချက်ခြင်း
    days_to_add = coins // cost_per_day
    remaining_coins = coins % cost_per_day
    
    # Database တွင် Coin လျှော့ခြင်း
    config_col.update_one({"_id": str(user_id)}, {"$set": {"coins": remaining_coins}})
    
    # သက်တမ်းကိုပေါင်းထည့်ခြင်း
    current_expiry = authorized_cache.get(user_id, time.time())
    if current_expiry is None or current_expiry < time.time():
        current_expiry = time.time()
        
    new_expiry = current_expiry + (days_to_add * 86400)
    config_col.update_one(
        {"_id": str(ADMIN_ID)}, 
        {"$set": {f"authorized_users.{user_id}": new_expiry}}
    )

    with cache_lock:
        authorized_cache[user_id] = new_expiry
    
    bot.reply_to(message, f"✅ **အောင်မြင်ပါသည်။**\n\nCoins 🪙 ({days_to_add * cost_per_day}) ကို အသုံးပြုပြီး **{days_to_add} ရက်** စာ သက်တမ်းတိုးလိုက်ပါသည်။\n\n🪙 လက်ကျန် **{remaining_coins} Coins**")

# ==========================================
# ADMIN BROADCAST SYSTEM (ALL MEDIA SUPPORTED)
# ==========================================

@bot.message_handler(commands=['broadcast'])
def broadcast_command(message):
    if message.from_user.id != ADMIN_ID: return
    
    msg = bot.reply_to(
        message, 
        "📢 **Broadcast စနစ်**\n\nပေးပို့လိုသော စာသား၊ ပုံ၊ ဗီဒီယို (သို့) ဖိုင်ကို ယခုပေးပို့ပါ။\n(ပေးပို့ခြင်းကို ရပ်ဆိုင်းလိုပါက `/cancel` ဟု ရိုက်ထည့်ပါ။)", 
        parse_mode="Markdown"
    )
    bot.register_next_step_handler(msg, process_broadcast)

def process_broadcast(message):
    if message.content_type == 'text' and message.text.lower() == '/cancel':
        bot.reply_to(message, "❌ Broadcast ပေးပို့ခြင်းကို ရပ်ဆိုင်းလိုက်ပါသည်။")
        return

    bot.reply_to(message, "🚀 Broadcast စတင်ပေးပို့နေပါပြီ။ ပြီးဆုံးပါက အကြောင်းကြားပေးပါမည်...")
    Thread(target=execute_broadcast, args=(message,)).start()

def execute_broadcast(broadcast_msg):
    admin_cfg = get_user_config(ADMIN_ID)
    users_dict = admin_cfg.get('authorized_users', {})
    
    user_ids = list(users_dict.keys())
    if str(ADMIN_ID) not in user_ids:
        user_ids.append(str(ADMIN_ID))

    success = 0
    failed = 0

    for uid_str in set(user_ids):
        try:
            uid = int(uid_str)
            bot.copy_message(
                chat_id=uid, 
                from_chat_id=broadcast_msg.chat.id, 
                message_id=broadcast_msg.message_id
            )
            success += 1
            time.sleep(0.05) 
        except Exception as e:
            failed += 1

    # ပေးပို့မှု ပြီးစီးကြောင်း Admin ထံ Report ပြန်ပို့မည်
    final_report = (
        f"📊 **Broadcast ပေးပို့မှု ပြီးဆုံးပါပြီ**\n\n"
        f"✅ အောင်မြင်စွာ လက်ခံရရှိသူ: **{success}** ယောက်\n"
        f"❌ မအောင်မြင်သူ (Bot ကို Block ထားသူများ): **{failed}** ယောက်"
    )
    try:
        bot.send_message(ADMIN_ID, final_report, parse_mode="Markdown")
    except: pass

# ==========================================
# USER PROFILE & STATUS
# ==========================================
@bot.message_handler(commands=['myinfo', 'profile'])
def show_user_profile(message):
    user_id = message.from_user.id
    first_name = message.from_user.first_name

    user_data = get_user_config(user_id)
    ref_count = user_data.get('referral_count', 0)
    coins = user_data.get('coins', 0)
    channels_count = len(user_data.get('channels', {}))

    # Status နှင့် ကျန်ရှိသက်တမ်းကို တွက်ချက်ခြင်း
    status_text = "❌ အသုံးပြုခွင့် မရှိပါ"
    time_left_str = "-"

    if user_id == ADMIN_ID:
        status_text = "👑 Admin"
        time_left_str = "Unlimited (အချိန်အကန့်အသတ်မရှိ)"
    elif is_authorized(user_id):
        status_text = "✅ အသုံးပြုခွင့် ရရှိထားပါသည်"
        expiry = authorized_cache.get(user_id)
        if expiry:
            current_time = time.time()
            time_left_seconds = expiry - current_time
            if time_left_seconds > 0:
                # စက္ကန့်ကို ရက်အဖြစ် ပြောင်းလဲခြင်း
                days_left = time_left_seconds / 86400
                time_left_str = f"{days_left:.1f} ရက်"
            else:
                time_left_str = "သက်တမ်းကုန်ဆုံးသွားပါပြီ"
        else:
            time_left_str = "Unlimited (အချိန်အကန့်အသတ်မရှိ)"

    # Message ပုံစံဖန်တီးခြင်း
    text = f"👤 **{first_name} ၏ အချက်အလက်များ**\n"
    text += "━━━━━━━━━━━━━━━━\n"
    text += f"🆔 **User ID:** `{user_id}`\n"
    text += f"🔰 **Status:** {status_text}\n"
    text += f"⏳ **ကျန်ရှိသက်တမ်း:** {time_left_str}\n"
    text += f"📌 **Target Channels:** `{channels_count}` ခု သတ်မှတ်ထားပါသည်\n\n"
    
    text += "🎁 **Referral & Rewards**\n"
    text += "━━━━━━━━━━━━━━━━\n"
    text += f"👥 **ဖိတ်ခေါ်ထားသူ:** {ref_count} ယောက်\n"
    text += f"🪙 **လက်ကျန် Coins:** {coins} Coins\n\n"
    
    text += "💡 *မှတ်ချက် - Coins 50 ပြည့်တိုင်း /redeem ကိုနှိပ်၍ သက်တမ်း ၁ ရက် အခမဲ့ တိုးနိုင်ပါသည်။*"

    bot.reply_to(message, text, parse_mode="Markdown")

@bot.message_handler(commands=['setchannel'])
def set_channel(message):
    user_id = message.from_user.id
    if not is_authorized(user_id): return
    parts = message.text.split(maxsplit=2)
    if len(parts) >= 3:
        channel_id = parts[1]
        channel_name = parts[2]
        
        cfg = get_user_config(user_id)
        channels = cfg.get('channels', {})
        # Channel အသစ်ထည့်မည် (Caption အလွတ်ဖြင့်)
        channels[channel_id] = {"name": channel_name, "caption": ""}
        update_user_setting(user_id, "channels", channels)
        
        bot.reply_to(message, f"✅ Target Channel အသစ်ထည့်သွင်းပြီးပါပြီ။\n\n🆔 ID: `{channel_id}`\n📛 Name: {channel_name}")
    else:
        bot.reply_to(message, "⚠️ အသုံးပြုနည်း မှားယွင်းနေပါသည်။\n\nUsage: `/setchannel [Channel ID] [Channel Name]`\nExample: `/setchannel -100123456789 Action Movies`")

@bot.message_handler(commands=['delchannel'])
def del_channel(message):
    user_id = message.from_user.id
    if not is_authorized(user_id): return
    parts = message.text.split()
    if len(parts) == 2:
        channel_id = parts[1]
        cfg = get_user_config(user_id)
        channels = cfg.get('channels', {})
        if channel_id in channels:
            del channels[channel_id]
            update_user_setting(user_id, "channels", channels)
            bot.reply_to(message, f"🗑 Channel `{channel_id}` ကို ဖယ်ရှားလိုက်ပါပြီ။")
        else:
            bot.reply_to(message, "⚠️ ထို Channel ID ကို ထည့်သွင်းထားခြင်း မရှိပါ။")
    else:
        bot.reply_to(message, "⚠️ Usage: `/delchannel [Channel ID]`")

@bot.message_handler(commands=['checkchannel'])
def check_channel(message):
    user_id = message.from_user.id
    if not is_authorized(user_id): return
    cfg = get_user_config(user_id)
    channels = cfg.get('channels', {})
    
    if not channels:
        bot.reply_to(message, "⚠️ မည်သည့် Target Channel မှ ထည့်သွင်းထားခြင်း မရှိသေးပါ။")
        return
        
    text = "📡 **သင်၏ Target Channels များ**\n━━━━━━━━━━━━━━━━\n"
    for ch_id, ch_data in channels.items():
        cap_status = "✅ ရှိသည်" if ch_data.get('caption') else "❌ မရှိပါ"
        text += f"📛 **{ch_data.get('name', 'Unknown')}**\n🆔 `{ch_id}`\n📝 Caption: {cap_status}\n\n"
        
    bot.reply_to(message, text, parse_mode="Markdown")
    
@bot.message_handler(commands=['auth'])
def add_user(message):
    if message.from_user.id != ADMIN_ID: return
    try:
        parts = message.text.split()
        new_user_id = int(parts[1])
        # ရက်အရေအတွက် မထည့်ရင် ပုံသေ ၃၀ ရက် သတ်မှတ်မယ်
        days = float(parts[2]) if len(parts) > 2 else 30.0 
        
        # သက်တမ်းကုန်မည့် အချိန်ကို တွက်ချက်ခြင်း (၁ ရက် = ၈၆၄၀၀ စက္ကန့်)
        expiry_time = time.time() + (days * 86400)
        
        # Database ထဲသို့ ထည့်ရန်
        config_col.update_one(
            {"_id": str(ADMIN_ID)}, 
            {"$set": {f"authorized_users.{new_user_id}": expiry_time}}, 
            upsert=True
        )

        with cache_lock:
            authorized_cache[new_user_id] = expiry_time


        bot.reply_to(message, f"✅ User ID `{new_user_id}` ကို {days} ရက် အသုံးပြုခွင့်ပေးလိုက်ပါပြီ။")
    except Exception as e:
        bot.reply_to(message, "⚠️ Usage: `/auth [UserID] [Days]`\nExample: `/auth 123456789 7`")

@bot.message_handler(commands=['unauth'])
def remove_user(message):
    if message.from_user.id != ADMIN_ID: return
    try:
        target_id = int(message.text.split()[1])
        if target_id == ADMIN_ID: return
        
        # Database မှ ဖယ်ထုတ်ရန်
        config_col.update_one(
            {"_id": str(ADMIN_ID)}, 
            {"$unset": {f"authorized_users.{target_id}": ""}}
        )

        # Cache ထဲမှ ဖယ်ထုတ်ရန်
        with cache_lock: # 👈 ဒီ Lock ထည့်ပေးပါ
            if target_id in authorized_cache:
                del authorized_cache[target_id]

        bot.reply_to(message, f"🗑 User ID `{target_id}` ရဲ့ အသုံးပြုခွင့်ကို ရပ်ဆိုင်းလိုက်ပါပြီ။")
    except:
        bot.reply_to(message, "⚠️ Usage: `/unauth [UserID]`")

@bot.message_handler(commands=['setcaption'])
def set_custom_caption_text(message):
    user_id = message.from_user.id
    if not is_authorized(user_id): return
    parts = message.text.split(maxsplit=2)
    if len(parts) >= 3:
        channel_id = parts[1]
        caption_text = parts[2]
        
        cfg = get_user_config(user_id)
        channels = cfg.get('channels', {})
        
        if channel_id in channels:
            channels[channel_id]['caption'] = caption_text
            update_user_setting(user_id, "channels", channels)
            bot.reply_to(message, f"✅ Channel `{channel_id}` အတွက် ပုံသေစာသား သတ်မှတ်ပြီးပါပြီ:\n\n`{caption_text}`")
        else:
            bot.reply_to(message, "⚠️ ထို Channel အား ထည့်သွင်းထားခြင်း မရှိပါ။ `/setchannel` ကို အရင်အသုံးပြုပါ။")
    else:
        bot.reply_to(message, "⚠️ Usage: `/setcaption [Channel ID] [Your Text]`\nExample: `/setcaption -100123456789 Join our main channel!`")

@bot.message_handler(commands=['delcaption'])
def delete_custom_caption_text(message):
    user_id = message.from_user.id
    if not is_authorized(user_id): return
    parts = message.text.split()
    if len(parts) == 2:
        channel_id = parts[1]
        cfg = get_user_config(user_id)
        channels = cfg.get('channels', {})
        if channel_id in channels:
            channels[channel_id]['caption'] = ""
            update_user_setting(user_id, "channels", channels)
            bot.reply_to(message, f"🗑 Channel `{channel_id}` ၏ ပုံသေစာသားကို ဖျက်လိုက်ပါပြီ။")
        else:
            bot.reply_to(message, "⚠️ ထို Channel အား ထည့်သွင်းထားခြင်း မရှိပါ။")
    else:
         bot.reply_to(message, "⚠️ Usage: `/delcaption [Channel ID]`")

@bot.message_handler(commands=['users'])
def list_authorized_users(message):
    if message.from_user.id != ADMIN_ID: return
    
    # ၁။ Lock ခံပြီး လက်ရှိ Cache ထဲက Data တွေကို Snapshot ရယူမယ်
    with cache_lock:
        current_users = dict(authorized_cache) 
    
    # ၂။ အောက်က အလုပ်တွေအားလုံးကို Snapshot (current_users) နဲ့ပဲ လုပ်တော့မယ်
    total_users = len(current_users) - 1
    text = f"👥 **Authorized Users Total: {total_users}**\n"
    text += "━━━━━━━━━━━━━━━━\n"
    current_time = time.time()
    
    # ⚠️ အရေးကြီး - ဒီနေရာမှာ current_users.items() ကိုပဲ သုံးရပါမယ်
    for uid, expiry in current_users.items():
        if uid == ADMIN_ID: continue
        
        time_left_str = "Unlimited"
        if expiry is not None:
            days_left = max(0, (expiry - current_time) / 86400)
            time_left_str = f"{days_left:.1f} Days Left"
            
        try:
            # User တစ်ယောက်ချင်းစီရဲ့ နာမည်ကို Telegram API ကနေ လှမ်းယူမယ်
            user = bot.get_chat(uid)
            text += f"👤 {user.first_name}\n🆔 `{uid}`\n⏳ {time_left_str}\n\n"
        except:
            text += f"👤 Unknown User\n🆔 `{uid}`\n⏳ {time_left_str}\n\n"
            
    bot.reply_to(message, text, parse_mode="Markdown")

# ==========================================
# BATCH PROCESSING
# ==========================================
pending_files = {}
batch_data = {} 
pending_sends = {}

def ask_for_channel_selection(chat_id, user_id, messages, user_custom_text=None):
    """Button များထုတ်ပေးပြီး မည်သည့် Channel ကို ပို့မည်လဲ မေးသော Function"""
    cfg = get_user_config(user_id)
    channels = cfg.get('channels', {})

    if not channels:
        bot.send_message(chat_id, "⚠️ Target Channel မသတ်မှတ်ရသေးပါ။ `/setchannel` ဖြင့် အရင်သတ်မှတ်ပါ။")
        return

    # User Button နှိပ်ချိန်တွင် ပို့နိုင်ရန် Temporary သိမ်းထားမည်
    pending_sends[chat_id] = {
        'messages': messages,
        'user_text': user_custom_text # Single file မှာ User ရိုက်ထည့်လိုက်တဲ့ စာသား
    }

    markup = InlineKeyboardMarkup(row_width=1)
    for ch_id, ch_data in channels.items():
        btn = InlineKeyboardButton(text=f"ပို့မည် ➡️ {ch_data.get('name', 'Unknown')}", callback_data=f"sendto_{ch_id}")
        markup.add(btn)

    bot.send_message(chat_id, "📌 ကျေးဇူးပြု၍ ပေးပို့လိုသော Channel ကို ရွေးချယ်ပါ:", reply_markup=markup)

def process_batch(chat_id, user_id):
    if chat_id not in batch_data: return
    messages = batch_data[chat_id]['messages']

    if len(messages) > 1:
        # Group လိုက်ပို့ခြင်းဖြစ်လျှင် Button တန်းပြမည်
        bot.send_message(chat_id, f"✅ ဖိုင် ({len(messages)}) ခု လက်ခံရရှိသည်။")
        ask_for_channel_selection(chat_id, user_id, messages)
    
    elif len(messages) == 1:
        # တစ်ကားတည်းဆိုလျှင် Caption အရင်တောင်းမည်
        msg = messages[0]
        pending_files[chat_id] = {'message': msg, 'user_id': user_id}
        bot.reply_to(msg, "✏️ **ဒီကားအတွက် Caption ရေးပို့ပေးပါ။**\n(စာမရေးလိုပါက 'x' ဟု ရိုက်ထည့်ပါ။)")

    if chat_id in batch_data: del batch_data[chat_id]

@bot.message_handler(func=lambda m: m.chat.id in pending_files, content_types=['text'])
def receive_caption(message):
    user_id = message.from_user.id
    if not is_authorized(user_id): return
    chat_id = message.chat.id
    
    file_info = pending_files.get(chat_id)
    if not file_info: return
    
    user_text = message.text
    if user_text.lower() == 'x':
        user_text = "" # x ဟုရိုက်လျှင် Caption မပါဘဲ ပို့မည်
        
    msg_obj = file_info['message']
    
    del pending_files[chat_id]
    # Caption ရပြီဖြစ်သဖြင့် မည်သည့် Channel သို့ပို့မည်ကို Button ပြမည်
    ask_for_channel_selection(chat_id, user_id, [msg_obj], user_custom_text=user_text)

@bot.callback_query_handler(func=lambda call: call.data.startswith('sendto_'))
def handle_channel_selection(call):
    chat_id = call.message.chat.id
    user_id = call.from_user.id
    target_channel_id = call.data.split('sendto_')[1]

    if chat_id not in pending_sends:
        bot.answer_callback_query(call.id, "⚠️ သက်တမ်းကုန်သွားသော Action ဖြစ်ပါသည်။ ဖိုင်များပြန်လည်ပေးပို့ပါ။", show_alert=True)
        return

    data = pending_sends[chat_id]
    messages = data['messages']
    user_text = data['user_text']

    cfg = get_user_config(user_id)
    channels = cfg.get('channels', {})
    
    if target_channel_id not in channels:
        bot.answer_callback_query(call.id, "⚠️ ဤ Channel ကို ဖယ်ရှားလိုက်ပြီဖြစ်ပါသည်။", show_alert=True)
        return
        
    ch_data = channels[target_channel_id]
    ch_name = ch_data.get('name', target_channel_id)
    ch_caption = ch_data.get('caption', "")

    # Loading ပြမည်
    bot.edit_message_text(f"🚀 `{ch_name}` သို့ ပို့ဆောင်နေပါပြီ... စောင့်ဆိုင်းပေးပါ...", chat_id=chat_id, message_id=call.message.message_id)

    success_count = 0
    for msg in messages:
        try:
            # Caption တွဲခြင်း Logic
            if len(messages) == 1 and user_text is not None:
                base_text = user_text # Single file အတွက် user ရိုက်တဲ့စာ
            else:
                base_text = msg.caption if msg.caption else "" # Batch အတွက် မူလ caption

            # Channel ရဲ့ Custom Caption နဲ့ ပေါင်းမည်
            final_caption = f"{base_text}\n\n{ch_caption}" if ch_caption else base_text
            final_caption = final_caption.strip()[:1024] # Telegram ၏ Limit သို့ ဖြတ်မည်

            bot.copy_message(chat_id=target_channel_id, from_chat_id=msg.chat.id, message_id=msg.message_id, caption=final_caption)
            success_count += 1
            time.sleep(1.5) # Telegram Limit မထိအောင် နားမည်
        except Exception as e:
            bot.send_message(chat_id, f"❌ မပို့နိုင်သောဖိုင် (ID: {msg.message_id}) - Error: {e}")

    # ပို့ပြီးကြောင်း ပြမည်
    bot.edit_message_text(f"✅ `{ch_name}` သို့ ဖိုင်ပေါင်း ({success_count}) ခု အောင်မြင်စွာ ပို့ဆောင်ပြီးပါပြီ။", chat_id=chat_id, message_id=call.message.message_id)
    
    # Temporary Data ရှင်းလင်းမည်
    del pending_sends[chat_id]

@bot.message_handler(content_types=['video', 'document', 'photo', 'text'])
def receive_video(message):
    user_id = message.from_user.id
    if not is_authorized(user_id): return
    chat_id = message.chat.id

    if chat_id in pending_files:
        return
        
    if chat_id in batch_data and batch_data[chat_id]['timer']:
        batch_data[chat_id]['timer'].cancel()
    if chat_id not in batch_data:
        batch_data[chat_id] = {'messages': [], 'timer': None}
    batch_data[chat_id]['messages'].append(message)
    batch_data[chat_id]['timer'] = Timer(2.0, process_batch, [chat_id, user_id])
    batch_data[chat_id]['timer'].start()

@bot.message_handler(func=lambda m: m.text and "t.me/" in m.text)
def handle_post_link(message):
    user_id = message.from_user.id
    if not is_authorized(user_id): return
    match = re.search(r"t\.me/([^/]+)/(\d+)", message.text)
    if match:
        cfg = get_user_config(user_id)
        try:
            bot.copy_message(chat_id=cfg.get('channel_id'), from_chat_id=f"@{match.group(1)}", message_id=int(match.group(2)))
            bot.reply_to(message, "✅ Sent.")
        except Exception as e:
            bot.reply_to(message, f"❌ Error: {e}")

# ==========================================
# BOT MENU SETUP
# ==========================================
def setup_bot_commands():
    """Bot ရဲ့ Chat ဘေးမှာ Menu (Commands List) ပေါ်အောင် သတ်မှတ်ရန်"""
    commands = [
        BotCommand("backup", "Channel Backup ပြုလုပ်ရန်"),
        BotCommand("setchannel", "Target Channel သတ်မှတ်ရန်"),
        BotCommand("checkchannel", "လက်ရှိ Target Channel စစ်ဆေးရန်"),
        BotCommand("setcaption", "ပုံသေတွဲတင်မည့် စာသား သတ်မှတ်ရန်"),
        BotCommand("delcaption", "ပုံသေစာသားကို ဖယ်ရှားရန်"),
        BotCommand("invite", "သူငယ်ချင်းကို ဖိတ်ခေါ်ြီး အခမဲ့ သုံးခွင့်ရယူရန်"),
        BotCommand("redeem", "🪙 Coins များလဲလှယ်ရန်"),
        BotCommand("myinfo", "👤 မိမိ၏ အချက်အလက်နှင့် လက်ကျန်သက်တမ်းကြည့်ရန်"),
        BotCommand("delchannel", "Channel ဖယ်ရှားရန်"),
        BotCommand("clearlogs", "Backup မှတ်တမ်းများကို ဖျက်ရန်")
    ]
    try:
        bot.set_my_commands(commands)
        print("✅ Bot menu commands successfully configured.")
    except Exception as e:
        print(f"⚠️ Failed to set bot commands: {e}")

if __name__ == "__main__":
    load_authorized_users()
    setup_bot_commands()
    keep_alive()
    print("🤖 Bot Started with MongoDB Support...")
    bot.infinity_polling()




