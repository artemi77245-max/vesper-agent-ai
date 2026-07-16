"""
Отрисовка интерфейса Vesper в терминале — в стиле Claude Code.

Рисует:
- приветственную панель с рамкой и двумя колонками (слева маскот и инфо,
  справа подсказки и "что нового");
- статус-строку;
- обрамлённое поле ввода.

Ширину подстраиваем под терминал, ANSI-цвета не учитываем в длине строк.
"""

import atexit
import itertools
import os
import re
import shutil
import sys
import threading
import time

try:
    import readline  # редактирование строки и история по стрелкам (Linux/mac)
except ImportError:  # на Windows модуля нет — не критично
    readline = None

from mascot import FOX

# ── Цвета (сумеречная палитра Vesper) ───────────────────────────────
VIOLET = "\033[38;5;99m"   # фиолетовый — рамки и маскот
GRAY = "\033[38;5;245m"    # серый — второстепенный текст
DIM = "\033[38;5;240m"     # тусклый — разделители
YELLOW = "\033[38;5;229m"  # мягкий жёлтый — акценты
GREEN = "\033[38;5;114m"   # зелёный — успех
RED = "\033[38;5;203m"     # красный — ошибка
CYAN = "\033[38;5;80m"     # голубой — прогресс шагов
BOLD = "\033[1m"
RESET = "\033[0m"

# Символы для рамок
TL, TR, BL, BR = "╭", "╮", "╰", "╯"
H, V = "─", "│"

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _visible_len(text: str) -> int:
    """Длина строки без учёта ANSI-кодов цвета."""
    return len(_ANSI_RE.sub("", text))


def _pad(text: str, width: int, align: str = "left") -> str:
    """Дополняет строку пробелами до нужной ширины (учитывая ANSI)."""
    gap = width - _visible_len(text)
    if gap <= 0:
        return text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    if align == "right":
        return " " * gap + text
    return text + " " * gap


def _term_width() -> int:
    """Ширина терминала, ограниченная разумными рамками."""
    return max(80, min(shutil.get_terminal_size((100, 24)).columns, 108))


def render_welcome(name: str, model: str, workspace: str, resumed: bool) -> str:
    """Собирает приветственную панель с двумя колонками."""
    total = _term_width()
    # Разбивка ширины: борта и разделители съедают 7 символов
    # "│ " + left + " │ " + right + " │"
    inner = total - 7
    left_w = inner * 2 // 5      # левая колонка уже
    right_w = inner - left_w

    # ── Левая колонка: приветствие, маскот, инфо ──
    fox_lines = [ln for ln in FOX.splitlines() if ln.strip()]
    left = []
    left.append(_pad(f"{BOLD}{VIOLET}С возвращением, {name}!{RESET}", left_w, "center"))
    left.append("")
    for fl in fox_lines:
        left.append(_pad(f"{VIOLET}{fl}{RESET}", left_w, "center"))
    left.append("")
    left.append(_pad(f"{GRAY}Свет в сумерках кода{RESET}", left_w, "center"))

    # ── Правая колонка: подсказки и что нового ──
    right = []
    right.append(f"{BOLD}{YELLOW}С чего начать{RESET}")
    right.append(f"{GRAY}Опиши задачу — Vesper напишет и запустит код.{RESET}")
    right.append(f"{GRAY}Напиши /help, чтобы увидеть все команды.{RESET}")
    right.append(f"{DIM}{H * right_w}{RESET}")
    right.append(f"{BOLD}{YELLOW}Возможности{RESET}")
    right.append(f"{GRAY}Читает, пишет и ищет по коду.{RESET}")
    right.append(f"{GRAY}Спрашивает перед опасными командами.{RESET}")
    right.append(f"{GRAY}Помнит контекст между запусками.{RESET}")

    # Выравниваем число строк в колонках
    rows = max(len(left), len(right))
    left += [""] * (rows - len(left))
    right += [""] * (rows - len(right))

    # ── Верхняя рамка с заголовком ──
    title = f"{BOLD}{VIOLET}Vesper Agent{RESET}"
    prefix = f"{VIOLET}{TL}{H * 3} {RESET}{title}{VIOLET} "
    dashes = total - _visible_len(prefix) - 1
    lines = [f"{prefix}{H * dashes}{TR}{RESET}"]

    # ── Строки с колонками ──
    for l, r in zip(left, right):
        row = (
            f"{VIOLET}{V}{RESET} "
            + _pad(l, left_w)
            + f" {VIOLET}{V}{RESET} "
            + _pad(r, right_w)
            + f" {VIOLET}{V}{RESET}"
        )
        lines.append(row)

    # ── Нижняя рамка ──
    lines.append(f"{VIOLET}{BL}{H * (total - 2)}{BR}{RESET}")

    # ── Строка статуса под панелью ──
    status = (
        f"{GRAY}модель:{RESET} {VIOLET}{model}{RESET}   "
        f"{GRAY}папка:{RESET} {GRAY}{workspace}{RESET}"
    )
    lines.append("")
    lines.append(status)
    if resumed:
        lines.append(f"{DIM}продолжаю прошлую сессию (история загружена){RESET}")

    return "\n".join(lines)


