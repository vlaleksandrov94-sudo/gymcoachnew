#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Личный фитнес-тренер в Telegram.

Логика как у настоящего тренера: 3-недельный мезоцикл с периодизацией.
  Неделя 1 — Мягкий вход (адаптация, умеренный вес, 12-15 повторов)
  Неделя 2 — Объём    (гипертрофия, 10-12 повторов, чуть тяжелее)
  Неделя 3 — Сила     (4-8 повторов, тяжёлый вес, пирамида)
Потом цикл повторяется.

Каждая мышечная группа двигается по фазам НЕЗАВИСИМО — фаза продвигается
только когда ты жмёшь «✅ Выполнил» под выданной тренировкой.

Тренировки собраны из реального чата с тренером (Максим) и распределены
по фазам, плюс добавлены варианты в том же стиле.

Умный режим: бот спрашивает «Как самочувствие?», затем отправляет базовую
тренировку под текущую фазу + твой ответ в Claude (Anthropic API), и Claude
адаптирует тренировку (устал → меньше объёма, болит плечо → замена упражнений,
мало времени → короче). Если ключа Claude нет — бот выдаёт базовую тренировку.

Запуск:
  pip install -r requirements.txt
  export BOT_TOKEN="ТОКЕН_ОТ_BOTFATHER"
  export ANTHROPIC_API_KEY="КЛЮЧ_ОТ_ANTHROPIC"   # необязательно
  python trainer_bot.py
