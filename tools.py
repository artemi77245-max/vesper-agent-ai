"""
Инструменты Vesper — это "руки" агента.

Каждый инструмент состоит из двух частей:
  1. Python-функция, которая реально что-то делает (читает файл, пишет и т.д.)
  2. Описание (schema) в формате OpenAI — чтобы модель знала, какие
     инструменты есть и какие аргументы им нужны.

Модель НЕ выполняет код сама. Она лишь просит: "вызови write_file с такими
аргументами", а выполняет уже наш Python здесь.
"""

import os
import re
import subprocess

# Рабочая папка агента. Все файловые операции ограничены ею —
# чтобы агент случайно не залез куда-то за пределы проекта.
WORKSPACE = os.path.abspath(os.getenv("VESPER_WORKSPACE", "./workspace"))

# Опасные шаблоны команд. Если run_command содержит один из них —
# перед выполнением спросим у пользователя подтверждение (y/n).
# Это то, что делает и настоящий Claude Code: не выполнять разрушительное молча.
DANGEROUS_PATTERNS = [
    r"\brm\b",           # удаление файлов
    r"\brmdir\b",        # удаление папок
    r"\bmkfs\b",         # форматирование
    r"\bdd\b",           # низкоуровневая запись на диск
    r":\(\)\{",          # fork-бомба
    r"\bshutdown\b",     # выключение
    r"\breboot\b",       # перезагрузка
    r"\bchmod\b",        # смена прав
    r"\bchown\b",        # смена владельца
    r"\bsudo\b",         # повышение прав
    r"\bcurl\b",         # загрузка из сети
    r"\bwget\b",         # загрузка из сети
    r">\s*/dev/",        # запись в устройства
    r"\bgit\s+push\b",   # отправка в удалённый репозиторий
]


def _is_dangerous(command: str) -> bool:
    """Проверяет, похожа ли команда на потенциально разрушительную."""
    return any(re.search(p, command) for p in DANGEROUS_PATTERNS)


# Обработчик подтверждения опасных команд. По умолчанию — консольный
# (print/input). Полноэкранный интерфейс подменяет его своим через
# set_confirm_handler, иначе печать в обход curses ломает экран.
def _console_confirm(command: str) -> bool:
    print(
        f"\n\033[38;5;196m⚠ Vesper хочет выполнить потенциально опасную "
        f"команду:\033[0m\n\033[38;5;214m  {command}\033[0m"
    )
    answer = input("\033[38;5;196mРазрешить? (y/n): \033[0m").strip().lower()
    return answer in ("y", "yes", "д", "да")


_confirm_handler = _console_confirm


def set_confirm_handler(fn) -> None:
    """Подменяет способ спросить пользователя (например, на curses-диалог)."""
    global _confirm_handler
    _confirm_handler = fn


# --- Пароль sudo ---
# Команды с sudo раньше запускались на живом терминале, чтобы sudo мог
# спросить пароль. Но это ломало полноэкранный интерфейс: фоновые хуки
# (обновление кешей шрифтов у pacman/wine) писали в терминал уже после
# возврата, поверх curses. Решение: спрашиваем пароль ОДИН раз через UI
# (скрытый ввод), передаём его sudo через SUDO_ASKPASS, а саму команду
# запускаем отделённой от терминала (start_new_session) — фоновым процессам
# просто некуда писать поверх экрана.
def _needs_sudo(command: str) -> bool:
    """Есть ли в команде вызов sudo?"""
    return bool(re.search(r"\bsudo\b", command))


def _console_password() -> str | None:
    """Запрос пароля в консольном режиме (скрытый ввод через getpass)."""
    import getpass

    try:
        return getpass.getpass("[sudo] пароль: ")
    except (EOFError, KeyboardInterrupt):
        return None


_password_handler = _console_password
_cached_password: str | None = None  # держим в памяти на время сессии


def set_password_handler(fn) -> None:
    """Подменяет способ спросить пароль (например, скрытый ввод в curses)."""
    global _password_handler
    _password_handler = fn