# ── Закреплённая шапка (scroll region, как в nano) ──────────────────
# Идея: резервируем верхние N строк под панель с лисёнком, а всё
# остальное (диалог и ввод) держим в области ниже. Терминал сам
# прокручивает только нижнюю область — шапка остаётся на месте.

_header_height = 0  # сколько строк занимает закреплённая шапка

# История ввода между запусками (стрелка вверх поднимает прошлые команды)
_HISTORY_FILE = os.path.join(
    os.getenv("VESPER_WORKSPACE", "./workspace"), ".vesper_input_history"
)


def _term_lines() -> int:
    """Высота терминала в строках."""
    return shutil.get_terminal_size((100, 24)).lines


def enter_pinned(header: str) -> None:
    """
    Рисует шапку вверху экрана и закрепляет её: ниже задаётся область
    прокрутки, где и живёт весь диалог. Шапка больше не уезжает вверх.
    """
    global _header_height
    lines = header.split("\n")
    _header_height = len(lines)
    rows = _term_lines()

    sys.stdout.write("\033[2J\033[H")          # очистить экран, курсор домой
    sys.stdout.write(header + "\n")            # нарисовать шапку сверху
    top = _header_height + 1                   # первая строка под шапкой
    sys.stdout.write(f"\033[{top};{rows}r")    # область прокрутки: top..низ
    sys.stdout.write(f"\033[{top};1H")         # курсор в начало области
    sys.stdout.flush()


def redraw_header(header: str) -> None:
    """Перерисовывает шапку на месте (например, после смены модели)."""
    lines = header.split("\n")
    sys.stdout.write("\0337")                  # сохранить позицию курсора
    for i, line in enumerate(lines, start=1):
        sys.stdout.write(f"\033[{i};1H\033[2K{line}")  # строка i, очистить, текст
    sys.stdout.write("\0338")                  # вернуть курсор обратно
    sys.stdout.flush()


def leave_pinned() -> None:
    """Снимает закрепление — возвращает терминал в обычное состояние."""
    rows = _term_lines()
    sys.stdout.write("\033[r")                 # сбросить область прокрутки
    sys.stdout.write(f"\033[{rows};1H\n")      # курсор вниз
    sys.stdout.flush()


def init_input_history() -> None:
    """Подключает readline: стрелки, редактирование, история между сессиями."""
    if readline is None:
        return
    try:
        readline.read_history_file(_HISTORY_FILE)
    except (OSError, FileNotFoundError):
        pass
    readline.set_history_length(1000)
    atexit.register(_save_input_history)


def _save_input_history() -> None:
    if readline is None:
        return
    try:
        readline.write_history_file(_HISTORY_FILE)
    except OSError:
        pass


def ask_user() -> str:
    """Разделитель + приглашение ❯. Ввод редактируется стрелками (readline)."""
    print(f"{DIM}{H * _term_width()}{RESET}")
    return input(f"{VIOLET}❯ {RESET}")