"""

import os
import json
import base64
import logging
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Claude подключается опционально — бот работает и без него
try:
    from anthropic import AsyncAnthropic
    _anthropic_available = True
except ImportError:
    _anthropic_available = False

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Хранилище состояния.
#  Если задан DATABASE_URL (Railway Postgres) — храним в БД (данные не теряются
#  при передеплоях). Иначе — в локальном JSON-файле (удобно для запуска на компе).
#  Снаружи всё работает через load_state()/save_state(), как раньше, поэтому
#  остальной код менять не нужно.
# ---------------------------------------------------------------------------
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "progress.json")
DATABASE_URL = os.environ.get("DATABASE_URL")
_db_pool = None  # ленивая инициализация

# Кэш состояния в памяти, чтобы не дёргать БД на каждый чих.
_state_cache = None


def _db_connect():
    """Создаёт пул соединений к Postgres (psycopg3). Возвращает пул или None."""
    global _db_pool
    if not DATABASE_URL:
        return None
    if _db_pool is not None:
        return _db_pool
    try:
        from psycopg_pool import ConnectionPool
        # Railway даёт URL вида postgres://...; psycopg ждёт postgresql://
        url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        _db_pool = ConnectionPool(url, min_size=1, max_size=3, kwargs={"autocommit": True})
        with _db_pool.connection() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS bot_state ("
                "id INT PRIMARY KEY DEFAULT 1, data JSONB NOT NULL, "
                "CONSTRAINT single_row CHECK (id = 1))"
            )
        logger.info("Хранилище: PostgreSQL (данные сохраняются между деплоями).")
        return _db_pool
    except Exception as e:
        logger.error("Не удалось подключиться к БД (%s). Откат на файл.", e)
        _db_pool = None
        return None


def load_state():
    """Загрузить всё состояние (dict). Источник: БД или файл."""
    global _state_cache
    if _state_cache is not None:
        return _state_cache
    pool = _db_connect()
    if pool is not None:
        try:
            with pool.connection() as conn:
                row = conn.execute("SELECT data FROM bot_state WHERE id = 1").fetchone()
                _state_cache = (row[0] if row else {}) or {}
                return _state_cache
        except Exception as e:
            logger.error("Ошибка чтения из БД (%s). Пробую файл.", e)
    # файловый режим
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            _state_cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _state_cache = {}
    return _state_cache


def save_state(state):
    """Сохранить всё состояние. Назначение: БД или файл."""
    global _state_cache
    _state_cache = state
    pool = _db_connect()
    if pool is not None:
        try:
            with pool.connection() as conn:
                conn.execute(
                    "INSERT INTO bot_state (id, data) VALUES (1, %s) "
                    "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data",
                    (json.dumps(state, ensure_ascii=False),),
                )
            return
        except Exception as e:
            logger.error("Ошибка записи в БД (%s). Пишу в файл.", e)
    # файловый режим
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _group_record(state, uid, group):
    """Запись группы: {"pos": int (0..6 позиция в мезоцикле), "history": [...]}.
    Совместимо со старым форматом (int = номер фазы, либо ключ 'phase')."""
    uid = str(uid)
    state.setdefault(uid, {})
    rec = state[uid].get(group)
    if rec is None:
        rec = {"pos": 0, "history": []}
        state[uid][group] = rec
    elif isinstance(rec, int):  # совсем старый формат: просто число фазы
        rec = {"pos": rec % 3, "history": []}
        state[uid][group] = rec
    # миграция с промежуточного формата, где было поле 'phase'
    if "pos" not in rec:
        rec["pos"] = rec.get("phase", 0) % 3
    rec.setdefault("history", [])
    return rec


# Мезоцикл из 7 позиций: два круга (вход→объём→сила) + разгрузка.
# Индекс позиции -> индекс «типа» фазы (0=вход,1=объём,2=сила,3=разгрузка)
CYCLE_LEN = 7
_POS_TO_PHASE = [0, 1, 2, 0, 1, 2, 3]


def get_phase(user_id, group):
    """Тип текущей фазы: 0=вход, 1=объём, 2=сила, 3=разгрузка."""
    state = load_state()
    rec = _group_record(state, user_id, group)
    return _POS_TO_PHASE[rec["pos"] % CYCLE_LEN]


def get_cycle_pos(user_id, group):
    state = load_state()
    rec = _group_record(state, user_id, group)
    return rec["pos"] % CYCLE_LEN


def is_reassessment(user_id, group):
    """True, если СЕЙЧАС начало нового большого цикла (позиция 0 после разгрузки),
    т.е. пора предложить поднять рабочие веса. Срабатывает после прохождения разгрузки."""
    return get_cycle_pos(user_id, group) == 0 and _was_after_deload(user_id, group)


def _was_after_deload(user_id, group):
    state = load_state()
    rec = _group_record(state, user_id, group)
    return bool(rec.get("after_deload"))


def advance_phase(user_id, group):
    """Сдвинуть позицию мезоцикла на +1. Возвращает (новая_позиция, новый_тип_фазы)."""
    state = load_state()
    rec = _group_record(state, user_id, group)
    old_pos = rec["pos"] % CYCLE_LEN
    new_pos = (rec["pos"] + 1) % CYCLE_LEN
    rec["pos"] = new_pos
    # отметка: только что завершили разгрузку (позиция 6) и встали на 0
    rec["after_deload"] = (old_pos == 6 and new_pos == 0)
    save_state(state)
    return new_pos, _POS_TO_PHASE[new_pos]


def add_history(user_id, group, feeling=None, notes=None):
    """Добавить запись о выполненной тренировке. Храним последние 3."""
    import datetime
    state = load_state()
    rec = _group_record(state, user_id, group)
    entry = {"date": datetime.date.today().isoformat()}
    if feeling:
        entry["feeling"] = feeling
    if notes:
        entry["notes"] = notes
    rec["history"].append(entry)
    rec["history"] = rec["history"][-3:]  # только последние 3
    save_state(state)


def get_history(user_id, group):
    """Список последних тренировок группы (до 3)."""
    state = load_state()
    rec = _group_record(state, user_id, group)
    return rec["history"]


def history_text(user_id, group):
    """Человекочитаемая сводка истории для передачи в Claude."""
    hist = get_history(user_id, group)
    if not hist:
        return "Истории пока нет — это первая записанная тренировка."
    lines = []
    for i, h in enumerate(hist, 1):
        bits = [h.get("date", "")]
        if h.get("feeling"):
            bits.append(f"ощущения: {h['feeling']}")
        if h.get("notes"):
            bits.append(f"веса/заметки: {h['notes']}")
        lines.append(f"{i}) " + "; ".join(bits))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
#  Профиль оборудования зала (один на пользователя)
# ---------------------------------------------------------------------------
def get_equipment(user_id):
    """Список/описание оборудования зала пользователя (строка) или None."""
    state = load_state()
    return state.get(str(user_id), {}).get("_equipment")


def set_equipment(user_id, text):
    state = load_state()
    uid = str(user_id)
    state.setdefault(uid, {})
    state[uid]["_equipment"] = text
    save_state(state)


def equipment_text(user_id):
    """Строка оборудования для передачи в Claude."""
    eq = get_equipment(user_id)
    if not eq:
        return ("Оборудование не задано. Считай, что доступен стандартный тренажёрный зал "
                "(штанги, гантели, базовые тренажёры, блоки/кроссовер).")
    return eq


# ---------------------------------------------------------------------------
#  Профиль атлета (цель, стаж, частота) — один на пользователя
# ---------------------------------------------------------------------------
GOAL_MAP = {
    "mass": "набор мышечной массы",
    "strength": "развитие силы",
    "cut": "сушка / рельеф (снижение жира с сохранением мышц)",
    "endurance": "выносливость / общая физуха",
    "maintain": "поддержание формы",
}
EXP_MAP = {
    "novice": "новичок (меньше года)",
    "inter": "средний уровень (1–3 года стажа)",
    "adv": "опытный (3+ лет стажа)",
}
FREQ_MAP = {
    "2": "2 тренировки в неделю",
    "3": "3 тренировки в неделю",
    "4": "4 тренировки в неделю",
    "5": "5+ тренировок в неделю",
}


def get_profile(user_id):
    """Профиль атлета (dict) или {}."""
    state = load_state()
    return state.get(str(user_id), {}).get("_profile", {})


def set_profile_field(user_id, field, value):
    state = load_state()
    uid = str(user_id)
    state.setdefault(uid, {})
    state[uid].setdefault("_profile", {})
    state[uid]["_profile"][field] = value
    save_state(state)


def profile_complete(user_id):
    p = get_profile(user_id)
    return all(k in p for k in ("goal", "exp", "freq"))


def profile_text(user_id):
    """Человекочитаемый профиль для передачи в Claude."""
    p = get_profile(user_id)
    if not p:
        return "Профиль атлета не заполнен (цель, стаж и частота неизвестны)."
    parts = []
    if p.get("goal"):
        parts.append(f"цель — {GOAL_MAP.get(p['goal'], p['goal'])}")
    if p.get("exp"):
        parts.append(f"уровень — {EXP_MAP.get(p['exp'], p['exp'])}")
    if p.get("freq"):
        parts.append(f"частота — {FREQ_MAP.get(p['freq'], p['freq'])}")
    return "; ".join(parts) if parts else "Профиль атлета не заполнен."


# ---------------------------------------------------------------------------
#  Подготовительный этап (круговые тренировки на всё тело)
#  Длительность зависит от стажа из профиля; по умолчанию 5.
# ---------------------------------------------------------------------------
PREP_TARGET_DEFAULT = 5
PREP_TARGET_BY_EXP = {"novice": 6, "inter": 4, "adv": 2}


def prep_target(user_id):
    """Сколько круговых тренировок нужно пройти на старте (решает 'тренер')."""
    p = get_profile(user_id)
    return PREP_TARGET_BY_EXP.get(p.get("exp"), PREP_TARGET_DEFAULT)


def prep_done(user_id):
    state = load_state()
    return state.get(str(user_id), {}).get("_prep_done", 0)


def prep_active(user_id):
    """True, если атлет ещё в подготовительном этапе (круговые)."""
    return prep_done(user_id) < prep_target(user_id)


def advance_prep(user_id):
    """+1 к счётчику пройденных круговых. Возвращает (done, target, just_finished)."""
    state = load_state()
    uid = str(user_id)
    state.setdefault(uid, {})
    done = state[uid].get("_prep_done", 0) + 1
    state[uid]["_prep_done"] = done
    save_state(state)
    target = prep_target(user_id)
    return done, target, done >= target


# ---------------------------------------------------------------------------
#  Названия фаз
# ---------------------------------------------------------------------------
PHASE_NAMES = [
    "🟢 Мягкий вход (адаптация)",
    "🟡 Объём (гипертрофия)",
    "🔴 Сила (тяжёлый вес)",
    "🌙 Разгрузка (восстановление)",
]

# Базовая круговая тренировка на всё тело для подготовительного этапа.
CIRCUIT_WORKOUT = (
    "*Круговая тренировка (всё тело) — подготовительный этап*\n\n"
    "Цель сейчас — связки, сердце, техника. Работаем по кругу, вес умеренный.\n\n"
    "Сделай *3–4 круга*, между упражнениями отдых 15–30 сек, между кругами 1.5–2 мин:\n\n"
    "1️⃣ Приседания (с собственным весом или лёгкой гирей) — 15\n"
    "2️⃣ Отжимания от пола (можно с колен) — 12\n"
    "3️⃣ Тяга гантели/блока к поясу — 12 на каждую руку\n"
    "4️⃣ Выпады на месте — 10 на каждую ногу\n"
    "5️⃣ Жим гантелей сидя (лёгкие) — 12\n"
    "6️⃣ Планка — 30–40 сек\n"
    "7️⃣ Скручивания на пресс — 15–20\n\n"
    "_Темп ровный, без отказа. Задача — войти в форму, а не убиться._"
)

WARMUP = (
    "🔥 *Разминка (перед каждой тренировкой)*\n"
    "3 раунда:\n"
    "• 15 «лодочка»\n"
    "• 15 коротких скручиваний\n"
    "• 30 сек планка\n"
    "Плюс разогрей суставы той группы, что тренируешь.\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
)

# ---------------------------------------------------------------------------
#  БАЗА ТРЕНИРОВОК
#  Для каждой группы — 3 варианта (по одному на фазу).
#  Тексты собраны/адаптированы из чата с тренером.
# ---------------------------------------------------------------------------
WORKOUTS = {
    "chest": {
        "title": "💪 Грудь + трицепс",
        "phases": [
            # ---- Фаза 0: мягкий вход ----
            "*Грудь + трицепс — мягкий вход*\n\n"
            "1️⃣ Отжимания от пола — 3×14 _(можно на гири/паралеты)_\n"
            "2️⃣ Сведения в кроссовере стоя — 3×14, ладони друг к другу, вес 7,5–10 кг\n"
            "3️⃣ Жим в тренажёре на грудь — 3×12, вес по 20 с каждой, дальше по самочувствию\n"
            "4️⃣ Разведения гантелей под углом — 3×12, вес 12–14 кг, без разворота ладоней\n"
            "5️⃣ Французский жим с гантелями лёжа — 3×14, вес 10 кг\n"
            "6️⃣ Разгибания рук с верхнего блока, канат — 3×12, вес 17,5–20 кг\n\n"
            "_Темп спокойный, чувствуй мышцу, не гонись за весом._",

            # ---- Фаза 1: объём ----
            "*Грудь + трицепс — объём*\n\n"
            "1️⃣ Жим гантелей под углом — 4×12, вес 1/22-2/24-3-4/26 кг\n"
            "2️⃣ Жим в Смите под углом — 4×12, старт 15 кг с каждой, дальше по самочувствию\n"
            "3️⃣ Брусья — 4×8–12 без веса _(легко идёт — повесь блин 5 кг)_\n"
            "4️⃣ Сведения в кроссовере стоя — 4×12, прожимай корпусом, не шатайся\n"
            "5️⃣ Жим узким хватом — 4×12, вес 30 кг, смотри по ощущениям\n"
            "6️⃣ Разгибания из-за головы в кроссовере стоя, канат — 4×12, вес 15–20 кг\n\n"
            "_Всё должно гореть: грудь, плечи, трицепс._",

            # ---- Фаза 2: сила ----
            "*Грудь + трицепс — сила*\n\n"
            "1️⃣ Жим штанги под углом — 5×8, вес 50-60-70-70-70+\n"
            "2️⃣ Жим гантелей на горизонтальной скамье — 4×8, вес 28-30-35+\n"
            "3️⃣ Брусья с весом — 4×8-6-4-4, вес 0-5-10-10 кг\n"
            "4️⃣ Сведения в кроссовере снизу вверх (на верх груди) — 3×12, вес 5+\n"
            "5️⃣ Французский жим со штангой лёжа — 4×12-8, вес 30+ кг\n"
            "6️⃣ Разгибания рук с верхнего блока, ровная ручка — 3×12, вес 20+\n\n"
            "_В конце: 1 подход отжиманий от пола на максимум. Пресс: 2-3×20 подъёмы ног._",
        ],
    },

    "back": {
        "title": "🏋️ Спина + бицепс",
        "phases": [
            # ---- Фаза 0: мягкий вход ----
            "*Спина + бицепс — мягкий вход*\n\n"
            "1️⃣ Подтягивания с резинкой — 4×8, без пауз, полегче резинка\n"
            "2️⃣ Тяга верхнего блока широким хватом к груди — 3×12, вес 40–50 кг\n"
            "3️⃣ Тяга горизонтального блока узким хватом — 3×12, вес 40 кг\n"
            "4️⃣ Пуловер с верхнего блока, канат — 3×14, вес 25+\n"
            "5️⃣ Молотки сидя — 4×12, вес 12 кг\n"
            "6️⃣ Сгибания на бицепс с нижнего блока — 3×12, вес 12+\n\n"
            "_Каждое подтягивание — медленно вниз. Втягиваемся в работу._",

            # ---- Фаза 1: объём ----
            "*Спина + бицепс — объём*\n\n"
            "1️⃣ Подтягивания широким хватом с резинкой — 4×12-10-8-6 _(резинка от сильной к слабой)_\n"
            "2️⃣ Тяга верхнего блока широким хватом — 4×12, вес 50+\n"
            "3️⃣ Тяга гантели в наклоне — 3×12+12, вес 28-35 кг _(можно лямки)_\n"
            "4️⃣ Тяга Т-грифа в отказ — 4×12\n"
            "5️⃣ Гиперэкстензия с блином 10 кг — 3×12\n"
            "6️⃣ Сгибания на бицепс с нижнего блока сидя — 4×12, локти на колени, вес 12+\n"
            "7️⃣ Молотки стоя к середине груди — 3×12+12, сначала одной, вес 12+ кг\n\n"
            "_Пресс любой в конце._",

            # ---- Фаза 2: сила ----
            "*Спина + бицепс — сила*\n\n"
            "1️⃣ Подтягивания широким хватом — 4×4\n"
            "2️⃣ Становая тяга — 5×8-8-4-4-4, вес 50-70-80-90-100 _(последний подход сними на видео)_\n"
            "3️⃣ Тяга штанги в наклоне — 4×8, вес 50-60-70-70\n"
            "4️⃣ Тяга верхнего блока широким хватом — 3×12, вес 1/40-2/50-3/60\n"
            "5️⃣ Тяга гантели в наклоне — 3×12+12, вес 1/22-2/26-3/28 кг\n"
            "6️⃣ Молотки сидя — 4×12, вес 12 кг\n\n"
            "_Тяжёлая база. Техника важнее веса — медленно вниз на становой._",
        ],
    },

    "legs": {
        "title": "🦵 Ноги + плечи",
        "phases": [
            # ---- Фаза 0: мягкий вход ----
            "*Ноги + плечи — мягкий вход*\n\n"
            "Разминка: 3 раунда — 10 гиперэкстензий без веса + 20 подъёмов ног на пресс.\n\n"
            "1️⃣ Голень в тренажёре сидя — 3×25, вес 100+\n"
            "2️⃣ Приседания со штангой — 4×15, вес 1/50-2-3-4/70\n"
            "3️⃣ Жим ногами двумя — 3×15, вес 100+\n"
            "4️⃣ Разгибания ног — 3×12, вес 30-40-50 / Сгибания ног — 3×12, вес 30-40-50\n"
            "5️⃣ Задняя дельта сидя в наклоне, гантели — 3×12, вес 7-9 кг\n"
            "6️⃣ Подъём двух гантелей перед собой, хват молотки — 3×12, вес 7-9 кг\n"
            "7️⃣ Жим гантелей сидя — 3×12, лёгкий вес\n\n"
            "_Гоняем кровь, разогреваем колени, без фанатизма._",

            # ---- Фаза 1: объём ----
            "*Ноги + плечи — объём*\n\n"
            "Разминка по классике + разогрей плечи.\n\n"
            "1️⃣ Голень в тренажёре сидя — 3×20, вес 100\n"
            "2️⃣ Приседания со штангой — 5×12, вес 50-60-70-80-80\n"
            "3️⃣ Good Morning (наклоны со штангой) — 4×10, вес 50-55-60\n"
            "4️⃣ Жим ногами — 4×12, вес 100-120-150+\n"
            "5️⃣ Задняя дельта в наклоне, гантели — 4×14-12-10-10, вес 8-10-12\n"
            "6️⃣ Подъём двух гантелей перед собой, молотки — 4×14-12-10-10, вес 8-10-12\n"
            "7️⃣ Жим гантелей сидя — 4×12-10-8-8, вес 20-22-24+\n"
            "8️⃣ Махи гантелей стоя — 4×12-10-8-8, вес 8-10-12+\n\n"
            "_Пресс в конце._",

            # ---- Фаза 2: сила ----
            "*Ноги + плечи — сила*\n\n"
            "Разминка по классике, хорошо разомни колени.\n\n"
            "1️⃣ Голень — 3×20, вес 100\n"
            "2️⃣ Сгибания ног сидя — 3×15, вес 30-40-50\n"
            "3️⃣ Приседания — 4×8-6-4-2, вес 1/50-2/80-3/90-4/100, +2×15 вес 70 _(добивка)_\n"
            "4️⃣ Выпады на месте с гантелями — 3×20, вес 1/6-2/9-3/12 кг\n"
            "5️⃣ Жим сидя в Смите с груди — 4×12-10-8-8, вес 10-20-30-30+ с каждой стороны\n"
            "6️⃣ Дроп-сет махи гантелей стоя — 3 раунда: 8 повт/9кг → 10 повт/6кг → 12 повт/3кг\n"
            "7️⃣ Тяга штанги к подбородку — 3×14-12, вес 30 кг\n\n"
            "_Тяжёлый день. В конце пресс любой._",
        ],
    },
}

# Распознавание группы по свободному тексту
KEYWORDS = {
    "chest": ["груд", "трицепс", "жим", "грудь", "chest"],
    "back": ["спин", "бицепс", "тяга", "становая", "подтяг", "back"],
    "legs": ["ног", "присед", "плеч", "ноги", "legs", "квадрицепс", "дельт"],
}


def detect_group(text):
    t = text.lower()
    scores = {g: sum(1 for kw in kws if kw in t) for g, kws in KEYWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else None


def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💪 Грудь + трицепс", callback_data="ask:chest")],
        [InlineKeyboardButton("🏋️ Спина + бицепс", callback_data="ask:back")],
        [InlineKeyboardButton("🦵 Ноги + плечи", callback_data="ask:legs")],
        [InlineKeyboardButton("📊 Мой прогресс", callback_data="progress")],
    ])


def workout_keyboard(group):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Выполнил (следующая фаза)", callback_data=f"done:{group}")],
        [InlineKeyboardButton("⬅️ Назад к выбору", callback_data="back_menu")],
    ])


def build_workout_message(user_id, group):
    # Подготовительный этап — всегда круговая на всё тело
    if prep_active(user_id):
        done = prep_done(user_id)
        target = prep_target(user_id)
        header = (f"🔄 *Подготовительный этап* — тренировка {done + 1} из {target}\n"
                  "━━━━━━━━━━━━━━━━━━━━\n\n")
        return WARMUP + header + CIRCUIT_WORKOUT

    phase = get_phase(user_id, group)
    data = WORKOUTS[group]
    header = f"*{data['title']}*\n{PHASE_NAMES[phase]}\n━━━━━━━━━━━━━━━━━━━━\n\n"
    if phase == 3:
        # Разгрузка: берём силовую базу, но помечаем как лёгкую неделю.
        body = (
            "🌙 *Разгрузочная неделя.* Веса 60–70% от рабочих, на 2 повтора меньше, "
            "минус 1 подход в каждом упражнении. Цель — восстановиться, не убиваться.\n\n"
            "За основу — обычная силовая, но облегчённая:\n\n" + data["phases"][2]
        )
        return WARMUP + header + body
    return WARMUP + header + data["phases"][phase]


# Короткие коды для callback_data (Telegram лимит 64 байта; кириллица = 2 байта/символ)
FEELING_MAP = {
    "fresh": "бодрый, полон сил, готов работать тяжело",
    "tired": "уставший, мало спал, сил немного",
    "pain": "есть лёгкая боль/дискомфорт в суставе или мышце",
    "short": "сегодня мало времени, нужна короткая тренировка",
}
RATING_MAP = {
    "easy": "было легко, можно прибавить",
    "ok": "в самый раз",
    "hard": "было тяжело, на пределе",
}


def feeling_keyboard(group):
    """Кнопки самочувствия после выбора группы."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💪 Бодр, полон сил", callback_data=f"feel:{group}:fresh")],
        [InlineKeyboardButton("😮‍💨 Устал / недоспал", callback_data=f"feel:{group}:tired")],
        [InlineKeyboardButton("🤕 Что-то побаливает", callback_data=f"feel:{group}:pain")],
        [InlineKeyboardButton("⏱ Мало времени", callback_data=f"feel:{group}:short")],
        [InlineKeyboardButton("➡️ Без изменений (базовая)", callback_data=f"grp:{group}")],
    ])


