# -*- coding: utf-8 -*-
"""
Bot Telegram thông báo hội nghị khoa học tại Nga (nguồn: konferencii.ru).

Cách hoạt động:
  1. Quét các trang chuyên mục (topic) trên konferencii.ru.
  2. Lọc các hội nghị KHAI MẠC sau đúng N ngày (mặc định: 30, 14, 7 ngày).
  3. Đánh dấu 🎯 nếu tiêu đề khớp từ khóa (robot, mechatronics, phục hồi chức năng...).
  4. Đánh dấu ⭐ВАК nếu kỷ yếu/tạp chí của hội nghị nằm trong Перечень ВАК.
  5. Gửi báo cáo qua Telegram (HTML).

Mọi cấu hình đều qua biến môi trường (xem README.md). Chạy thử cục bộ:
  DRY_RUN=1 python conference_bot.py
"""

import html
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

# ============================ CẤU HÌNH ============================

BASE_URL = "https://konferencii.ru"

# Token và Chat ID của Telegram — đặt trong GitHub Secrets, KHÔNG ghi thẳng vào code
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Thông báo khi còn đúng N ngày tới ngày khai mạc (có thể đặt "30" nếu chỉ muốn 1 mốc)
NOTIFY_DAYS = {
    int(x) for x in os.environ.get("NOTIFY_DAYS", "30,14,7").split(",") if x.strip()
}

# Các chuyên mục cần quét (slug trong URL https://konferencii.ru/topic/<slug>/<trang>)
TOPIC_SLUGS = [
    s.strip()
    for s in os.environ.get(
        "TOPIC_SLUGS",
        "tehnicheskie-nauki,mashinostroenie,meditsina,"
        "informatsionnyie-tehnologii,biotehnologii,tehnologii",
    ).split(",")
    if s.strip()
]

# Số trang quét cho mỗi chuyên mục (trang đầu chứa các sự kiện gần nhất)
MAX_PAGES = int(os.environ.get("MAX_PAGES", "5"))

# Độ trễ giữa các request (giây) — để tránh bị chặn
DELAY = float(os.environ.get("DELAY", "2"))

# Gốc từ khóa (tiếng Nga, viết thường, không cần đuôi biến cách).
# Tiêu đề chứa bất kỳ gốc nào sẽ được gắn 🎯 và xếp lên đầu.
KEYWORDS = [
    k.strip().lower()
    for k in os.environ.get(
        "KEYWORDS",
        "робот,мехатрон,биомехан,реабилит,экзоскелет,протез,"
        "медицин,автоматиз,манипулятор,привод,искусственный интеллект",
    ).split(",")
    if k.strip()
]

# true = CHỈ gửi hội nghị khớp từ khóa; false = gửi mọi hội nghị thuộc chuyên mục đã chọn
STRICT_KEYWORDS = os.environ.get("STRICT_KEYWORDS", "false").lower() == "true"

# true = vẫn gửi tin "hôm nay không có hội nghị nào" (mặc định: im lặng)
SEND_EMPTY = os.environ.get("SEND_EMPTY", "false").lower() == "true"

# In ra màn hình thay vì gửi Telegram (để chạy thử)
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")

# Ghi đè "ngày hôm nay" để kiểm thử, định dạng YYYY-MM-DD
TEST_TODAY = os.environ.get("TEST_TODAY", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}

# ============================ PHÂN TÍCH HTML ============================

RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

DATE_RE = re.compile(
    r"(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|"
    r"августа|сентября|октября|ноября|декабря)\s+(\d{4})\s*г",
    re.IGNORECASE,
)

