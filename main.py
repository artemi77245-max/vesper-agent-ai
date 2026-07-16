"""
Точка входа Vesper — запуск агента в терминале.

Запуск:  python main.py

По умолчанию открывается полноэкранный интерфейс (curses): панель с
маскотом закреплена сверху, лента диалога прокручивается колёсиком, поле
ввода и статус — снизу. Если терминал не поддерживает полноэкранный режим
(например, вывод перенаправлен в файл) — работает простой консольный режим.
Выход — команда 'exit' или Ctrl+C.
"""

import getpass
import os
import sys

from dotenv import load_dotenv

# Загружаем .env ДО импорта agent/tools, чтобы переменные (ключ, модель,
# рабочая папка) уже были доступны на момент их чтения в тех модулях.
load_dotenv()

from agent import get_model, run_agent, set_model
from session import clear_history, load_history, save_history
from tools import WORKSPACE, set_confirm_handler, set_password_handler
from ui import ConsoleUI, RESET, VIOLET, render_welcome

HELP_TEXT = (
    "Доступные команды:\n"
    "  /model <имя>  — сменить модель (например /model "
    "meta-llama/llama-3.3-70b-instruct:free)\n"
    "  /model        — показать текущую модель\n"
    "  /reset        — очистить историю диалога\n"
    "  /help         — показать эту справку\n"
    "  exit          — выйти"
)


def _user_name() -> str:
    """Имя для приветствия: из .env (VESPER_USER) или системное."""
    return os.getenv("VESPER_USER") or getpass.getuser() or "друг"


def handle_command(cmd: str, state: dict, ui) -> None:
    """Обрабатывает команды, начинающиеся с '/'. Меняет state на месте."""
    parts = cmd.split(maxsplit=1)
    name = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if name == "/help":
        ui.info(HELP_TEXT)
    elif name == "/model":
        if arg:
            set_model(arg)
            ui.info(f"Модель изменена на: {arg}")
            ui.refresh_header()
        else:
            ui.info(f"Текущая модель: {get_model()}")
    elif name == "/reset":
        clear_history()
        state["history"] = []
        ui.info("История очищена.")
    else:
        ui.info("Неизвестная команда. Напиши /help.")


def process_input(text: str, state: dict, ui) -> bool:
    """
    Обрабатывает один ввод пользователя. Возвращает True, если пора выходить.
    Общая логика для обоих интерфейсов (полноэкранного и консольного).
    """
    if text.lower() in ("exit", "quit", "выход"):
        return True
    if text.startswith("/"):
        handle_command(text, state, ui)
        save_history(state["history"])
        return False
    state["history"] = run_agent(text, state["history"], ui)
    save_history(state["history"])
    return False


def run_tui(state: dict) -> None:
    """Полноэкранный режим на curses."""
    import curses

    from tui import CursesUI

    def _loop(scr):
        ui = CursesUI(scr, _user_name(), get_model, WORKSPACE)
        # Подтверждение опасных команд — через curses-диалог, а не print/input:
        # печать в обход curses ломает полноэкранный интерфейс.
        set_confirm_handler(ui.confirm)
        # Пароль sudo спрашиваем скрытым вводом прямо в curses. Команда затем
        # выполняется отделённой от терминала (через SUDO_ASKPASS), поэтому
        # фоновые хуки pacman/wine не могут писать поверх интерфейса.
        set_password_handler(ui.ask_password)
        if state["history"]:
            ui.info("Продолжаю прошлую сессию (история загружена).")
        while True:
            try:
                text = ui.read_line().strip()
            except KeyboardInterrupt:
                break
            if not text:
                continue
            ui.user(text)
            if process_input(text, state, ui):
                break

    curses.wrapper(_loop)


def run_console(state: dict) -> None:
    """Запасной консольный режим (если полноэкранный недоступен)."""
    print()
    print(render_welcome(_user_name(), get_model(), WORKSPACE, bool(state["history"])))
    print()
    ui = ConsoleUI()
    set_confirm_handler(ui.confirm)
    while True:
        try:
            text = input(f"{VIOLET}❯ {RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nДо встречи!")
            break
        if not text:
            continue
        if process_input(text, state, ui):
            print("До встречи!")
            break


def main():
    # Проверяем ключ до старта, чтобы не падать посреди работы
    if not os.getenv("OPENROUTER_API_KEY"):
        print("Ошибка: не задан OPENROUTER_API_KEY. Добавь его в файл .env")
        print("Бесплатный ключ можно взять на https://openrouter.ai/keys")
        sys.exit(1)

    # Создаём рабочую папку, если её ещё нет
    os.makedirs(WORKSPACE, exist_ok=True)

    # Загружаем историю прошлой сессии — Vesper помнит контекст между запусками
    state = {"history": load_history()}

    # Полноэкранный режим только в настоящем терминале. Иначе — консоль.
    if sys.stdout.isatty() and sys.stdin.isatty():
        try:
            run_tui(state)
        except Exception as e:
            # Если curses не смог стартовать — не оставляем пользователя ни с чем
            print(f"Полноэкранный режим недоступен ({e}). Перехожу в консольный.")
            run_console(state)
    else:
        run_console(state)


if __name__ == "__main__":
    main()
