"""User-facing strings, in Uzbek.

Kept in one module so milestone 7's ru/uz switch is a lookup change rather than a
sweep through every handler.
"""

STATUS_LABEL = {
    "backlog": "Backlog",
    "todo": "Bajarilishi kerak",
    "in_progress": "Bajarilmoqda",
    "blocked": "To‘xtatilgan",
    "review": "Tekshiruvda",
    "done": "Bajarildi",
    "cancelled": "Bekor qilindi",
}

STATUS_EMOJI = {
    "backlog": "⚪️",
    "todo": "🔵",
    "in_progress": "🟠",
    "blocked": "🔴",
    "review": "🟣",
    "done": "✅",
    "cancelled": "⚫️",
}

PRIORITY_LABEL = {
    "low": "past",
    "normal": "oddiy",
    "high": "muhim",
    "urgent": "shoshilinch",
}

PRIORITY_EMOJI = {"low": "🔽", "normal": "▫️", "high": "🔺", "urgent": "‼️"}

NOT_REGISTERED = (
    "Siz hali ro‘yxatdan o‘tmagansiz.\n\n"
    "Avval saytga Telegram orqali kiring, so‘ng menejer hisobingizni tasdiqlaydi."
)

PENDING_APPROVAL = (
    "Hisobingiz hali tasdiqlanmagan.\n\n"
    "Menejer tasdiqlagach, bot vazifalarni ko‘rsata boshlaydi."
)

HELP = (
    "<b>Vazifa yaratish</b>\n"
    "Shunchaki yozing:\n"
    "<code>keto: mini app url ni tuzat !shoshilinch @asad ertaga 18:00</code>\n\n"
    "Bot tushunganini ko‘rsatadi — tasdiqlaganingizdan keyingina yaratiladi.\n\n"
    "<code>@codex</code> PR ochadi; <code>!fast</code> hozircha faqat "
    "Task Manager loyihasining egasi uchun pilot rejimda PRsiz prod yo‘lini tanlaydi.\n\n"
    "<b>Buyruqlar</b>\n"
    "/new — bosqichma-bosqich yaratish\n"
    "/my — mening ochiq vazifalarim\n"
    "/today — bugungi va muddati o‘tganlari\n"
    "/projects — loyihalar ro‘yxati\n"
    "/login — bir martalik brauzer havolasi\n"
    "/invite — jamoaga taklif (egasi uchun)\n"
    "/pending — tasdiqlash kutayotgan a’zolar\n"
    "/help — shu yordam\n\n"
    "Xabarga <code>/task</code> deb javob bersangiz, o‘sha xabar vazifaga aylanadi."
)
