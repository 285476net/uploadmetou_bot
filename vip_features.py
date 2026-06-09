import time
import uuid
from datetime import datetime
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

bot = None
db = None
vip_files = None
vip_users = None
settings = None

def init(main_bot, main_db):
    """မူရင်း bot.py မှ Database နှင့် Bot ကို ချိတ်ဆက်ရန်"""
    global bot, db, vip_files, vip_users, settings
    bot = main_bot
    db = main_db
    vip_files = db['vip_files']
    vip_users = db['vip_downloads']
    settings = db['settings']
    
    # 🚀 Data Indexing ပြုလုပ်ခြင်း (Data များလာလျှင် မနှေးစေရန်)
    try:
        vip_files.create_index([("file_id", 1)], name="fast_vip_lookup", unique=True)
        print("🚀 VIP Database Indexing: Success")
    except Exception as e:
        print(f"⚠️ VIP Indexing Error: {e}")

def is_vip_mode_on(user_id):
    cfg = settings.find_one({"_id": str(user_id)})
    return cfg.get("vip_mode", False) if cfg else False

def get_daily_limit(admin_id):
    cfg = settings.find_one({"_id": str(admin_id)})
    return cfg.get("vip_limit", 10) if cfg else 10

def process_vip_send(user_id, target_channel_id, msg, final_caption):
    """Target Channel သို့ Button ဖြင့်တွဲ၍ ပို့ခြင်း"""
    file_id = str(uuid.uuid4())[:8] 
    bot_info = bot.get_me()
    deep_link = f"https://t.me/{bot_info.username}?start=getfile_{file_id}"
    
    # DB တွင် မှတ်သားခြင်း
    vip_files.insert_one({
        "file_id": file_id,
        "owner_id": user_id,
        "source_chat": msg.chat.id,
        "msg_id": msg.message_id,
        "channel_id": target_channel_id,
        "caption": final_caption
    })
    
    # 🔘 Inline Button ဖန်တီးခြင်း
    markup = InlineKeyboardMarkup()
    btn = InlineKeyboardButton(text="📥 Get Donwload File", url=deep_link)
    markup.add(btn)
    
    # Channel သို့ ပို့သည့်အခါ စာသားအောက်တွင် Button တွဲ၍ ပို့မည်
    bot.copy_message(
        chat_id=target_channel_id, 
        from_chat_id=msg.chat.id, 
        message_id=msg.message_id, 
        caption=final_caption, 
        reply_markup=markup
    )

def handle_vip_download(message, file_id):
    """User က Button လင့်ခ်ကို နှိပ်ပြီး ဝင်လာသောအခါ အလုပ်လုပ်မည့်အပိုင်း"""
    downloader_id = message.from_user.id
    
    # ၁။ ဖိုင်ကို Database တွင် ရှာခြင်း
    file_data = vip_files.find_one({"file_id": file_id})
    if not file_data:
        bot.send_message(downloader_id, "❌ ဤဖိုင်ကို ရှာမတွေ့တော့ပါ။ (သို့) ပယ်ဖျက်လိုက်ပါပြီ။")
        return
        
    channel_id = file_data['channel_id']
    owner_id = file_data['owner_id']
    
    # ၂။ VIP Subscriber ဟုတ်/မဟုတ် စစ်ဆေးခြင်း
    try:
        member = bot.get_chat_member(chat_id=channel_id, user_id=downloader_id)
        if member.status not in ['member', 'administrator', 'creator']:
            bot.send_message(downloader_id, "❌ Access Denied\n\nသင်ဟာ VIP ဝယ်ယူထားခြင်းမရှိသည့်အတွက် ဖိုင်ကို ရယူခွင့်မရှိပါ။")
            return
    except Exception as e:
        bot.send_message(downloader_id, "🚫 Access Denied\n\nသင်ဟာ Premium VIP Member မဟုတ်ကြောင်း တွေ့ရှိရပါသည်။")
        return
        
    # ၃။ Daily Limit စစ်ဆေးခြင်း
    today_str = datetime.now().strftime("%Y-%m-%d")
    user_dl_data = vip_users.find_one({"_id": str(downloader_id)})
    
    current_count = 0
    if user_dl_data and user_dl_data.get("date") == today_str:
        current_count = user_dl_data.get("count", 0)
        
    daily_limit = get_daily_limit(owner_id)
    
    if current_count >= daily_limit:
        bot.send_message(downloader_id, f"⚠️Limit Reached! Try again after 24hours.")
        return
        
    # ၄။ ဖိုင်ပေးပို့ခြင်း
    try:
        bot.copy_message(
            chat_id=downloader_id, 
            from_chat_id=file_data['source_chat'], 
            message_id=file_data['msg_id'], 
            caption=file_data['caption'] 
        )
        
        # Limit Count တိုးခြင်း
        vip_users.update_one(
            {"_id": str(downloader_id)}, 
            {"$set": {"date": today_str}, "$inc": {"count": 1}}, 
            upsert=True
        )
    except Exception as e:
        bot.send_message(downloader_id, "❌ ဖိုင်ပေးပို့ရာတွင် အခက်အခဲရှိနေပါသည်။")
