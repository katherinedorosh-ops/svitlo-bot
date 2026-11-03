"""
Світло-бот: стежить за графіком стабілізаційних відключень ДТЕК КРЕМ
для однієї адреси й надсилає сповіщення в Телеграм.

Запускається автоматично через GitHub Actions (файл .github/workflows/svitlo.yml).
Адреса, токен бота й chat ID зберігаються в секретах GitHub, а не в коді.
"""

import asyncio
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

SITE_URL = "https://www.dtek-krem.com.ua/ua/shutdowns"
KYIV = ZoneInfo("Europe/Kyiv")
STATE_FILE = "state.json"

# За скільки часу до події бот уже може надіслати нагадування.
# GitHub запускає перевірку приблизно раз на 15 хвилин (іноді із запізненням),
# тому вікно трохи більше за 30 хвилин.
REMIND_WINDOW = timedelta(minutes=45)

# Після скількох невдалих спроб поспіль (кожна ~15 хв) попередити тебе.
FAIL_ALERT_AFTER = 4

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

WEEKDAYS = ["понеділок", "вівторок", "середа", "четвер", "пʼятниця", "субота", "неділя"]

# JavaScript, що виконується всередині сторінки ДТЕК: робить той самий запит,
# який робить сайт, коли ти обираєш адресу у формі.
FETCH_JS = """
async ({city, street}) => {
  const tokenMeta = document.querySelector('meta[name="csrf-token"]');
  const ajaxMeta = document.querySelector('meta[name="ajaxUrl"]');
  const ajaxUrl = ajaxMeta ? ajaxMeta.content : '/ua/ajax';
  const pad = n => String(n).padStart(2, '0');
  const now = new Date();
  const stamp = `${pad(now.getDate())}.${pad(now.getMonth() + 1)}.${now.getFullYear()} ${pad(now.getHours())}:${pad(now.getMinutes())}`;
  const body = new URLSearchParams();
  body.append('method', 'getHomeNum');
  body.append('data[0][name]', 'city');
  body.append('data[0][value]', city);
  body.append('data[1][name]', 'street');
  body.append('data[1][value]', street);
  body.append('data[2][name]', 'updateFact');
  body.append('data[2][value]', stamp);
  const response = await fetch(ajaxUrl, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
      'X-Requested-With': 'XMLHttpRequest',
      'X-CSRF-Token': tokenMeta ? tokenMeta.content : '',
    },
    body,
  });
  let pageFact = null;
  try {
    const ds = (typeof DisconSchedule !== 'undefined') ? DisconSchedule : window.DisconSchedule;
    if (ds && ds.fact) pageFact = JSON.parse(JSON.stringify(ds.fact));
  } catch (e) {}
  return {status: response.status, text: await response.text(), pageFact};
}
"""


def log(*args):
    print(*args, flush=True)


# ---------- Налаштування ----------

def read_config():
    cfg = {
        "token": os.environ.get("TELEGRAM_TOKEN", "").strip(),
        "chat_id": os.environ.get("CHAT_ID", "").strip(),
        "city": os.environ.get("DTEK_CITY", "").strip(),
        "street": os.environ.get("DTEK_STREET", "").strip(),
        "house": os.environ.get("DTEK_HOUSE", "").strip(),
        "group": os.environ.get("DTEK_GROUP", "").strip(),
    }
    required = [("TELEGRAM_TOKEN", "token"), ("CHAT_ID", "chat_id"),
                ("DTEK_CITY", "city"), ("DTEK_STREET", "street"), ("DTEK_HOUSE", "house")]
    missing = [name for name, key in required if not cfg[key]]
    if missing:
        log("ПОМИЛКА: не заповнені секрети:", ", ".join(missing))
        sys.exit(1)
    return cfg


# ---------- Телеграм ----------

def send(cfg, text):
    data = urllib.parse.urlencode({"chat_id": cfg["chat_id"], "text": text}).encode()
    url = f"https://api.telegram.org/bot{cfg['token']}/sendMessage"
    try:
        with urllib.request.urlopen(url, data=data, timeout=30) as response:
            response.read()
        return True
    except Exception as error:
        log("Не вдалося надіслати повідомлення в Телеграм:", error)
        return False


# ---------- Пам'ять бота між запусками ----------

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


# ---------- Сайт ДТЕК ----------

def fact_from_html(html):
    match = re.search(r"DisconSchedule\.fact\s*=\s*", html)
    if not match:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(html[match.end():])
        return obj
    except ValueError:
        return None


