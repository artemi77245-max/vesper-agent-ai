"""
Ядро агента Vesper.

Здесь живёт то самое сердце — цикл tool-calling. Логика простая:

  1. Отправляем историю сообщений + список инструментов в модель.
  2. Если модель просит инструменты — выполняем их и кладём результат
     обратно в историю, затем повторяем шаг 1.
  3. Если модель ответила обычным текстом — задача решена, возвращаем ответ.

Это ровно та архитектура, что стоит за Claude Code и Codex.
"""

import json
import os

import httpx
from openai import OpenAI

from tools import TOOL_FUNCTIONS, TOOL_SCHEMAS

# Модель OpenRouter. По умолчанию — бесплатная с поддержкой tool calling.
# Список моделей: https://openrouter.ai/models (ищи тег ":free" и "tools").
MODEL = os.getenv("VESPER_MODEL", "google/gemini-2.0-flash-exp:free")


def set_model(name: str) -> None:
    """Меняет активную модель на лету (команда /model в терминале)."""
    global MODEL
    MODEL = name


def get_model() -> str:
    """Возвращает имя текущей активной модели."""
    return MODEL

# Клиент создаём "лениво" — при первом обращении, а не при импорте.
# Иначе клиент собрался бы до того, как main.py загрузит .env с ключом.
_client = None


def get_client() -> OpenAI:
    """
    Возвращает клиент к OpenRouter (OpenAI-совместимый API), создавая его один раз.
    OpenRouter даёт один ключ к десяткам моделей, включая бесплатные.
    """
    global _client
    if _client is None:
        # PROXY_URL — прокси (например NekoBox: socks5://127.0.0.1:2080).
        # Если задан — весь трафик к OpenRouter пойдёт через него.
        # Если пуст — подключаемся напрямую.
        proxy_url = os.getenv("PROXY_URL", "").strip()
        http_client = httpx.Client(proxy=proxy_url) if proxy_url else None

        _client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY"),
            # OpenRouter просит указывать приложение — это не обязательно,
            # но помогает с приоритетом на бесплатных моделях.
            default_headers={
                "HTTP-Referer": "https://github.com/vesper-agent",
                "X-Title": "Vesper Agent",
            },
            http_client=http_client,
        )
    return _client

# Системный промпт задаёт "характер" Vesper и правила работы.
SYSTEM_PROMPT = """Ты Vesper — аккуратный кодинг-агент, работающий в терминале.

Правила:
- Отвечай на русском, кратко и по делу.
- Прежде чем менять файл, прочитай его (read_file), чтобы не сломать.
- Разбивай задачу на маленькие шаги и используй инструменты по одному.
- Проверяй результат своих действий (например, запусти файл через run_command).
- Когда задача выполнена — напиши короткий итог без вызова инструментов.
"""

# Максимум шагов цикла — защита от бесконечного зацикливания.
MAX_STEPS = 25


def run_agent(user_input: str, history: list, ui) -> list:
    """
    Прогоняет одну задачу пользователя через цикл tool-calling.
    Принимает и возвращает историю сообщений (чтобы помнить контекст).

    `ui` — адаптер отрисовки (консольный или полноэкранный curses).
    Агент не печатает сам, а вызывает методы ui: так один цикл работает
    в любом интерфейсе.
    """
    history.append({"role": "user", "content": user_input})

    for step in range(MAX_STEPS):
        # Шаг 1: спрашиваем модель, передавая ей историю и инструменты.
        # ui.call_model показывает индикатор «Vesper думает…», пока идёт запрос.
        try:
            response = ui.call_model(
                lambda: get_client().chat.completions.create(
                    model=MODEL,
                    messages=[{"role": "system", "content": SYSTEM_PROMPT}] + history,
                    tools=TOOL_SCHEMAS,
                )
            )
        except Exception as e:
            ui.error(f"Ошибка запроса к модели: {e}")
            return history

        # У бесплатных моделей OpenRouter бывает, что вместо ответа приходит
        # объект с ошибкой (перегрузка, лимит, провайдер недоступен) — тогда
        # response.choices пустой. Не падаем, а показываем понятную причину.
        if not getattr(response, "choices", None):
            err = getattr(response, "error", None) or getattr(
                response, "model_extra", None
            )
            ui.error(
                "Модель не вернула ответ (вероятно, перегружена или лимит).\n"
                f"Детали: {err}\n"
                "Попробуй ещё раз или смени модель командой /model."
            )
            return history

        message = response.choices[0].message

        # Кладём ответ модели в историю (важно для контекста следующего шага)
        history.append(message.model_dump(exclude_none=True))

        # Шаг 2: модель НЕ просит инструментов -> это финальный текстовый ответ
        if not message.tool_calls:
            ui.assistant(message.content or "")
            return history

        # Шаг 3: модель просит инструменты -> выполняем каждый
        for call in message.tool_calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            func = TOOL_FUNCTIONS.get(name)
            if func is None:
                result = f"Неизвестный инструмент: {name}"
                ui.tool_call(step + 1, MAX_STEPS, name, args)
                ui.tool_result(result, is_error=True)
            else:
                # Прозрачность: красиво показываем, что делает агент и с чем
                ui.tool_call(step + 1, MAX_STEPS, name, args)
                try:
                    result = func(**args)
                    ui.tool_result(str(result))
                except Exception as e:
                    result = f"Ошибка инструмента {name}: {e}"
                    ui.tool_result(result, is_error=True)

            # Результат инструмента возвращаем модели через историю
            history.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": str(result),
                }
            )
        # ...и цикл повторяется: модель увидит результаты и решит, что дальше

    ui.error("Достигнут лимит шагов. Останавливаюсь.")
    return history