def rate_keyboard(group):
    """Оценка прошедшей тренировки для прогрессии весов."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔥 Легко (добавь нагрузку)", callback_data=f"rate:{group}:easy")],
        [InlineKeyboardButton("👌 Нормально", callback_data=f"rate:{group}:ok")],
        [InlineKeyboardButton("😣 Тяжело (сбавь)", callback_data=f"rate:{group}:hard")],
    ])


# ---------------------------------------------------------------------------
#  Адаптация тренировки через Claude (Anthropic)
# ---------------------------------------------------------------------------
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
_claude_client = None
if _anthropic_available and ANTHROPIC_KEY:
    _claude_client = AsyncAnthropic(api_key=ANTHROPIC_KEY)

SYSTEM_PROMPT = (
    "Ты — опытный персональный фитнес-тренер. Тебе дают базовую тренировку "
    "(её составил живой тренер атлета), самочувствие атлета на сегодня и историю "
    "последних тренировок этой группы (даты, ощущения, рабочие веса). "
    "Твоя задача — выдать тренировку на сегодня: адаптировать базовую под самочувствие "
    "И осмысленно прогрессировать нагрузку по истории, сохранив структуру, дух и фазу "
    "(мягкий вход / объём / сила).\n\n"
    "Прогрессия (автоматическая, ты решаешь сам):\n"
    "- Если в прошлый раз было «легко» / атлет бодр — подними рабочий вес на разумный шаг "
    "(обычно +2.5–5 кг на крупных движениях, +1–2 кг на мелких) или добавь повтор/подход.\n"
    "- Если было «тяжело» — оставь тот же вес для закрепления или чуть снизь; не гони.\n"
    "- Если «норм» — закрепи или прибавь совсем чуть-чуть.\n"
    "- Опирайся на конкретные веса из истории, если они есть. Указывай рекомендуемые веса.\n\n"
    "Адаптация под самочувствие:\n"
    "- Устал/недоспал — снизь объём (убери 1-2 подхода), прогрессию веса в этот день не форсируй.\n"
    "- Что-то болит — замени упражнения на проблемную зону на безопасные аналоги, добавь заметку "
    "про осторожность (без диагнозов; при сильной/острой боли советуй не тренировать зону и обратиться к врачу).\n"
    "- Мало времени — укороти до ключевых базовых упражнений (суперсеты ок).\n\n"
    "Оборудование зала:\n"
    "- Тебе дают список оборудования, доступного атлету. Используй ТОЛЬКО доступное.\n"
    "- Если в базовой тренировке есть упражнение на тренажёр/снаряд, которого нет — замени его "
    "равноценным аналогом на доступном оборудовании, сохранив целевую мышцу и характер нагрузки.\n"
    "- Не предлагай упражнения, требующие отсутствующего оборудования.\n\n"
    "Профиль атлета (цель, стаж, частота):\n"
    "- Подстраивай тренировку под цель: масса — умеренные веса и больше объёма (8–12 повт), "
    "сила — тяжелее и меньше повторов (3–6), сушка/рельеф — выше плотность, суперсеты, чуть больше повторов, "
    "выносливость — многоповторка и короткий отдых, поддержание — сбалансированно.\n"
    "- Учитывай стаж: новичку — проще движения, акцент на технику и меньше объёма; опытному — можно "
    "сложнее и интенсивнее.\n"
    "- Учитывай частоту в неделю при выборе объёма за тренировку (реже тренируется — можно чуть больше за раз).\n\n"
    "Разнообразие:\n"
    "- Не копируй прошлую тренировку из истории один-в-один: меняй порядок или часть упражнений "
    "на равноценные в рамках доступного оборудования и текущей фазы. Атлет не должен видеть одно и то же.\n\n"
    "ФОРМАТ ОТВЕТА (строго соблюдай, пиши по-русски, компактно — это читают с телефона в зале):\n"
    "1) Первая строка — заголовок группы и фаза.\n"
    "2) Блок «🎯 Логика дня:» — 1–3 коротких предложения: почему сегодня именно эта фаза, "
    "как она работает на цель атлета, и общий замысел тренировки.\n"
    "3) Список упражнений с эмодзи-нумерацией. Для КАЖДОГО упражнения в одной компактной строке:\n"
    "   • название, подходы×повторы и рекомендуемый вес;\n"
    "   • в скобках коротко — целевая мышца и зачем (например: «верх груди, базовое»);\n"
    "   • пометка оборудования: «✓ есть» если снаряд точно в списке зала, либо «🔄 замена: <вместо чего>» "
    "если ты подобрал аналог из-за отсутствия снаряда.\n"
    "   ВАЖНО: если оборудование НЕ задано (список пуст/общий), не пиши «✓ есть» и «🔄 замена» — "
    "вместо этого одной строкой вверху попроси атлета настроить зал командой /зал, чтобы пометки заработали.\n"
    "4) Блок «⚖️ По весам:» — 1–2 строки: откуда взяты цифры (из истории, прогрессия, % от рабочего на разгрузке).\n"
    "Будь конкретным и без воды. Не раздувай текст — пояснения короткие, по сути."
)


async def adapt_with_claude(base_workout, feeling, phase_name, hist_text, equip_text, prof_text):
    """Просим Claude адаптировать тренировку с учётом истории, оборудования и профиля."""
    if _claude_client is None:
        return None
    user_msg = (
        f"Фаза цикла: {phase_name}\n\n"
        f"Профиль атлета: {prof_text}\n\n"
        f"Самочувствие атлета сегодня: {feeling}\n\n"
        f"Доступное оборудование зала:\n{equip_text}\n\n"
        f"История последних тренировок этой группы:\n{hist_text}\n\n"
        f"Базовая тренировка (адаптируй под профиль и самочувствие, прогрессируй и подгони под оборудование):\n{base_workout}"
    )
    try:
        resp = await _claude_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        return "\n".join(parts).strip() or None
    except Exception as e:
        logger.error("Ошибка вызова Claude: %s", e)
        return None


GYM_VISION_PROMPT = (
    "На фото(графиях) — тренажёрный зал атлета. Перечисли кратко, какое оборудование "
    "видно: свободные веса (штанги, гантели и их диапазон если виден), стойки/рамы, "
    "скамьи (горизонт/наклон), тренажёры (жим, тяга, разгибания/сгибания ног, голень и т.д.), "
    "блоки/кроссовер, турник, брусья, гири, резины и прочее. "
    "Ответь компактным списком на русском, без вступлений — только перечень оборудования. "
    "Если что-то не видно — не выдумывай."
)


async def analyze_gym_photos(images_b64):
    """images_b64: список (media_type, base64). Возвращает текст-список оборудования или None."""
    if _claude_client is None:
        return None
    content = []
    for media_type, b64 in images_b64:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64},
        })
    content.append({"type": "text", "text": GYM_VISION_PROMPT})
    try:
        resp = await _claude_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            messages=[{"role": "user", "content": content}],
        )
        parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        return "\n".join(parts).strip() or None
    except Exception as e:
        logger.error("Ошибка анализа фото: %s", e)
        return None


# ---------------------------------------------------------------------------
#  Хендлеры
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    smart_line = (
        "\n🤖 Я спрошу, как ты себя чувствуешь, и подстрою тренировку под тебя.\n"
        if _claude_client else ""
    )
    text = (
        "Привет! Я твой карманный тренер 🏋️\n\n"
        "Напиши, что у тебя сегодня — например «*грудь и трицепс*», «*спина*» или "
        "«*ноги*» — и я выдам тренировку под твою текущую фазу.\n\n"
        "Я веду 3-недельный цикл для каждой группы отдельно:\n"
        "🟢 Мягкий вход → 🟡 Объём → 🔴 Сила → по кругу.\n"
        f"{smart_line}\n"
        "Жми «✅ Выполнил» после тренировки — и группа перейдёт в следующую фазу.\n\n"
        "🏋️ Команда /зал — настроить оборудование твоего зала (можно фоткой).\n"
        "🧑‍💼 Команда /profile — задать цель, стаж и частоту (тренировки точнее).\n\n"
        "Или выбери кнопкой ниже 👇"
    )
    await update.message.reply_text(text, reply_markup=main_keyboard(), parse_mode="Markdown")


async def gym_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /зал и /gym — настройка оборудования."""
    uid = update.effective_user.id
    current = get_equipment(uid)
    context.user_data["awaiting_gym"] = True
    cur_line = f"\n\n*Сейчас сохранено:*\n{current}" if current else ""
    if _claude_client:
        msg = (
            "🏋️ *Настройка зала*\n\n"
            "Пришли *одно или несколько фото* твоего зала (разные углы, стойки, тренажёры) — "
            "я распознаю оборудование и запомню его.\n\n"
            "Можно вместо фото просто *описать зал текстом* "
            "(например: «штанга, гантели до 40 кг, кроссовер, скамья, турник»).\n\n"
            "Когда закончишь с фото — напиши «*готово*»."
            f"{cur_line}"
        )
    else:
        msg = (
            "🏋️ *Настройка зала*\n\n"
            "Опиши зал текстом — какое оборудование есть "
            "(например: «штанга, гантели до 40 кг, кроссовер, скамья, турник»). Я запомню."
            "\n\n_(Распознавание по фото доступно только с подключённым Claude.)_"
            f"{cur_line}"
        )
    await update.message.reply_text(msg, parse_mode="Markdown")