async def fetch_from_site(cfg):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                locale="uk-UA", timezone_id="Europe/Kyiv",
                user_agent=USER_AGENT, viewport={"width": 1366, "height": 900},
            )
            page = await context.new_page()
            await page.goto(SITE_URL, wait_until="domcontentloaded", timeout=60_000)
            try:
                await page.wait_for_selector('meta[name="csrf-token"]', state="attached", timeout=45_000)
            except Exception:
                log("Сторінка ДТЕК не завантажилась як слід (можливо, спрацював захист від ботів).")
                log("Заголовок сторінки:", await page.title())
                log("Початок сторінки:", (await page.content())[:1000])
                raise RuntimeError("не вдалося відкрити сторінку графіків")
            await page.wait_for_timeout(2000)
            result = await page.evaluate(FETCH_JS, {"city": cfg["city"], "street": cfg["street"]})
            html = await page.content()
        finally:
            await browser.close()
    return result, html


def parse_response(cfg, result, html):
    log("Відповідь сайту, статус:", result.get("status"))
    raw = result.get("text") or ""
    try:
        resp = json.loads(raw)
    except ValueError:
        log("Сайт відповів не так, як очікувалось:", raw[:500])
        raise RuntimeError("незрозуміла відповідь сайту")
    if not isinstance(resp, dict):
        raise RuntimeError("незрозуміла відповідь сайту")
    log("Ключі у відповіді:", list(resp.keys()))

    houses = resp.get("data")
    if not isinstance(houses, dict):
        houses = {}
    if not houses:
        log("Сайт не повернув жодного будинку. Перевір, чи DTEK_CITY і DTEK_STREET "
            "написані точно так, як на сайті.")

    house = houses.get(cfg["house"])
    if house is None:
        wanted = cfg["house"].replace(" ", "").lower()
        for number, info in houses.items():
            if number.replace(" ", "").lower() == wanted:
                house = info
                break
    if house is None and houses:
        log(f"Будинок не знайдено. Сайт знає {len(houses)} номерів, наприклад: {list(houses)[:20]}")
    house = house if isinstance(house, dict) else {}

    group = cfg["group"]
    if group:
        if not group.upper().startswith("GPV"):
            group = "GPV" + group
    else:
        reasons = house.get("sub_type_reason") or []
        if isinstance(reasons, str):
            reasons = [reasons]
        group = reasons[0] if reasons else ""

    fact = resp.get("fact") or result.get("pageFact") or fact_from_html(html)
    return group, fact, house


# ---------- Обробка графіка ----------

def day_slots(hours):
    """48 символів, кожен — пів години: '1' = світла немає, '0' = світло є."""
    slots = ""
    for hour in range(1, 25):
        value = str(hours.get(str(hour), "yes"))
        if value in ("no", "maybe"):
            slots += "11"
        elif value in ("first", "mfirst"):
            slots += "10"
        elif value in ("second", "msecond"):
            slots += "01"
        else:
            slots += "00"
    return slots


def extract_days(fact, group):
    days = {}
    for timestamp, groups in (fact.get("data") or {}).items():
        if not isinstance(groups, dict):
            continue
        hours = groups.get(group)
        if hours:
            day = datetime.fromtimestamp(int(timestamp), KYIV).date()
            days[day.isoformat()] = day_slots(hours)
    return days


def intervals(slots):
    found, i = [], 0
    while i < len(slots):
        if slots[i] == "1":
            j = i
            while j < len(slots) and slots[j] == "1":
                j += 1
            found.append((i, j))
            i = j
        else:
            i += 1
    return found


def slot_time(i):
    return f"{i // 2:02d}:{'30' if i % 2 else '00'}"


def describe_day(day_iso, slots):
    day = date.fromisoformat(day_iso)
    head = f"{WEEKDAYS[day.weekday()].capitalize()}, {day:%d.%m}"
    blocks = intervals(slots)
    if not blocks:
        return f"{head}: відключень за графіком немає ✅"
    lines = [f"{head}:"]
    lines += [f"🔴 {slot_time(a)}–{slot_time(b)}" for a, b in blocks]
    hours_off = sum(b - a for a, b in blocks) / 2
    lines.append(f"Без світла разом: {hours_off:g} год".replace(".", ","))
    return "\n".join(lines)


def timeline(days):
    """Усі відключення як (початок, кінець) з датою й часом, разом через північ."""
    result = []
    for day_iso in sorted(days):
        base = datetime.combine(date.fromisoformat(day_iso), time(0), tzinfo=KYIV)
        for a, b in intervals(days[day_iso]):
            start = base + timedelta(minutes=30 * a)
            end = base + timedelta(minutes=30 * b)
            if result and result[-1][1] == start:
                result[-1] = (result[-1][0], end)
            else:
                result.append((start, end))
    return result


