"""
Сохранение и загрузка истории диалога между запусками Vesper.

История хранится в JSON-файле внутри рабочей папки. Благодаря этому
Vesper "помнит" предыдущую сессию: закрыл терминал, открыл снова —
и можешь продолжить с того же места.
"""

import json
import os

from tools import WORKSPACE

# Файл истории лежит рядом с рабочей папкой агента.
HISTORY_FILE = os.path.join(WORKSPACE, ".vesper_history.json")


def load_history() -> list:
    """Загружает историю из файла. Если файла нет — возвращает пустой список."""
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        # Битый файл истории не должен ронять агента — просто начнём заново.
        return []


def save_history(history: list) -> None:
    """Сохраняет историю в файл (молча, чтобы не мешать работе)."""
    try:
        os.makedirs(WORKSPACE, exist_ok=True)
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def clear_history() -> None:
    """Удаляет файл истории (команда /reset)."""
    if os.path.exists(HISTORY_FILE):
        try:
            os.remove(HISTORY_FILE)
        except OSError:
            pass
