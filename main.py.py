#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NBA Telegram AutoBot (Full)
- Publica noticias (RSS ESPN/Yahoo) con imagen + título + resumen
- Traduce a español automáticamente
- Si el RSS no trae imagen, obtiene og:image del artículo
- Envía CTA 3x/día con tu mensaje + calendario del día
- Genera imagen 1080x1080: título grande, filas alternadas, logos grandes y píldora de hora
- Evita duplicados con SQLite
- Corre en grupos (no se requiere canal)
"""
import os, time, sqlite3, logging, io, re, html
from datetime import datetime
from typing import Tuple

import feedparser
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dateutil import tz
from dateutil.parser import isoparse
from PIL import Image, ImageDraw, ImageFont

from telegram import Bot, ParseMode
from telegram.error import TelegramError
from dotenv import load_dotenv
from pathlib import Path

# Extras
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator

# ----------------- Carga .env -----------------
ENV_PATH = Path('.env')
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()
RSS_FEEDS = [u.strip() for u in os.getenv("RSS_FEEDS", "").split(",") if u.strip()]
CHECK_EVERY_MINUTES = int(os.getenv("CHECK_EVERY_MINUTES", "20"))
TIMEZONE = os.getenv("TIMEZONE", "America/Argentina/Buenos_Aires")

CTA_CRONS = [c.strip() for c in os.getenv("CTA_CRONS", "0 11 * * *; 0 16 * * *; 30 21 * * *").split(";") if c.strip()]
CTA_INCLUDE_SCHEDULE = os.getenv("CTA_INCLUDE_SCHEDULE", "true").strip().lower() in ("1","true","yes")
CTA_MESSAGE = os.getenv("CTA_MESSAGE", "").strip() or (
    "🚨 ULTIMAS CUENTAS DE NBA LEAGUE PASS 🚨\\n\\n"
    "🏀 Mirá todos los juegos desde tu cuenta propia de NBA League Pass.\\n\\n"
    "💸 Pago único de $26.700 por toda la temporada\\n\\n"
    "📩 Escribime por privado para que te cree tu cuenta en el momento @nbapass_latam."
)
SCHEDULE_DAILY_CRON = os.getenv("SCHEDULE_DAILY_CRON", "0 10 * * *")
SCHEDULE_HEADER = os.getenv("SCHEDULE_HEADER", "🗓️ Partidos NBA de hoy")
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"

# ----------------- Validaciones -----------------
if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("Falta TELEGRAM_BOT_TOKEN en .env")
if not TELEGRAM_CHANNEL_ID:
    raise SystemExit("Falta TELEGRAM_CHANNEL_ID en .env")
if not RSS_FEEDS:
    raise SystemExit("Falta RSS_FEEDS en .env")

# ----------------- Logging -----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("nba_telebot")

# ----------------- DB -----------------
DB_PATH = "data/state.db"
os.makedirs("data", exist_ok=True)

def db_init():
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feed TEXT NOT NULL,
            guid TEXT NOT NULL UNIQUE,
            title TEXT,
            url TEXT,
            published_at TEXT,
            posted_at TEXT
        )""")
        con.commit()

def was_posted(guid: str) -> bool:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("SELECT 1 FROM posts WHERE guid = ?", (guid,))
        return cur.fetchone() is not None

