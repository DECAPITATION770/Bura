"""
╔══════════════════════════════════════════════╗
║       БУРА — Telegram Bot  (aiogram 3)       ║
║  2–4 игрока | один экран на игрока (edit)    ║
╚══════════════════════════════════════════════╝

Принцип:
  • Каждый игрок видит ОДНО сообщение — оно редактируется при каждом событии.
  • /new /join /leave удаляются сразу.
  • Временные уведомления об ошибках — в answer(show_alert=True), не в чат.
  • Никаких новых сообщений в ходе игры — только edit_message_text.
"""

import asyncio
import logging
import random
import string
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"          # ← вставь сюда токен
# ──────────────────────────────────────────────────────────────────────────────


# ══════════════════════════════════════════════
#  КАРТЫ
# ══════════════════════════════════════════════
SUITS  = ["♠", "♥", "♦", "♣"]
RANKS  = ["6", "7", "8", "9", "10", "J", "Q", "K", "A"]
POINTS = {"A": 11, "10": 10, "K": 4, "Q": 3, "J": 2,
          "9": 0,  "8": 0,   "7": 0, "6": 0}
RANK_ORDER = {r: i for i, r in enumerate(RANKS)}

Card = tuple[str, str]   # ("A", "♠")

def make_deck() -> list[Card]:
    deck = [(r, s) for s in SUITS for r in RANKS]
    random.shuffle(deck)
    return deck

def cs(card: Card) -> str:
    """Card string: 'A♠'"""
    return f"{card[0]}{card[1]}"

def cp(card: Card) -> int:
    return POINTS[card[0]]


# ══════════════════════════════════════════════
#  ИГРОК
# ══════════════════════════════════════════════
class Player:
    def __init__(self, uid: int, name: str):
        self.uid        = uid
        self.name       = name
        self.hand:  list[Card] = []
        self.round_pts: int    = 0   # очки за текущий раунд
        self.total_pts: int    = 0   # суммарные очки
        self.screen_mid: Optional[int] = None   # message_id экрана
        self.selected:  Optional[int]  = None   # выбранный индекс карты

    def ready(self) -> bool:
        return self.screen_mid is not None


# ══════════════════════════════════════════════
#  ИГРА
# ══════════════════════════════════════════════
WIN_SCORE = 31