def _get_sudo_password(force: bool = False) -> str | None:
    """Возвращает пароль sudo: из кеша или спросив у пользователя."""
    global _cached_password
    if _cached_password is not None and not force:
        return _cached_password
    pw = _password_handler()
    if pw:
        _cached_password = pw
    return pw


def _safe_path(path: str) -> str:
    """
    Защита: приводим путь к абсолютному внутри WORKSPACE и проверяем,
    что он не вылезает за её пределы (защита от '../../etc/passwd').
    """
    full = os.path.abspath(os.path.join(WORKSPACE, path))
    if not full.startswith(WORKSPACE):
        raise ValueError(f"Путь за пределами рабочей папки: {path}")
    return full


# --- Сами инструменты (простые Python-функции) ---


def read_file(path: str) -> str:
    """Читает файл и возвращает его содержимое."""
    full = _safe_path(path)
    if not os.path.exists(full):
        return f"Файл не найден: {path}"
    with open(full, "r", encoding="utf-8") as f:
        return f.read()


def write_file(path: str, content: str) -> str:
    """Записывает content в файл (создаёт папки при необходимости)."""
    full = _safe_path(path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Записано в {path} ({len(content)} символов)"


def list_files(directory: str = ".") -> str:
    """Возвращает список файлов и папок в директории."""
    full = _safe_path(directory)
    if not os.path.isdir(full):
        return f"Папка не найдена: {directory}"
    entries = []
    for name in sorted(os.listdir(full)):
        mark = "/" if os.path.isdir(os.path.join(full, name)) else ""
        entries.append(name + mark)
    return "\n".join(entries) if entries else "(пусто)"


def make_dir(path: str) -> str:
    """Создаёт папку (и промежуточные папки) внутри рабочей директории."""
    full = _safe_path(path)
    os.makedirs(full, exist_ok=True)
    return f"Папка создана: {path}"


def search_code(query: str, directory: str = ".") -> str:
    """
    Ищет текст query во всех файлах внутри directory (рекурсивно).
    Возвращает совпадения в формате 'путь:номер_строки: текст'.
    """
    base = _safe_path(directory)
    if not os.path.isdir(base):
        return f"Папка не найдена: {directory}"

    hits = []
    for root, _dirs, files in os.walk(base):
        for name in files:
            fpath = os.path.join(root, name)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    for i, line in enumerate(f, 1):
                        if query in line:
                            rel = os.path.relpath(fpath, WORKSPACE)
                            hits.append(f"{rel}:{i}: {line.strip()}")
            except (UnicodeDecodeError, OSError):
                continue  # пропускаем бинарные/нечитаемые файлы
            if len(hits) >= 100:
                break
    return "\n".join(hits) if hits else f"Совпадений не найдено: {query}"


def _run_isolated(command: str, env: dict | None = None, timeout: int = 120):
    """
    Запускает команду отделённой от терминала и захватывает вывод.

    - stdout/stderr пишутся во ВРЕМЕННЫЙ ФАЙЛ, а не в пайп: демоны (wineserver
      и пр.), наследующие дескриптор, не заблокируют захват до таймаута.
    - start_new_session=True — отдельная сессия без нашего управляющего
      терминала, поэтому фоновые процессы не могут писать поверх curses.
    - stdin=DEVNULL — команда не зависнет в ожидании ввода.

    Возвращает (код_возврата | None, вывод, флаг_таймаута).
    """
    import tempfile

    run_env = os.environ.copy()
    if env:
        run_env.update(env)

    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as f:
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=WORKSPACE,
                stdin=subprocess.DEVNULL,
                stdout=f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                timeout=timeout,
                env=run_env,
            )
            code, timed_out = result.returncode, False
        except subprocess.TimeoutExpired:
            code, timed_out = None, True
        f.seek(0)
        out = f.read().strip()
    return code, out, timed_out