# ---------------------------------------------------------------------------
#  Профиль атлета: команда и клавиатуры
# ---------------------------------------------------------------------------
def profile_goal_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💪 Масса", callback_data="pg:goal:mass")],
        [InlineKeyboardButton("🏋️ Сила", callback_data="pg:goal:strength")],
        [InlineKeyboardButton("🔥 Сушка / рельеф", callback_data="pg:goal:cut")],
        [InlineKeyboardButton("🏃 Выносливость", callback_data="pg:goal:endurance")],
        [InlineKeyboardButton("⚖️ Поддержание", callback_data="pg:goal:maintain")],
    ])


def profile_exp_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Новичок (<1 года)", callback_data="pg:exp:novice")],
        [InlineKeyboardButton("🟡 Средний (1–3 года)", callback_data="pg:exp:inter")],
        [InlineKeyboardButton("🔴 Опытный (3+ лет)", callback_data="pg:exp:adv")],
    ])


def profile_freq_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("2 в неделю", callback_data="pg:freq:2")],
        [InlineKeyboardButton("3 в неделю", callback_data="pg:freq:3")],
        [InlineKeyboardButton("4 в неделю", callback_data="pg:freq:4")],
        [InlineKeyboardButton("5+ в неделю", callback_data="pg:freq:5")],
    ])


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /profile — анкета атлета."""
    uid = update.effective_user.id
    cur = profile_text(uid)
    cur_line = f"\n\n*Сейчас:* {cur}" if profile_complete(uid) else ""
    await update.message.reply_text(
        "🧑‍💼 *Профиль атлета*\n\n"
        "Заполним 3 коротких пункта — тренировки станут прицельнее под твою цель.\n\n"
        "*Шаг 1 из 3.* Какая у тебя цель?" + cur_line,
        reply_markup=profile_goal_keyboard(),
        parse_mode="Markdown",
    )


async def progress_text(user_id):
    lines = ["📊 *Твой прогресс*\n"]
    if prep_active(user_id):
        lines.append(
            f"🔄 Подготовительный этап: {prep_done(user_id)}/{prep_target(user_id)} круговых\n"
            "(после него откроется силовой сплит)\n"
        )
    lines.append("*По группам:*")
    for g, data in WORKOUTS.items():
        phase = get_phase(user_id, g)
        pos = get_cycle_pos(user_id, g)
        lines.append(f"\n{data['title']}")
        lines.append(f"  Фаза: {PHASE_NAMES[phase]} (шаг {pos+1}/{CYCLE_LEN})")
        hist = get_history(user_id, g)
        if hist:
            last = hist[-1]
            bits = [last.get("date", "")]
            if last.get("feeling"):
                bits.append(last["feeling"])
            if last.get("notes"):
                bits.append(f"веса: {last['notes']}")
            lines.append("  Последняя: " + "; ".join(bits))
        else:
            lines.append("  Последняя: ещё не тренировался")
    return "\n".join(lines)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text_in = update.message.text.strip()

    # Слово «зал» (или /зал) запускает настройку оборудования
    if not context.user_data.get("awaiting_gym") and text_in.lower().lstrip("/") in ("зал", "оборудование"):
        await gym_command(update, context)
        return

    if not context.user_data.get("awaiting_gym") and text_in.lower().lstrip("/") in ("профиль", "profile", "анкета"):
        await profile_command(update, context)
        return

    # Режим настройки зала текстом
    if context.user_data.get("awaiting_gym"):
        low = text_in.lower()
        if low in ("готово", "done", "ок", "ok", "всё", "все"):
            context.user_data.pop("awaiting_gym", None)
            pending_eq = context.user_data.pop("gym_pending", None)
            if pending_eq:
                set_equipment(uid, pending_eq)
                await update.message.reply_text(
                    "✅ Зал сохранён! Буду подбирать упражнения под него.\n\n"
                    f"*Оборудование:*\n{pending_eq}",
                    reply_markup=main_keyboard(), parse_mode="Markdown",
                )
            else:
                await update.message.reply_text(
                    "Ок, ничего не изменил. 👇", reply_markup=main_keyboard()
                )
            return
        # иначе — это текстовое описание зала
        set_equipment(uid, text_in)
        context.user_data.pop("awaiting_gym", None)
        context.user_data.pop("gym_pending", None)
        await update.message.reply_text(
            "✅ Записал оборудование зала. Учту при подборе упражнений. 👇",
            reply_markup=main_keyboard(),
        )
        return

    # Если ждём рабочие веса после оценки тренировки — записываем их в историю
    pending = context.user_data.get("awaiting_notes_group")
    if pending and detect_group(text_in) is None:
        state = load_state()
        rec = _group_record(state, uid, pending)
        if rec["history"]:
            rec["history"][-1]["notes"] = text_in
        save_state(state)
        context.user_data.pop("awaiting_notes_group", None)
        await update.message.reply_text(
            "📝 Записал веса — учту в следующей тренировке этой группы. 👇",
            reply_markup=main_keyboard(),
        )
        return

    group = detect_group(text_in)
    if group is None:
        await update.message.reply_text(
            "Не понял группу 🤔 Напиши «грудь», «спина» или «ноги», либо выбери кнопкой:",
            reply_markup=main_keyboard(),
        )
        return
    context.user_data.pop("awaiting_notes_group", None)
    title = WORKOUTS[group]["title"]
    smart = " Подстрою тренировку под тебя 🤖" if _claude_client else ""
    await update.message.reply_text(
        f"{title}\n\nКак самочувствие сегодня?{smart}",
        reply_markup=feeling_keyboard(group),
    )


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приём фото зала — распознаём оборудование через Claude."""
    uid = update.effective_user.id
    if not context.user_data.get("awaiting_gym"):
        await update.message.reply_text(
            "Если хочешь настроить зал по фото — сначала отправь команду /зал, потом фото 🏋️"
        )
        return
    if _claude_client is None:
        await update.message.reply_text(
            "Распознавание по фото работает только с подключённым Claude. "
            "Опиши зал текстом, пожалуйста."
        )
        return

    # Берём фото наибольшего размера
    photo = update.message.photo[-1]
    tg_file = await photo.get_file()
    buf = await tg_file.download_as_bytearray()
    b64 = base64.b64encode(bytes(buf)).decode("ascii")

    await update.message.reply_text("🔍 Смотрю фото, распознаю оборудование…")
    result = await analyze_gym_photos([("image/jpeg", b64)])
    if not result:
        await update.message.reply_text(
            "Не получилось распознать 😕 Попробуй другое фото или опиши зал текстом."
        )
        return

    # Накапливаем распознанное (можно несколько фото подряд)
    prev = context.user_data.get("gym_pending")
    merged = result if not prev else prev + "\n" + result
    context.user_data["gym_pending"] = merged
    await update.message.reply_text(
        "📋 Распознал на этом фото:\n\n" + result +
        "\n\nМожешь прислать ещё фото или написать «готово», чтобы сохранить.\n"
        "Если что-то не так — просто опиши зал текстом, и я заменю.",
    )