def mark_posted(feed: str, guid: str, title: str, url: str, published_at: str):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("""
        INSERT OR IGNORE INTO posts (feed, guid, title, url, published_at, posted_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (feed, guid, title, url, published_at, datetime.utcnow().isoformat()))
        con.commit()

# ----------------- Helpers de texto -----------------
def normalize_message(s: str) -> str:
    """Convierte '\n' y '\t' literales del .env a saltos reales y limpia espacios."""
    return s.replace("\\n", "\n").replace("\\t", "\t").strip()

# ----------------- Imagen/Media utils -----------------
ASSETS_DIR = "assets"
os.makedirs(ASSETS_DIR, exist_ok=True)
NEWS_PLACEHOLDER = os.path.join(ASSETS_DIR, "news_placeholder.png")

def download_image_to_bytes(url: str, timeout: int = 15) -> bytes:
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return r.content
    except Exception as e:
        log.warning(f"No pude descargar imagen: {url} -> {e}")
        return b""

def fetch_opengraph_image(article_url: str, timeout: int = 15) -> str:
    """Intenta obtener la imagen del artículo leyendo <meta property='og:image'>."""
    try:
        r = requests.get(article_url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        tag = soup.find("meta", attrs={"property": "og:image"}) or soup.find("meta", attrs={"name": "og:image"})
        if tag and tag.get("content"):
            return tag.get("content").strip()
    except Exception as e:
        log.warning(f"OG image fallback failed for {article_url}: {e}")
    return ""

# Traducción
_translator = GoogleTranslator(source="auto", target="es")
def translate_to_spanish(text: str, limit: int = 0) -> str:
    try:
        t = text if not limit or len(text) <= limit else text[:limit]
        return _translator.translate(t)
    except Exception as e:
        log.warning(f"No pude traducir: {e}")
        return text

# ----------------- Placa de “Partidos de hoy” (mejorada) -----------------
def build_daily_schedule_image(events) -> bytes:
    """Genera una imagen 1080x1080 con título, filas alternadas, logos grandes y 'píldora' de hora."""
    W, H = 1080, 1080
    pad, row_h = 48, 150
    max_rows, title_h, footer_h = 6, 180, 70

    bg = Image.new("RGB", (W, H), (12, 14, 22))
    draw = ImageDraw.Draw(bg)

    # Fuentes: usa TTF en assets/fonts si existen; si no, default
    font_title = ImageFont.load_default()
    font_team  = ImageFont.load_default()
    font_time  = ImageFont.load_default()
    font_small = ImageFont.load_default()
    try:
        from pathlib import Path as _Path
        fdir = _Path("assets") / "fonts"
        if (fdir / "Inter-SemiBold.ttf").exists():
            font_title = ImageFont.truetype(str(fdir / "Inter-SemiBold.ttf"), 56)
            font_team  = ImageFont.truetype(str(fdir / "Inter-SemiBold.ttf"), 36)
            font_time  = ImageFont.truetype(str(fdir / "Inter-Medium.ttf"), 34)
            font_small = ImageFont.truetype(str(fdir / "Inter-Regular.ttf"), 24)
    except Exception:
        pass

    # Header
    draw.rectangle([(0, 0), (W, title_h)], fill=(20, 22, 34))
    tz_local = tz.gettz(TIMEZONE)
    now_local = datetime.now(tz_local)
    date_str = now_local.strftime("%A %d %b %Y").title()
    draw.text((pad, 60), "PARTIDOS NBA DE HOY", fill=(245, 246, 255), font=font_title)
    draw.text((pad, 110), date_str, fill=(180, 184, 205), font=font_small)

    # Helpers
    def paste_logo(url, x, y_center, size=112):
        if not url: return
        raw = download_image_to_bytes(url)
        if not raw: return
        try:
            im = Image.open(io.BytesIO(raw)).convert("RGBA")
            im.thumbnail((size, size))
            bg.paste(im, (x, int(y_center - im.height / 2)), im)
        except Exception:
            pass

    # Filas
    y, shown = title_h + 10, 0
    for ev in events[:max_rows]:
        comps = ev.get("competitions", [])
        if not comps: continue
        comp = comps[0]
        teams = comp.get("competitors", [])
        if len(teams) != 2: continue

        home = next((t for t in teams if t.get("homeAway") == "home"), teams[0])
        away = next((t for t in teams if t.get("homeAway") == "away"), teams[-1])

        home_name = home.get("team", {}).get("shortDisplayName") or home.get("team", {}).get("displayName", "Local")
        away_name = away.get("team", {}).get("shortDisplayName") or away.get("team", {}).get("displayName", "Visita")

        def _logo(t):
            return t.get("team", {}).get("logo") or (t.get("team", {}).get("logos", [{}])[0].get("href") if t.get("team", {}).get("logos") else None)

        home_logo, away_logo = _logo(home), _logo(away)

        try:
            tip_local = isoparse(ev.get("date")).astimezone(tz.gettz(TIMEZONE))
            hour_str = tip_local.strftime("%H:%M")
        except Exception:
            hour_str = "--:--"

        top = y + shown * row_h
        bottom = top + row_h - 12
        fill_row = (24, 26, 38) if shown % 2 == 0 else (18, 20, 30)
        draw.rounded_rectangle([(pad, top), (W - pad, bottom)], radius=18, fill=fill_row)

        center_y = (top + bottom) // 2

        paste_logo(away_logo, pad + 20, center_y, size=110)
        paste_logo(home_logo, W - pad - 20 - 110, center_y, size=110)

        draw.text((pad + 150, center_y - 24), f"{away_name}  @  {home_name}",
                  fill=(235, 238, 250), font=font_team)

        # Píldora de hora
        pill_w, pill_h = 150, 44
        pill_x1 = W - pad - 150 - 140
        pill_y1 = center_y - pill_h // 2
        draw.rounded_rectangle([(pill_x1, pill_y1), (pill_x1 + pill_w, pill_y1 + pill_h)],
                               radius=22, fill=(39, 161, 79))
        tw, th = draw.textbbox((0, 0), hour_str, font=font_time)[2:]
        draw.text((pill_x1 + (pill_w - tw) / 2, pill_y1 + (pill_h - th) / 2),
                  hour_str, fill=(255, 255, 255), font=font_time)

        shown += 1

    draw.text((pad, H - footer_h + 20), "Fuente: ESPN Scoreboard",
              fill=(170, 174, 196), font=font_small)

    buf = io.BytesIO()
    bg.save(buf, format="PNG")
    return buf.getvalue()

# ----------------- RSS helpers -----------------
def get_feed_entry_image_url(entry) -> str:
    for key in ("media_content", "media_thumbnail"):
        if key in entry and entry[key]:
            cand = entry[key][0]
            url = cand.get("url")
            if url:
                return url
    for link in entry.get("links", []):
        if link.get("rel") in ("enclosure", "preview") and link.get("type","").startswith("image/"):
            return link.get("href")
    article = entry.get("link", "")
    if article:
        og = fetch_opengraph_image(article)
        if og:
            return og
    return ""

POST_TEMPLATE = os.getenv("POST_TEMPLATE", "").strip() or (
    "📰 {title}\n\n{excerpt}\n\n🔗 {link}\n🕒 {published}\nFuente: {source}"
)

def extract_excerpt(entry, max_chars: int = 350) -> str:
    raw = ""
    try:
        if "content" in entry and entry["content"]:
            raw = entry["content"][0].get("value", "")
        elif "summary_detail" in entry and entry["summary_detail"]:
            raw = entry["summary_detail"].get("value", "")
        else:
            raw = entry.get("summary") or entry.get("description") or ""
    except Exception:
        raw = entry.get("summary") or entry.get("description") or ""

    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) > max_chars:
        cut = text[:max_chars]
        cut = cut.rsplit(" ", 1)[0]
        text = cut + "…"
    return text

def format_entry(feed_name: str, entry) -> Tuple[str, str]:
    title = entry.get("title", "(Sin título)")
    link = entry.get("link", "")
    published = entry.get("published", "") or entry.get("updated", "")
    try:
        if entry.get("published_parsed"):
            dt = datetime(*entry.published_parsed[:6])
            published = dt.strftime("%d %b %Y %H:%M")
    except Exception:
        pass
    excerpt = extract_excerpt(entry)

    title_es = translate_to_spanish(title, limit=200)
    excerpt_es = translate_to_spanish(excerpt, limit=1000)

    text = POST_TEMPLATE.format(title=title_es, excerpt=excerpt_es, link=link, published=published, source=feed_name)
    return text, link

# ----------------- Telegram -----------------
bot = Bot(token=TELEGRAM_BOT_TOKEN)

def post_to_channel(text: str, image_url: str = "", image_bytes: bytes = b""):
    try:
        if image_bytes:
            bot.send_photo(chat_id=TELEGRAM_CHANNEL_ID, photo=image_bytes, caption=text, parse_mode=ParseMode.HTML)
        elif image_url:
            bot.send_photo(chat_id=TELEGRAM_CHANNEL_ID, photo=image_url, caption=text, parse_mode=ParseMode.HTML)
        else:
            bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=False)
    except TelegramError as e:
        log.error(f"Error al publicar en Telegram: {e}")

# ----------------- Lógica principal -----------------
def check_feeds():
    for feed_url in RSS_FEEDS:
        try:
            fp = feedparser.parse(feed_url)
            feed_name = fp.feed.get("title", feed_url)
            for entry in fp.entries[:8]:
                guid = entry.get("id") or entry.get("guid") or entry.get("link")
                if not guid or was_posted(guid):
                    continue
                text, link = format_entry(feed_name, entry)
                img_url = get_feed_entry_image_url(entry)
                if img_url:
                    post_to_channel(text, image_url=img_url)
                else:
                    try:
                        with open(NEWS_PLACEHOLDER, "rb") as ph:
                            post_to_channel(text, image_bytes=ph.read())
                    except Exception:
                        post_to_channel(text)
                mark_posted(feed_url, guid, entry.get("title",""), link, entry.get("published",""))
                time.sleep(2)
        except Exception as e:
            log.exception(f"Error procesando feed {feed_url}: {e}")

def fetch_todays_games_message() -> str:
    try:
        tz_local = tz.gettz(TIMEZONE)
        now_local = datetime.now(tz_local)
        dates_param = now_local.strftime("%Y%m%d")
        resp = requests.get(ESPN_SCOREBOARD_URL, params={"dates": dates_param}, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        events = data.get("events", [])
        if not events:
            return f"{SCHEDULE_HEADER}\n\nNo hay partidos programados hoy."
        lines = [SCHEDULE_HEADER, ""]
        for ev in events:
            comps = ev.get("competitions", [])
            if not comps:
                continue
            comp = comps[0]
            teams = comp.get("competitors", [])
            if len(teams) != 2:
                continue
            home = next((t for t in teams if t.get("homeAway") == "home"), teams[0])
            away = next((t for t in teams if t.get("homeAway") == "away"), teams[-1])
            home_name = home.get("team", {}).get("shortDisplayName") or home.get("team", {}).get("displayName")
            away_name = away.get("team", {}).get("shortDisplayName") or away.get("team", {}).get("displayName")
            date_iso = ev.get("date")
            tip_local = None
            if date_iso:
                try:
                    tip_local = isoparse(date_iso).astimezone(tz_local)
                except Exception:
                    tip_local = None
            hour_str = tip_local.strftime("%H:%M") if tip_local else "Horario a confirmar"
            status = comp.get("status", {}).get("type", {}).get("state", "").lower()
            status_map = {"pre": "Programado", "in": "EN VIVO", "post": "Finalizado"}
            status_txt = status_map.get(status, "").strip()
            line = f"• {away_name} @ {home_name} — {hour_str}" + (f" ({status_txt})" if status_txt else "")
            lines.append(line)
        return "\n".join(lines)
    except Exception:
        return f"{SCHEDULE_HEADER}\n\nNo pude obtener los partidos de hoy."

def post_cta():
    msg = normalize_message(CTA_MESSAGE)  # arregla \n del .env
    if CTA_INCLUDE_SCHEDULE:
        sched_text = fetch_todays_games_message()
        msg = f"{msg}\n\n{sched_text}"
    # Intentar con imagen del calendario
    try:
        tz_local = tz.gettz(TIMEZONE)
        now_local = datetime.now(tz_local)
        dates_param = now_local.strftime("%Y%m%d")
        resp = requests.get(ESPN_SCOREBOARD_URL, params={"dates": dates_param}, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        events = data.get("events", [])
        if events:
            img_bytes = build_daily_schedule_image(events)
            post_to_channel(msg, image_bytes=img_bytes)
            return
    except Exception as e:
        log.warning(f"No pude generar imagen del calendario: {e}")
    post_to_channel(msg)

def main():
    db_init()
    log.info("Iniciando NBA Telegram AutoBot 🤖🏀")
    # Primer barrido al inicio
    check_feeds()

    scheduler = BackgroundScheduler(timezone=TIMEZONE)
    scheduler.add_job(check_feeds, 'interval', minutes=CHECK_EVERY_MINUTES, id="feeds")

    # CTAs 3x/día
    for i, cron in enumerate(CTA_CRONS):
        try:
            trigger = CronTrigger.from_crontab(cron, timezone=TIMEZONE)
            scheduler.add_job(post_cta, trigger, id=f"cta_{i}")
        except Exception as e:
            log.error(f"No pude agregar el cron '{cron}': {e}")

    # Calendario diario (opcional)
    try:
        trigger_daily = CronTrigger.from_crontab(SCHEDULE_DAILY_CRON, timezone=TIMEZONE)
        scheduler.add_job(post_cta, trigger_daily, id="daily_schedule")
    except Exception as e:
        log.error(f"No pude agregar el cron de 'partidos de hoy': {e}")

    scheduler.start()
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        log.info("Cerrando bot...")
        scheduler.shutdown()

if __name__ == "__main__":
    main()