def _format_output(code, out: str, timed_out: bool) -> str:
    """Готовит вывод для модели: хвост до 60 строк, пометка о таймауте/коде."""
    if timed_out:
        tail = "\n".join(out.splitlines()[-40:])
        return (
            "Команда выполнялась слишком долго и была прервана (120с).\n" + tail
        ).strip()
    lines = out.splitlines()
    if len(lines) > 60:
        out = "…(показаны последние 60 строк)\n" + "\n".join(lines[-60:])
    return out or f"(команда завершена, код {code})"


def _run_with_sudo(command: str) -> str:
    """
    Выполняет команду с sudo, не отдавая терминал. Пароль спрашиваем через UI
    и передаём sudo через SUDO_ASKPASS (маленький скрипт-помощник, читающий
    пароль из переменной окружения). Команда запускается отделённой от
    терминала, поэтому её фоновые хуки не ломают полноэкранный интерфейс.
    """
    import stat
    import tempfile

    pw = _get_sudo_password()
    if not pw:
        return "Команда отменена: пароль sudo не введён."

    # Скрипт-askpass печатает пароль (из окружения — не светится в argv/файле).
    helper = tempfile.NamedTemporaryFile(
        mode="w", suffix=".sh", delete=False, encoding="utf-8"
    )
    helper.write('#!/bin/sh\nprintf "%s" "$VESPER_SUDO_PW"\n')
    helper.close()
    os.chmod(helper.name, stat.S_IRWXU)

    # Каждый sudo в команде переводим в неинтерактивный режим через askpass.
    wrapped = re.sub(r"\bsudo\b(?!\s+-A\b)", "sudo -A", command)
    env = {"SUDO_ASKPASS": helper.name, "VESPER_SUDO_PW": pw}

    try:
        code, out, timed_out = _run_isolated(wrapped, env=env)
    finally:
        try:
            os.unlink(helper.name)
        except OSError:
            pass

    # Неверный пароль — сбрасываем кеш, чтобы в следующий раз спросить заново.
    if out and re.search(
        r"(incorrect password|Sorry, try again|отказано|askpass)", out, re.I
    ):
        global _cached_password
        _cached_password = None
        out += "\n(похоже, пароль sudo неверный — попробуй ещё раз)"

    return _format_output(code, out, timed_out)


def run_command(command: str) -> str:
    """Выполняет shell-команду в рабочей папке и возвращает вывод."""
    # Защита: разрушительные команды требуют явного подтверждения пользователя.
    # Как именно спросить — решает активный интерфейс (см. set_confirm_handler).
    if _is_dangerous(command):
        if not _confirm_handler(command):
            return "Команда отменена пользователем."

    # sudo — особый случай: пароль через askpass, запуск отделён от терминала.
    if _needs_sudo(command):
        return _run_with_sudo(command)

    code, out, timed_out = _run_isolated(command)
    return _format_output(code, out, timed_out)


# --- Реестр: имя инструмента -> функция ---
# Ядро агента будет искать функцию здесь по имени, которое назвала модель.
TOOL_FUNCTIONS = {
    "read_file": read_file,
    "write_file": write_file,
    "list_files": list_files,
    "make_dir": make_dir,
    "search_code": search_code,
    "run_command": run_command,
}


# --- Описания инструментов для модели (OpenAI tools schema) ---
# Это то, что видит модель. По этим описаниям она понимает,
# какие инструменты доступны и какие параметры им передавать.
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Прочитать содержимое файла в рабочей папке.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к файлу"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Записать (или перезаписать) файл в рабочей папке.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к файлу"},
                    "content": {"type": "string", "description": "Содержимое"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "Показать список файлов и папок в директории.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "Путь к папке (по умолчанию текущая)",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "make_dir",
            "description": "Создать папку (с промежуточными) в рабочей папке.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к папке"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Найти текст во всех файлах папки (рекурсивно). "
            "Возвращает путь, номер строки и саму строку.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Что искать"},
                    "directory": {
                        "type": "string",
                        "description": "Где искать (по умолчанию вся рабочая папка)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Выполнить shell-команду в рабочей папке. "
            "Опасные команды потребуют подтверждения пользователя.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Команда"}
                },
                "required": ["command"],
            },
        },
    },
]