async def send_long(query, context, text, reply_markup):
    """Показать длинный текст: первая часть в текущем сообщении, остаток — отдельными.
    Клавиатура крепится к последнему сообщению. Telegram лимит ~4096 символов."""
    LIMIT = 3800
    # Разбиваем по абзацам, чтобы не рвать посреди строки
    chunks = []
    cur = ""
    for para in text.split("\n\n"):
        piece = (para + "\n\n")
        if len(cur) + len(piece) > LIMIT and cur:
            chunks.append(cur.rstrip())
            cur = piece
        else:
            cur += piece
    if cur.strip():
        chunks.append(cur.rstrip())
    if not chunks:
        chunks = [text[:LIMIT]]

    async def _send(target_edit, body, markup):
        try:
            if target_edit:
                await query.edit_message_text(body, reply_markup=markup, parse_mode="Markdown")
            else:
                await context.bot.send_message(
                    chat_id=query.message.chat_id, text=body,
                    reply_markup=markup, parse_mode="Markdown")
        except Exception:
            # запасной путь без Markdown (на случай проблемной разметки)
            if target_edit:
                await query.edit_message_text(body, reply_markup=markup)
            else:
                await context.bot.send_message(
                    chat_id=query.message.chat_id, text=body, reply_markup=markup)

    for i, chunk in enumerate(chunks):
        first = (i == 0)
        last = (i == len(chunks) - 1)
        await _send(first, chunk, reply_markup if last else None)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = update.effective_user.id

    if data.startswith("ask:"):
        group = data.split(":", 1)[1]
        title = WORKOUTS[group]["title"]
        smart = " Подстрою тренировку под тебя 🤖" if _claude_client else ""
        await query.edit_message_text(
            f"{title}\n\nКак самочувствие сегодня?{smart}",
            reply_markup=feeling_keyboard(group),
        )

    elif data.startswith("grp:"):
        group = data.split(":", 1)[1]
        # Прямой показ базовой тренировки (кнопка "без изменений")
        msg = build_workout_message(uid, group)
        await query.edit_message_text(
            msg, reply_markup=workout_keyboard(group), parse_mode="Markdown"
        )

    elif data.startswith("feel:"):
        _, group, feel_code = data.split(":", 2)
        feeling = FEELING_MAP.get(feel_code, feel_code)
        in_prep = prep_active(uid)
        if in_prep:
            phase_label = f"🔄 Подготовительный этап ({prep_done(uid)+1}/{prep_target(uid)})"
        else:
            phase_label = PHASE_NAMES[get_phase(uid, group)]
        base = build_workout_message(uid, group)
        if _claude_client is None:
            # Нет ключа Claude — выдаём базовую
            await query.edit_message_text(
                base, reply_markup=workout_keyboard(group), parse_mode="Markdown"
            )
            return
        await query.edit_message_text("🤖 Подбираю тренировку под твоё самочувствие…")
        hist = history_text(uid, "_circuit" if in_prep else group)
        equip = equipment_text(uid)
        prof = profile_text(uid)
        adapted = await adapt_with_claude(base, feeling, phase_label, hist, equip, prof)
        if adapted:
            title = "🔄 Круговая (всё тело)" if in_prep else WORKOUTS[group]["title"]
            text = (
                f"*{title}* · {phase_label}\n"
                f"🤖 _Адаптировано под самочувствие_\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n" + adapted
            )
            await send_long(query, context, text, workout_keyboard(group))
        else:
            await query.edit_message_text(
                base + "\n\n_(умную адаптацию сделать не удалось — вот базовая)_",
                reply_markup=workout_keyboard(group),
                parse_mode="Markdown",
            )

    elif data.startswith("done:"):
        group = data.split(":", 1)[1]
        title = WORKOUTS[group]["title"]
        await query.edit_message_text(
            f"💪 {title} — как прошло?\n\n"
            "Оцени, чтобы я вёл прогрессию весов 👇\n"
            "_(потом можешь дописать веса текстом, например «жим 70, присед 100»)_",
            reply_markup=rate_keyboard(group),
            parse_mode="Markdown",
        )

    elif data.startswith("rate:"):
        _, group, rate_code = data.split(":", 2)
        feeling = RATING_MAP.get(rate_code, rate_code)
        context.user_data["awaiting_notes_group"] = group

        # Подготовительный этап: двигаем счётчик круговых, а не мезоцикл
        if prep_active(uid):
            add_history(uid, "_circuit", feeling=feeling)
            done, target, finished = advance_prep(uid)
            if finished:
                await query.edit_message_text(
                    f"✅ Круговая {done}/{target} засчитана!\n\n"
                    "🎉 *Подготовительный этап пройден!* Связки и техника готовы — "
                    "перехожу на силовой сплит с периодизацией.\n\n"
                    "Теперь пиши группу мышц (грудь / спина / ноги) — и я дам "
                    "целевую тренировку под фазу. 👇",
                    reply_markup=main_keyboard(), parse_mode="Markdown",
                )
            else:
                await query.edit_message_text(
                    f"✅ Круговая {done}/{target} засчитана!\n\n"
                    f"Ещё {target - done} подготовительных — и переходим к силовому сплиту. "
                    "Можешь дописать веса текстом. Возвращайся за следующей 👇",
                    reply_markup=main_keyboard(), parse_mode="Markdown",
                )
            return

        # Обычный мезоцикл
        add_history(uid, group, feeling=feeling)
        new_pos, new_phase = advance_phase(uid, group)
        title = WORKOUTS[group]["title"]
        extra = ""
        if new_phase == 3:
            extra = "\n\n🌙 Дальше — *разгрузочная неделя*: восстановимся перед новым витком."
        elif is_reassessment(uid, group):
            extra = ("\n\n📈 *Новый цикл!* Разгрузка позади — на следующих тренировках "
                     "пробуй поднять рабочие веса на шаг вверх. Окрепли — пора прибавлять.")
        await query.edit_message_text(
            f"✅ Записал! {title} — {feeling}.\n"
            f"Следующий раз будет: *{PHASE_NAMES[new_phase]}*{extra}\n\n"
            "Можешь прислать рабочие веса текстом — учту в следующий раз. "
            "Или просто возвращайся за новой тренировкой 👇",
            reply_markup=main_keyboard(),
            parse_mode="Markdown",
        )

    elif data == "progress":
        await query.edit_message_text(
            await progress_text(uid), reply_markup=main_keyboard(), parse_mode="Markdown"
        )

    elif data.startswith("pg:"):
        _, field, value = data.split(":", 2)
        set_profile_field(uid, field, value)
        if field == "goal":
            await query.edit_message_text(
                "🧑‍💼 *Профиль атлета*\n\n*Шаг 2 из 3.* Твой уровень/стаж?",
                reply_markup=profile_exp_keyboard(), parse_mode="Markdown",
            )
        elif field == "exp":
            await query.edit_message_text(
                "🧑‍💼 *Профиль атлета*\n\n*Шаг 3 из 3.* Сколько тренировок в неделю?",
                reply_markup=profile_freq_keyboard(), parse_mode="Markdown",
            )
        elif field == "freq":
            await query.edit_message_text(
                "✅ *Профиль сохранён!*\n\n" + profile_text(uid) +
                "\n\nТеперь тренировки будут подбираться под твою цель. "
                "Напиши группу мышц или жми кнопку 👇",
                reply_markup=main_keyboard(), parse_mode="Markdown",
            )

    elif data == "back_menu":
        await query.edit_message_text(
            "Выбери группу мышц 👇", reply_markup=main_keyboard()
        )


def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit(
            "❌ Не задан BOT_TOKEN.\n"
            "Получи токен у @BotFather и запусти:\n"
            '  export BOT_TOKEN="твой_токен"\n'
            "  python trainer_bot.py"
        )

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("progress", lambda u, c: u.message.reply_text("Используй /start")))
    app.add_handler(CommandHandler("gym", gym_command))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    logger.info("Бот запущен. Жду сообщений...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