INFO_LINK_RE = re.compile(
    r'<a[^>]+href="(?:https?://(?:www\.)?konferencii\.ru)?/info/(\d+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)

COUNTRIES = (
    "Россия|Беларусь|Казахстан|Узбекистан|Кыргызстан|Таджикистан|Азербайджан|"
    "Армения|Грузия|Молдова|Вьетнам|Китай|Австралия|США|Германия|Чехия|Турция|"
    "Индия|Сербия|Болгария|Венгрия|Польша"
)
LOCATION_RE = re.compile(r"(?:%s)\s*,\s*[^\n()<,;]{2,40}" % COUNTRIES)


def to_date(m):
    """Chuyển match của DATE_RE thành datetime.date."""
    day, month_name, year = m.groups()
    return date(int(year), RU_MONTHS[month_name.lower()], int(day))


def clean(text):
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def parse_listing(html_page):
    """Tách một trang danh sách của konferencii.ru thành list các sự kiện (dict).

    Mỗi sự kiện trên trang được phân tách bởi ảnh huy hiệu 'medal4.png',
    sau đó là: ngày bắt đầu — ngày kết thúc, hạn nộp ("срок заявок"),
    tiêu đề (link /info/<id>), chuyên mục (/topic/...), địa điểm (in đậm),
    hệ thống chỉ mục (/ref-base/...) và ban tổ chức ("Организаторы:").
    Nếu sau này trang web đổi giao diện, chỉ cần sửa hàm này.
    """
    events = []
    blocks = re.split(r"medal4\.png", html_page)[1:]  # bỏ phần đầu trang
    for block in blocks:
        # cắt bỏ phần phân trang/chân trang nếu lọt vào khối cuối
        block = re.split(r"предыдущая|Ctrl", block)[0]

        link_m = INFO_LINK_RE.search(block)
        if not link_m:
            continue
        event_id = link_m.group(1)
        title = clean(BeautifulSoup(link_m.group(2), "html.parser").get_text(" "))
        if not title:
            continue

        text = clean(BeautifulSoup(block, "html.parser").get_text(" "))
        dates = list(DATE_RE.finditer(text))
        if not dates:
            continue
        start = to_date(dates[0])
        end = to_date(dates[1]) if len(dates) > 1 else start

        deadline = None
        dl_m = re.search(r"срок\s+заявок\s*:?\s*(.{0,40})", text, re.IGNORECASE)
        if dl_m:
            d2 = DATE_RE.search(dl_m.group(1))
            if d2:
                deadline = to_date(d2)
        deadline_closed = "заявок закончен" in text

        topics = [
            clean(t)
            for t in re.findall(
                r'href="[^"]*/topic/[\w\-]+/\d+"[^>]*>([^<]+)<', block
            )
        ]
        ref_bases = list(dict.fromkeys(
            clean(t)
            for t in re.findall(
                r'href="[^"]*/ref-base/[\w\-]+/\d+"[^>]*>([^<]+)<', block
            )
        ))

        location = ""
        for tag in ("b", "strong"):
            bm = re.search(r"<%s>\s*([^<>]{3,80}?)\s*</%s>" % (tag, tag), block)
            if bm and "," in bm.group(1):
                location = clean(bm.group(1))
                break
        if not location:
            lm = LOCATION_RE.search(text)
            location = clean(lm.group(0)) if lm else ""

        org = ""
        om = re.search(r"Организаторы\s*:\s*(.+?)(?:$|\Z)", text)
        if om:
            org = clean(om.group(1))[:200]

        events.append({
            "id": event_id,
            "title": title,
            "url": f"{BASE_URL}/info/{event_id}",
            "start": start,
            "end": end,
            "deadline": deadline,
            "deadline_closed": deadline_closed,
            "topics": topics,
            "ref_bases": ref_bases,
            "location": location,
            "organizers": org,
        })
    return events


# ============================ THU THẬP ============================

def fetch(url):
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r.text
            print(f"[!] {url} -> HTTP {r.status_code}")
            if r.status_code == 404:
                return None
        except requests.RequestException as e:
            print(f"[!] {url} -> {e}")
        time.sleep(DELAY * (attempt + 1))
    return None


def collect_events():
    """Quét tất cả chuyên mục, gộp và khử trùng lặp theo id sự kiện."""
    seen = {}
    for slug in TOPIC_SLUGS:
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}/topic/{slug}/{page}"
            html_page = fetch(url)
            time.sleep(DELAY)
            if not html_page:
                break
            page_events = parse_listing(html_page)
            print(f"[i] {slug} trang {page}: {len(page_events)} sự kiện")
            if not page_events:
                break
            for ev in page_events:
                seen.setdefault(ev["id"], ev)
    return list(seen.values())