# ── Спиннер «Vesper думает…» ────────────────────────────────────────
class Spinner:
    """
    Анимированный индикатор в отдельном потоке. Запрос к модели блокирующий,
    поэтому крутим анимацию параллельно, а по готовности — стираем строку.
    Использование:  with Spinner("Vesper думает"): <долгий вызов>
    """

    FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, text: str = "Vesper думает"):
        self.text = text
        self._stop = threading.Event()
        self._thread = None

    def _spin(self):
        for frame in itertools.cycle(self.FRAMES):
            if self._stop.is_set():
                break
            sys.stdout.write(f"\r{VIOLET}{frame}{RESET} {GRAY}{self.text}…{RESET}")
            sys.stdout.flush()
            time.sleep(0.08)

    def __enter__(self):
        # Анимируем только в настоящем терминале (не при перенаправлении)
        if sys.stdout.isatty():
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join()
        # Стираем строку спиннера целиком
        sys.stdout.write("\r\033[2K")
        sys.stdout.flush()


# ── Красивое оформление вызовов инструментов ────────────────────────
# Иконка и человекочитаемое имя для каждого инструмента.
_TOOL_META = {
    "read_file": ("◇", "Читаю файл"),
    "write_file": ("◆", "Пишу файл"),
    "list_files": ("▤", "Смотрю папку"),
    "make_dir": ("▨", "Создаю папку"),
    "search_code": ("⌕", "Ищу по коду"),
    "run_command": ("»", "Выполняю команду"),
}


def _tool_arg_hint(name: str, args: dict) -> str:
    """Короткая подпись к вызову: путь/команда/запрос — что важнее для инструмента."""
    for key in ("path", "command", "query", "directory"):
        if key in args and args[key]:
            return str(args[key])
    return ""


def print_tool_call(step: int, max_steps: int, name: str, args: dict) -> None:
    """Красивый блок вызова инструмента со счётчиком шага и иконкой."""
    icon, label = _TOOL_META.get(name, ("•", name))
    hint = _tool_arg_hint(name, args)
    step_tag = f"{CYAN}[шаг {step}/{max_steps}]{RESET}"
    line = f"  {step_tag} {icon} {VIOLET}{label}{RESET}"
    if hint:
        line += f" {GRAY}{hint}{RESET}"
    print(line)


def print_tool_result(result: str, is_error: bool = False) -> None:
    """Компактный итог работы инструмента: первая строка + отметка успеха/ошибки."""
    first = (result or "").strip().splitlines()
    preview = first[0] if first else ""
    if len(preview) > 80:
        preview = preview[:77] + "…"
    if is_error:
        print(f"     {RED}✗ {preview}{RESET}")
    else:
        extra = f" {DIM}(+{len(first) - 1} строк){RESET}" if len(first) > 1 else ""
        print(f"     {GREEN}✓{RESET} {GRAY}{preview}{RESET}{extra}")


# ── Консольный UI-адаптер (запасной, без curses) ────────────────────
# Реализует тот же интерфейс, что и полноэкранный CursesUI, но через print.
# Используется, если терминал не поддерживает полноэкранный режим.
class ConsoleUI:
    def call_model(self, fn):
        with Spinner("Vesper думает"):
            return fn()

    def tool_call(self, step, max_steps, name, args):
        print_tool_call(step, max_steps, name, args)

    def tool_result(self, text, is_error=False):
        print_tool_result(text, is_error=is_error)

    def assistant(self, text):
        print(f"\n{VIOLET}Vesper:{RESET} {text}\n")

    def error(self, text):
        print(f"\n{RED}{text}{RESET}\n")

    def info(self, text):
        print(f"{GRAY}{text}{RESET}")

    def user(self, text):
        pass  # в консоли ввод уже виден на экране

    def refresh_header(self):
        pass  # в консоли шапка не закреплена

    def confirm(self, command: str) -> bool:
        print(f"\n{RED}⚠ Vesper хочет выполнить потенциально опасную команду:{RESET}")
        print(f"{YELLOW}  {command}{RESET}")
        answer = input(f"{RED}Разрешить? (y/n): {RESET}").strip().lower()
        return answer in ("y", "yes", "д", "да")