def house_notice(house):
    kind = str(house.get("sub_type") or "").strip()
    start = str(house.get("start_date") or "").strip()
    end = str(house.get("end_date") or "").strip()
    parts = []
    if kind:
        parts.append(kind)
    if start:
        parts.append(f"Початок: {start}")
    if end:
        parts.append(f"Орієнтовне відновлення: {end}")
    return "\n".join(parts)


# ---------- Головна логіка ----------

def main():
    cfg = read_config()
    state = load_state()
    now = datetime.now(KYIV)
    today = now.date().isoformat()
    ever_worked = "days" in state

    try:
        result, html = asyncio.run(fetch_from_site(cfg))
        group, fact, house = parse_response(cfg, result, html)
        if not group:
            raise RuntimeError("не вдалося визначити чергу. Її можна вписати вручну "
                               "в секрет DTEK_GROUP (наприклад 3.1)")
        if not isinstance(fact, dict) or not fact.get("data"):
            raise RuntimeError("на сторінці не знайшлося графіка")
        fresh_days = extract_days(fact, group)
    except Exception as error:
        state["fails"] = state.get("fails", 0) + 1
        log("ПОМИЛКА:", error)
        if ever_worked and state["fails"] == FAIL_ALERT_AFTER:
            send(cfg, "⚠️ Вже близько години не вдається отримати графік із сайту ДТЕК. "
                      "Перевір його, будь ласка, вручну. Я продовжую пробувати.")
        save_state(state)
        sys.exit(0 if ever_worked else 1)

    if state.get("fails", 0) >= FAIL_ALERT_AFTER:
        send(cfg, "✅ Знову бачу сайт ДТЕК, стежу за графіком далі.")
    state["fails"] = 0

    queue = group.replace("GPV", "")
    old_days = state.get("days", {})
    days = {d: s for d, s in {**old_days, **fresh_days}.items() if d >= today}
    notice = house_notice(house)
    messages = []

    if not ever_worked:
        text = f"👋 Привіт! Я стежу за графіком для твоєї адреси. Твоя черга: {queue}."
        if days:
            text += "\n\n" + "\n\n".join(describe_day(d, days[d]) for d in sorted(days))
        else:
            text += "\n\nЗараз графіка відключень немає ✅"
        if notice:
            text += "\n\nℹ️ ДТЕК повідомляє:\n" + notice
        messages.append(text)
    else:
        old_group = state.get("group")
        if old_group and old_group != group:
            messages.append(f"ℹ️ Твоя черга змінилася: тепер {queue} "
                            f"(була {old_group.replace('GPV', '')}).")
        for d in sorted(fresh_days):
            if d < today:
                continue
            if d not in old_days:
                title = "📅 Зʼявився графік на завтра" if d > today else "📅 Зʼявився графік на сьогодні"
                messages.append(f"{title}\n\n{describe_day(d, fresh_days[d])}")
            elif old_days[d] != fresh_days[d]:
                messages.append(f"🔄 Графік змінився!\n\n{describe_day(d, fresh_days[d])}")
        if notice and notice != state.get("notice"):
            messages.append("ℹ️ ДТЕК повідомляє для твоєї адреси:\n" + notice)

    sent = state.get("sent", {})
    for start, end in timeline(days):
        key_off = "off " + start.isoformat()
        if start - REMIND_WINDOW <= now < start and key_off not in sent:
            minutes = max(1, round((start - now).total_seconds() / 60))
            messages.append(f"⚠️ Приблизно через {minutes} хв, о {start:%H:%M}, за графіком вимкнуть світло.\n"
                            f"Орієнтовно повернеться о {end:%H:%M}.")
            sent[key_off] = now.isoformat()
        key_on = "on " + end.isoformat()
        if max(start, end - REMIND_WINDOW) <= now < end + timedelta(minutes=15) and key_on not in sent:
            messages.append(f"💡 Близько {end:%H:%M} за графіком має зʼявитися світло.")
            sent[key_on] = now.isoformat()
    cutoff = now - timedelta(days=3)
    sent = {k: v for k, v in sent.items() if datetime.fromisoformat(v) >= cutoff}

    state.update({"group": group, "days": days, "sent": sent, "notice": notice})
    save_state(state)

    for message in messages:
        send(cfg, message)
    log(f"Готово. Черга {queue}, днів у графіку: {len(days)}, надіслано повідомлень: {len(messages)}")


if __name__ == "__main__":
    main()