# ============================ LỌC & ĐỊNH DẠNG ============================

def keyword_hit(ev):
    t = ev["title"].lower()
    return any(k in t for k in KEYWORDS)


def select_events(events, today):
    out = []
    for ev in events:
        days_left = (ev["start"] - today).days
        if days_left not in NOTIFY_DAYS:
            continue
        hit = keyword_hit(ev)
        if STRICT_KEYWORDS and not hit:
            continue
        ev["days_left"] = days_left
        ev["keyword_hit"] = hit
        out.append(ev)
    # 🎯 khớp từ khóa lên đầu, sau đó theo số ngày còn lại
    out.sort(key=lambda e: (not e["keyword_hit"], e["days_left"], e["start"]))
    return out


def fmt_date(d):
    return d.strftime("%d.%m.%Y") if d else "—"


def format_event(ev):
    mark = "🎯 " if ev["keyword_hit"] else "🔹 "
    vak = " ⭐ВАК" if any("ВАК" in rb for rb in ev["ref_bases"]) else ""
    lines = [f'{mark}<b>{html.escape(ev["title"])}</b>{vak}']
    when = fmt_date(ev["start"])
    if ev["end"] and ev["end"] != ev["start"]:
        when += f' — {fmt_date(ev["end"])}'
    lines.append(f'📅 {when} (còn {ev["days_left"]} ngày)')
    if ev["location"]:
        lines.append(f'📍 {html.escape(ev["location"])}')
    if ev["deadline"]:
        dl = f'⏰ Hạn nộp bài: {fmt_date(ev["deadline"])}'
        if ev["deadline_closed"]:
            dl += " ⚠️ đã đóng"
        lines.append(dl)
    if ev["ref_bases"]:
        lines.append(f'📚 Chỉ mục: {html.escape(", ".join(ev["ref_bases"]))}')
    if ev["topics"]:
        lines.append(f'🏷 {html.escape(", ".join(ev["topics"][:4]))}')
    lines.append(f'🔗 {ev["url"]}')
    return "\n".join(lines)


def build_messages(selected, today):
    header = (
        f"🗓 <b>HỘI NGHỊ KHOA HỌC SẮP DIỄN RA</b>\n"
        f"(quét ngày {fmt_date(today)}, nguồn: konferencii.ru)\n\n"
    )
    chunks, current = [], header
    for ev in selected:
        item = format_event(ev) + "\n\n"
        if len(current) + len(item) > 3500:
            chunks.append(current.rstrip())
            current = ""
        current += item
    if current.strip():
        chunks.append(current.rstrip())
    return chunks


# ============================ GỬI TELEGRAM ============================

def send_telegram(text):
    if DRY_RUN:
        print("=" * 60)
        print(text)
        return True
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=30)
    if r.status_code != 200:
        print(f"[!] Telegram lỗi {r.status_code}: {r.text}")
        return False
    return True


# ============================ MAIN ============================

def main():
    if not DRY_RUN and (not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID):
        print("Thiếu TELEGRAM_TOKEN hoặc TELEGRAM_CHAT_ID (đặt trong GitHub Secrets).")
        sys.exit(1)

    if TEST_TODAY:
        today = datetime.strptime(TEST_TODAY, "%Y-%m-%d").date()
    else:
        # lấy ngày theo giờ Moscow (UTC+3) để cron trên GitHub chạy ổn định
        today = datetime.now(timezone(timedelta(hours=3))).date()

    print(f"[i] Hôm nay: {today}; mốc thông báo: {sorted(NOTIFY_DAYS)} ngày trước khai mạc")
    events = collect_events()
    print(f"[i] Tổng cộng thu được {len(events)} sự kiện")
    selected = select_events(events, today)
    print(f"[i] Khớp điều kiện: {len(selected)} sự kiện")

    if not selected:
        if SEND_EMPTY:
            send_telegram(
                f"🗓 {fmt_date(today)}: không có hội nghị nào đạt mốc "
                f"{sorted(NOTIFY_DAYS)} ngày trong các chuyên mục đã chọn."
            )
        return

    ok = all(send_telegram(chunk) for chunk in build_messages(selected, today))
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