class Game:
    def __init__(self, sid: str, host: Player, max_players: int):
        self.sid         = sid
        self.max_players = max_players          # 2, 3 или 4
        self.players:  list[Player] = [host]
        self.deck:     list[Card]   = []
        self.trump:    Optional[Card]   = None
        self.trump_s:  Optional[str]    = None  # козырная масть
        self.table:    list[tuple[Card, int]] = []  # [(card, player_idx)]
        self.cur_idx:  int  = 0
        self.phase:    str  = "lobby"   # lobby|setup|playing|round_over|game_over
        self.round_n:  int  = 0
        self.log:      list[str] = []   # последние события

    # ── участники ──────────────────────────────
    def full(self) -> bool:
        return len(self.players) >= self.max_players

    def get(self, uid: int) -> Optional[Player]:
        return next((p for p in self.players if p.uid == uid), None)

    def host(self) -> Player:
        return self.players[0]

    def add(self, uid: int, name: str) -> bool:
        if self.full() or self.get(uid):
            return False
        self.players.append(Player(uid, name))
        return True

    def remove(self, uid: int):
        self.players = [p for p in self.players if p.uid != uid]

    # ── раунд ──────────────────────────────────
    def start_round(self):
        self.round_n  += 1
        self.deck      = make_deck()
        self.table     = []
        self.cur_idx   = 0
        self.phase     = "playing"
        for p in self.players:
            p.hand      = []
            p.round_pts = 0
            p.selected  = None
        self._deal(3)
        # козырь — следующая карта, кладём в низ колоды
        self.trump   = self.deck.pop()
        self.trump_s = self.trump[1]
        self.deck.insert(0, self.trump)
        self.log = [f"🃏 Раунд {self.round_n}. Козырь: {cs(self.trump)}"]

    def _deal(self, n: int):
        for p in self.players:
            for _ in range(n):
                if self.deck:
                    p.hand.append(self.deck.pop())

    def cur(self) -> Player:
        return self.players[self.cur_idx]

    # ── ход ────────────────────────────────────
    def play(self, uid: int, idx: int) -> tuple[bool, str]:
        p = self.get(uid)
        if not p:
            return False, "Ты не в игре"
        if self.cur().uid != uid:
            return False, "Сейчас не твой ход"
        if idx < 0 or idx >= len(p.hand):
            return False, "Нет такой карты"

        card = p.hand[idx]

        # Проверка правила масти
        if self.table:
            led = self.table[0][0][1]
            has_led   = any(c[1] == led           for c in p.hand)
            has_trump = any(c[1] == self.trump_s  for c in p.hand)
            is_led    = card[1] == led
            is_trump  = card[1] == self.trump_s

            if has_led and not is_led and not is_trump:
                return False, f"Нужно ходить в масть {led} или козырем!"
            if not has_led and has_trump and not is_trump:
                return False, "Нет своей масти — нужно козырять!"

        p.hand.pop(idx)
        p.selected = None
        self.table.append((card, self.cur_idx))
        self.log.append(f"▶ {p.name}: {cs(card)}")

        if len(self.table) == len(self.players):
            self._resolve()
        else:
            self.cur_idx = (self.cur_idx + 1) % len(self.players)

        return True, ""

    def _resolve(self):
        led_s = self.table[0][0][1]
        win_i = 0
        win_c = self.table[0][0]

        for card, pidx in self.table[1:]:
            wt = win_c[1] == self.trump_s
            ct = card[1] == self.trump_s
            if ct and not wt:
                win_i, win_c = pidx, card
            elif ct == wt and card[1] == win_c[1]:
                if RANK_ORDER[card[0]] > RANK_ORDER[win_c[0]]:
                    win_i, win_c = pidx, card

        winner = self.players[win_i]
        pts = sum(cp(c) for c, _ in self.table)
        winner.round_pts += pts
        self.log.append(f"🏆 {winner.name} берёт взятку! +{pts} (итого {winner.round_pts})")

        self.table    = []
        self.cur_idx  = win_i

        # Добор карт
        need = min(len(p.hand) for p in self.players)
        if need < 3 and len(self.deck) >= len(self.players):
            add = min(3 - need, len(self.deck) // len(self.players))
            if add > 0:
                self._deal(add)
                self.log.append(f"📦 Добрали по {add}. В колоде: {len(self.deck)}")

        # Руки пусты — раунд окончен
        if all(len(p.hand) == 0 for p in self.players):
            self._end_round()

    def _end_round(self):
        self.phase = "round_over"
        self.log.append("━━━━━━━━━━━━━━━━━━━")
        self.log.append(f"Раунд {self.round_n} окончен")
        for p in self.players:
            p.total_pts += p.round_pts
            self.log.append(f"  {p.name}: +{p.round_pts} → ∑{p.total_pts}")

        winner = next((p for p in self.players if p.total_pts >= WIN_SCORE), None)
        if winner:
            self.phase = "game_over"
            self.log.append(f"🎉 {winner.name} победил!")

    # ── рендер ─────────────────────────────────
    def _log_tail(self, n=4) -> str:
        return "\n".join(f"  {l}" for l in self.log[-n:])

    def _score_table(self) -> str:
        lines = []
        for i, p in enumerate(self.players):
            arrow = "▶" if i == self.cur_idx and self.phase == "playing" else " "
            bar   = "█" * (p.total_pts * 10 // WIN_SCORE)
            lines.append(f"{arrow} {p.name:<12} {p.total_pts:>2}/{WIN_SCORE}  {bar}")
        return "\n".join(lines)

    def render_lobby(self) -> tuple[str, InlineKeyboardMarkup]:
        plist = "\n".join(
            f"  {'👑' if i==0 else '👤'} {p.name}"
            for i, p in enumerate(self.players)
        )
        text = (
            f"🎴 *БУРА* | Лобби\n"
            f"Сессия: `{self.sid}`\n"
            f"Макс. игроков: *{self.max_players}*\n\n"
            f"Участники ({len(self.players)}/{self.max_players}):\n"
            f"{plist}\n\n"
            f"Поделись кодом сессии с друзьями.\n"
            f"Хост нажимает ▶️ когда все готовы."
        )
        btns: list[list[InlineKeyboardButton]] = []
        if self.players[0].uid:   # хост увидит кнопку у себя
            if len(self.players) >= 2:
                btns.append([InlineKeyboardButton(
                    text=f"▶️  Начать игру ({len(self.players)} игрока/ов)",
                    callback_data="start_game"
                )])
            btns.append([InlineKeyboardButton(
                text="❌  Отменить сессию",
                callback_data="cancel_game"
            )])
        return text, InlineKeyboardMarkup(inline_keyboard=btns)

    def render(self, p: Player) -> tuple[str, InlineKeyboardMarkup]:
        """Один экран для одного игрока."""
        is_my_turn = (
            self.phase == "playing"
            and self.cur().uid == p.uid
        )
        trump_str = f"{cs(self.trump)} {self.trump_s}" if self.trump else "—"

        # ── шапка ──
        lines = [
            f"🎴 *БУРА*  |  `{self.sid}`",
            f"Раунд {self.round_n}  •  Козырь: {trump_str}  •  Колода: {len(self.deck)}",
            "",
        ]

        # ── стол ──
        if self.table:
            tbl = "  ".join(f"{cs(c)}({self.players[pi].name})" for c, pi in self.table)
            lines.append(f"🪣 Стол: {tbl}")
        else:
            lines.append("🪣 Стол: пусто")

        # ── счёт ──
        lines.append("")
        lines.append("```")
        lines.append(self._score_table())
        lines.append("```")
        lines.append("")

        # ── рука ──
        if p.hand:
            hand_parts = []
            for i, c in enumerate(p.hand):
                label = cs(c)
                if p.selected == i:
                    label = f"[{label}]"
                hand_parts.append(f"{i+1}:{label}")
            lines.append("🃏 Твои карты: " + "  ".join(hand_parts))
        else:
            lines.append("🃏 Твои карты: нет")

        # ── статус ──
        lines.append("")
        if self.phase == "playing":
            if is_my_turn:
                if p.selected is not None:
                    lines.append(f"👆 Выбрана: *{cs(p.hand[p.selected])}* — нажми ▶️ Сыграть или выбери другую")
                else:
                    lines.append("👆 Твой ход! Выбери карту ниже.")
            else:
                lines.append(f"⏳ Ждём хода *{self.cur().name}*...")
        elif self.phase == "round_over":
            lines.append("⏸ Раунд окончен.")
        elif self.phase == "game_over":
            lines.append("🏁 Игра завершена!")

        # ── лог ──
        lines.append("")
        lines.append(self._log_tail(4))

        text = "\n".join(lines)

        # ── кнопки ──────────────────────────────────────────────────────────
        btns: list[list[InlineKeyboardButton]] = []

        if self.phase == "playing" and is_my_turn:
            # Ряды карт (по 3 в ряд)
            row: list[InlineKeyboardButton] = []
            for i, c in enumerate(p.hand):
                label = f"✅ {cs(c)}" if p.selected == i else cs(c)
                row.append(InlineKeyboardButton(
                    text=label,
                    callback_data=f"sel:{i}"
                ))
                if len(row) == 3:
                    btns.append(row); row = []
            if row:
                btns.append(row)

            # Кнопка «Сыграть»
            if p.selected is not None:
                btns.append([InlineKeyboardButton(
                    text=f"▶️  Сыграть {cs(p.hand[p.selected])}",
                    callback_data=f"play:{p.selected}"
                )])

        elif self.phase == "playing":
            # Не твой ход — карты показаны в тексте, кнопок нет
            btns.append([InlineKeyboardButton(
                text="🔄 Обновить экран",
                callback_data="refresh"
            )])

        elif self.phase == "round_over":
            # Только хост запускает новый раунд (чтобы все успели прочитать)
            if p.uid == self.host().uid:
                btns.append([InlineKeyboardButton(
                    text="🔄 Новый раунд",
                    callback_data="new_round"
                )])
            else:
                btns.append([InlineKeyboardButton(
                    text="⏳ Ждём хоста...",
                    callback_data="noop"
                )])

        elif self.phase == "game_over":
            btns.append([InlineKeyboardButton(
                text="🏠 В главное меню",
                callback_data="main_menu"
            )])

        return text, InlineKeyboardMarkup(inline_keyboard=btns)


# ══════════════════════════════════════════════
#  СОСТОЯНИЕ
# ══════════════════════════════════════════════
sessions:     dict[str, Game] = {}   # sid → Game
user_session: dict[int, str]  = {}   # uid → sid


def gen_sid() -> str:
    chars = string.ascii_uppercase + string.digits
    while True:
        sid = "".join(random.choices(chars, k=5))
        if sid not in sessions:
            return sid


# ══════════════════════════════════════════════
#  ЭКРАН (send / edit)
# ══════════════════════════════════════════════
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()


async def send_screen(game: Game, player: Player):
    """Первая отправка экрана. Запоминаем message_id."""
    if game.phase == "lobby":
        text, kb = game.render_lobby()
    else:
        text, kb = game.render(player)
    try:
        msg = await bot.send_message(
            player.uid, text,
            reply_markup=kb,
            parse_mode="Markdown"
        )
        player.screen_mid = msg.message_id
    except Exception as e:
        logger.error(f"send_screen uid={player.uid}: {e}")


async def edit_screen(game: Game, player: Player):
    """Редактируем существующий экран. Если нет — отправляем заново."""
    if player.screen_mid is None:
        await send_screen(game, player)
        return
    if game.phase == "lobby":
        text, kb = game.render_lobby()
    else:
        text, kb = game.render(player)
    try:
        await bot.edit_message_text(
            text,
            chat_id=player.uid,
            message_id=player.screen_mid,
            reply_markup=kb,
            parse_mode="Markdown"
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            logger.warning(f"edit_screen uid={player.uid}: {e}")
    except Exception as e:
        logger.error(f"edit_screen uid={player.uid}: {e}")


async def edit_all(game: Game):
    """Обновляем экраны всех игроков."""
    await asyncio.gather(*(edit_screen(game, p) for p in game.players))


async def close_screen(uid: int, mid: int, text: str = "❌ Сессия закрыта."):
    """Финализируем экран без кнопок."""
    try:
        await bot.edit_message_text(
            text, chat_id=uid, message_id=mid,
            reply_markup=None, parse_mode="Markdown"
        )
    except:
        pass


# ══════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ
# ══════════════════════════════════════════════
async def safe_delete(msg: Message):
    try:
        await msg.delete()
    except:
        pass


def leave_session(uid: int):
    """Убирает игрока из его текущей сессии."""
    sid = user_session.pop(uid, None)
    if not sid or sid not in sessions:
        return
    game = sessions[sid]
    game.remove(uid)
    if not game.players:
        del sessions[sid]


# ══════════════════════════════════════════════
#  КОМАНДЫ
# ══════════════════════════════════════════════
@dp.message(Command("start", "help"))
async def cmd_help(msg: Message):
    await safe_delete(msg)
    text = (
        "🎴 *БУРА* — карточная игра для 2–4 игроков\n\n"
        "*Правила:*\n"
        "  • 36 карт (6–Туз, 4 масти)\n"
        "  • Козырь бьёт любую масть\n"
        "  • Обязан ходить в масть / козырять\n"
        "  • А=11 · 10=10 · К=4 · Д=3 · В=2\n"
        "  • Первый до *31 очка* — победитель\n\n"
        "*Команды:*\n"
        "  /new — создать сессию\n"
        "  /join КОД — войти в сессию\n"
        "  /leave — покинуть сессию"
    )
    tmp = await msg.answer(text, parse_mode="Markdown")
    await asyncio.sleep(20)
    try:
        await tmp.delete()
    except:
        pass


@dp.message(Command("new"))
async def cmd_new(msg: Message):
    await safe_delete(msg)
    uid  = msg.from_user.id
    name = msg.from_user.full_name[:20]

    # Выбор числа игроков — через лобби; по умолчанию 3
    # Сначала уточним кол-во через кнопки прямо в экране лобби
    leave_session(uid)

    sid  = gen_sid()
    host = Player(uid, name)
    game = Game(sid, host, max_players=3)   # можно изменить кнопкой в лобби
    sessions[sid]    = game
    user_session[uid] = sid

    await send_screen(game, host)


@dp.message(Command("join"))
async def cmd_join(msg: Message):
    await safe_delete(msg)
    uid  = msg.from_user.id
    name = msg.from_user.full_name[:20]
    parts = msg.text.strip().split(maxsplit=1)

    if len(parts) < 2:
        tmp = await msg.answer("❗ Укажи код: `/join XXXXX`", parse_mode="Markdown")
        await asyncio.sleep(6); 
        try: await tmp.delete()
        except: pass
        return

    sid = parts[1].strip().upper()
    game = sessions.get(sid)

    if not game:
        tmp = await msg.answer("❌ Сессия не найдена.")
        await asyncio.sleep(6); 
        try: await tmp.delete()
        except: pass
        return
    if game.phase != "lobby":
        tmp = await msg.answer("❌ Игра уже началась.")
        await asyncio.sleep(6); 
        try: await tmp.delete()
        except: pass
        return
    if game.full():
        tmp = await msg.answer("❌ Сессия заполнена.")
        await asyncio.sleep(6); 
        try: await tmp.delete()
        except: pass
        return

    leave_session(uid)
    ok = game.add(uid, name)
    if not ok:
        return

    user_session[uid] = sid
    player = game.get(uid)
    await send_screen(game, player)
    # Обновляем лобби у остальных
    for p in game.players:
        if p.uid != uid and p.ready():
            await edit_screen(game, p)


@dp.message(Command("leave"))
async def cmd_leave(msg: Message):
    await safe_delete(msg)
    uid  = msg.from_user.id
    sid  = user_session.get(uid)
    if not sid:
        return

    game = sessions.get(sid)
    if game:
        player = game.get(uid)
        if player and player.screen_mid:
            await close_screen(uid, player.screen_mid, "👋 Ты покинул игру.")

    leave_session(uid)

    if game and game.players:
        await edit_all(game)


# ══════════════════════════════════════════════
#  CALLBACKS
# ══════════════════════════════════════════════
def get_game_player(uid: int) -> tuple[Optional[Game], Optional[Player]]:
    sid  = user_session.get(uid)
    game = sessions.get(sid) if sid else None
    p    = game.get(uid) if game else None
    return game, p


@dp.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


@dp.callback_query(F.data == "refresh")
async def cb_refresh(cb: CallbackQuery):
    game, player = get_game_player(cb.from_user.id)
    if game and player:
        await edit_screen(game, player)
    await cb.answer("Обновлено")


# ── Лобби: выбор кол-ва игроков ──────────────
@dp.callback_query(F.data.startswith("set_max:"))
async def cb_set_max(cb: CallbackQuery):
    game, player = get_game_player(cb.from_user.id)
    if not game or not player:
        await cb.answer(); return
    if game.host().uid != cb.from_user.id:
        await cb.answer("Только хост", show_alert=True); return
    game.max_players = int(cb.data.split(":")[1])
    await cb.answer(f"Макс. игроков: {game.max_players}")
    await edit_all(game)


# ── Старт игры ────────────────────────────────
@dp.callback_query(F.data == "start_game")
async def cb_start(cb: CallbackQuery):
    game, player = get_game_player(cb.from_user.id)
    if not game or not player:
        await cb.answer(); return
    if game.host().uid != cb.from_user.id:
        await cb.answer("Только хост может начать", show_alert=True); return
    if len(game.players) < 2:
        await cb.answer("Нужно минимум 2 игрока", show_alert=True); return

    game.start_round()
    await cb.answer("Игра началась!")
    await edit_all(game)


# ── Отмена сессии ─────────────────────────────
@dp.callback_query(F.data == "cancel_game")
async def cb_cancel(cb: CallbackQuery):
    game, player = get_game_player(cb.from_user.id)
    if not game:
        await cb.answer(); return
    if game.host().uid != cb.from_user.id:
        await cb.answer("Только хост", show_alert=True); return

    sid = game.sid
    for p in game.players:
        if p.screen_mid:
            await close_screen(p.uid, p.screen_mid, "❌ Сессия отменена хостом.")
        user_session.pop(p.uid, None)
    del sessions[sid]
    await cb.answer("Сессия отменена")


# ── Выбор карты ───────────────────────────────
@dp.callback_query(F.data.startswith("sel:"))
async def cb_select(cb: CallbackQuery):
    game, player = get_game_player(cb.from_user.id)
    if not game or not player:
        await cb.answer(); return
    if game.phase != "playing" or game.cur().uid != cb.from_user.id:
        await cb.answer("Не твой ход!", show_alert=True); return

    idx = int(cb.data.split(":")[1])
    if idx >= len(player.hand):
        await cb.answer(); return

    # Тоггл
    player.selected = None if player.selected == idx else idx
    label = f"Выбрана: {cs(player.hand[idx])}" if player.selected == idx else "Выбор снят"
    await cb.answer(label)
    await edit_screen(game, player)   # обновляем только свой экран


# ── Сыграть карту ─────────────────────────────
@dp.callback_query(F.data.startswith("play:"))
async def cb_play(cb: CallbackQuery):
    game, player = get_game_player(cb.from_user.id)
    if not game or not player:
        await cb.answer(); return
    if game.phase != "playing":
        await cb.answer(); return

    idx = int(cb.data.split(":")[1])
    ok, err = game.play(cb.from_user.id, idx)
    if not ok:
        await cb.answer(err, show_alert=True); return

    await cb.answer()
    await edit_all(game)


# ── Новый раунд ───────────────────────────────
@dp.callback_query(F.data == "new_round")
async def cb_new_round(cb: CallbackQuery):
    game, player = get_game_player(cb.from_user.id)
    if not game or not player:
        await cb.answer(); return
    if game.phase != "round_over":
        await cb.answer(); return
    if game.host().uid != cb.from_user.id:
        await cb.answer("Только хост запускает новый раунд", show_alert=True); return

    game.start_round()
    await cb.answer("Новый раунд!")
    await edit_all(game)


# ── Главное меню ──────────────────────────────
@dp.callback_query(F.data == "main_menu")
async def cb_menu(cb: CallbackQuery):
    uid = cb.from_user.id
    game, player = get_game_player(uid)
    if game and player and player.screen_mid:
        await close_screen(uid, player.screen_mid,
            "🎴 *БУРА*\n\n/new — новая игра\n/join КОД — войти в сессию")
    leave_session(uid)
    await cb.answer()


# ══════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════
async def main():
    logger.info("Бура бот запущен")
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
