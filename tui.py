"""
Полноэкранный интерфейс Vesper на curses (как nano/htop).

Зачем: обычный вывод в терминал уезжает при прокрутке колёсиком, а шапка
с маскотом «отлепляется». Полноэкранный режим (альтернативный экран)
решает это принципиально: программа сама рисует весь экран, колёсико
скроллит ленту диалога, а шапка всегда закреплена. При выходе терминал
возвращается ровно в исходное состояние.

Раскладка сверху вниз:
  ┌ шапка с маскотом и подсказками (закреплена)
  │ ─── разделитель ───
  │ лента диалога (прокручивается: колёсико, PageUp/Down)
  │ ─── разделитель ───
  │ ❯ поле ввода
  └ статус-строка (модель, папка, подсказки)
"""

import curses
import threading

from mascot import FOX

# Кадры спиннера «Vesper думает…»
_SPIN_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

# Символы для рамок
TL, TR, BL, BR = "╭", "╮", "╰", "╯"
H, V = "─", "│"

# Иконка и имя для каждого инструмента (без эмодзи — ровное выравнивание)
_TOOL_META = {
    "read_file": ("◇", "Читаю файл"),
    "write_file": ("◆", "Пишу файл"),
    "list_files": ("▤", "Смотрю папку"),
    "make_dir": ("▨", "Создаю папку"),
    "search_code": ("⌕", "Ищу по коду"),
    "run_command": ("»", "Выполняю команду"),
}


class CursesUI:
    """
    Полноэкранное приложение и одновременно UI-адаптер для агента.

    Агент вызывает методы call_model / tool_call / tool_result / assistant /
    error / info — они добавляют строки в ленту и перерисовывают экран.
    """

    def __init__(self, scr, name, model_getter, workspace):
        self.scr = scr
        self.name = name
        self.get_model = model_getter
        self.workspace = workspace

        # Лента диалога: список записей.
        #   ("line", текст, стиль)      — одноцветная строка (переносится)
        #   ("segs", [(текст,стиль)…])  — сегменты в одну строку (обрезается)
        self.body = []
        self.scroll = 0            # сдвиг от низа ленты (0 = внизу)
        self.thinking = None       # кадр спиннера или None

        # Поле ввода
        self.input_buf = ""
        self.cursor = 0
        self.cmd_history = []
        self.hist_idx = 0

        self._init_colors()
        curses.curs_set(1)
        self.scr.keypad(True)
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
        except curses.error:
            pass

    # ── Цвета ───────────────────────────────────────────────────────
    def _init_colors(self):
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK

        # На 256-цветных терминалах берём сумеречную палитру Vesper,
        # иначе — базовые цвета, чтобы не упасть.
        if curses.COLORS >= 256:
            palette = {
                "violet": 99, "gray": 245, "dim": 240, "yellow": 229,
                "green": 114, "red": 203, "cyan": 80,
            }
        else:
            palette = {
                "violet": curses.COLOR_MAGENTA, "gray": curses.COLOR_WHITE,
                "dim": curses.COLOR_BLUE, "yellow": curses.COLOR_YELLOW,
                "green": curses.COLOR_GREEN, "red": curses.COLOR_RED,
                "cyan": curses.COLOR_CYAN,
            }

        self._pairs = {}
        for i, (name, color) in enumerate(palette.items(), start=1):
            try:
                curses.init_pair(i, color, bg)
            except curses.error:
                curses.init_pair(i, curses.COLOR_WHITE, bg)
            self._pairs[name] = curses.color_pair(i)

    def A(self, style):
        """Атрибут curses по имени стиля (например 'violet' или 'violet_bold')."""
        bold = style.endswith("_bold")
        key = style[:-5] if bold else style
        attr = self._pairs.get(key, 0)
        return attr | curses.A_BOLD if bold else attr

    # ── Безопасный вывод ─────────────────────────────────────────────
    def _put(self, y, x, text, style="gray"):
        """Печатает текст, не вылезая за границы окна (иначе curses падает)."""
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        text = text[: max(0, w - x - 1)]
        try:
            self.scr.addstr(y, x, text, self.A(style))
        except curses.error:
            pass

    # ── Шапка ────────────────────────────────────────────────────────
    def _draw_header(self):
        """Рисует закреплённую панель с маскотом и подсказками. Возвращает высоту."""
        h, w = self.scr.getmaxyx()
        total = min(w, 108)
        inner = total - 7
        left_w = max(14, inner * 2 // 5)
        right_w = inner - left_w

        fox_lines = [ln for ln in FOX.splitlines() if ln.strip()]
        left = [(f"С возвращением, {self.name}!", "violet_bold"), ("", "gray")]
        left += [(fl, "violet") for fl in fox_lines]
        left += [("", "gray"), ("Свет в сумерках кода", "gray")]

        right = [
            ("С чего начать", "yellow_bold"),
            ("Опиши задачу — Vesper напишет и запустит код.", "gray"),
            ("Напиши /help — все команды.", "gray"),
            (H * right_w, "dim"),
            ("Возможности", "yellow_bold"),
            ("Читает, пишет и ищет по коду.", "gray"),
            ("Спрашивает перед опасными командами.", "gray"),
            ("Помнит контекст между запусками.", "gray"),
        ]

        rows = max(len(left), len(right))
        left += [("", "gray")] * (rows - len(left))
        right += [("", "gray")] * (rows - len(right))

        # Верхняя рамка с заголовком
        title = " Vesper Agent "
        top = f"{TL}{H * 3}{title}"
        top += H * (total - len(top) - 1) + TR
        self._put(0, 0, top, "violet")

        # Строки с двумя колонками
        for i, ((lt, ls), (rt, rs)) in enumerate(zip(left, right), start=1):
            self._put(i, 0, V, "violet")
            self._center(i, 2, lt, left_w, ls)
            self._put(i, 2 + left_w + 1, V, "violet")
            self._put(i, 2 + left_w + 3, rt[:right_w], rs)
            self._put(i, total - 1, V, "violet")

        # Нижняя рамка
        self._put(rows + 1, 0, f"{BL}{H * (total - 2)}{BR}", "violet")
        return rows + 2

    def _center(self, y, x, text, width, style):
        """Печатает текст по центру колонки шириной width."""
        pad = max(0, (width - len(text)) // 2)
        self._put(y, x + pad, text[:width], style)

    # ── Лента диалога ────────────────────────────────────────────────
    def _wrap(self, text, width):
        """Переносит строку по ширине (грубо, по символам с учётом слов)."""
        out = []
        for para in text.split("\n"):
            if not para:
                out.append("")
                continue
            while len(para) > width:
                cut = para.rfind(" ", 0, width)
                cut = cut if cut > width // 2 else width
                out.append(para[:cut])
                para = para[cut:].lstrip()
            out.append(para)
        return out

    def _display_rows(self, width):
        """Разворачивает ленту в плоский список строк для отрисовки."""
        rows = []
        for entry in self.body:
            if entry[0] == "line":
                _, text, style = entry
                for ln in self._wrap(text, width):
                    rows.append([("rowline", ln, style)])
            else:  # "segs"
                rows.append([("rowsegs", entry[1])])
        return rows

    def _draw_body(self, top, height, width):
        rows = self._display_rows(width)
        total = len(rows)
        max_scroll = max(0, total - height)
        self.scroll = max(0, min(self.scroll, max_scroll))
        end = total - self.scroll
        start = max(0, end - height)

        y = top
        for row in rows[start:end]:
            kind = row[0][0]
            if kind == "rowline":
                _, text, style = row[0]
                self._put(y, 2, text, style)
            else:  # rowsegs
                x = 2
                for text, style in row[0][1]:
                    self._put(y, x, text, style)
                    x += len(text)
            y += 1

        # Индикатор прокрутки, если лента длиннее экрана и мы не внизу
        if self.scroll > 0:
            _, w = self.scr.getmaxyx()
            self._put(top, min(w, 108) - 6, "↑↑↑", "dim")

    # ── Ввод и статус ────────────────────────────────────────────────
    def _draw_input(self, y):
        self._put(y, 0, "❯ ", "violet_bold")
        self._put(y, 2, self.input_buf, "gray")

    def _draw_status(self, y):
        h, w = self.scr.getmaxyx()
        if self.thinking:
            left = f" {self.thinking} Vesper думает…"
            style = "violet"
        else:
            left = "  /help команды   /model модель   exit выход"
            style = "dim"
        model = self.get_model()
        right = f"модель: {model}  "
        line = left + " " * max(1, w - len(left) - len(right) - 1) + right
        try:
            self.scr.addstr(y, 0, line[: w - 1], self.A(style))
        except curses.error:
            pass

    # ── Полная перерисовка ───────────────────────────────────────────
    def render(self):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        width = min(w, 108) - 4

        header_h = self._draw_header()
        sep1 = header_h
        body_top = header_h + 1
        status_y = h - 1
        input_y = h - 2
        sep2 = h - 3
        body_h = max(1, sep2 - body_top)

        self._put(sep1, 0, H * min(w, 108), "dim")
        self._draw_body(body_top, body_h, width)
        self._put(sep2, 0, H * min(w, 108), "dim")
        self._draw_input(input_y)
        self._draw_status(status_y)

        # Курсор в поле ввода
        try:
            self.scr.move(input_y, 2 + self.cursor)
        except curses.error:
            pass
        self.scr.refresh()

    # ── UI-адаптер для агента ────────────────────────────────────────
    def _add_line(self, text, style="gray"):
        self.body.append(("line", text, style))
        self.scroll = 0  # прыгаем к низу при новом выводе

    def user(self, text):
        self.body.append(("segs", [("❯ ", "violet_bold"), (text, "gray")]))
        self.scroll = 0
        self.render()

    def assistant(self, text):
        self._add_line("", "gray")
        for i, ln in enumerate(text.split("\n")):
            prefix = "Vesper: " if i == 0 else ""
            self._add_line(prefix + ln, "violet")
        self._add_line("", "gray")
        self.render()

    def tool_call(self, step, max_steps, name, args):
        icon, label = _TOOL_META.get(name, ("•", name))
        hint = ""
        for key in ("path", "command", "query", "directory"):
            if args.get(key):
                hint = str(args[key])
                break
        segs = [
            (f"  [шаг {step}/{max_steps}] ", "cyan"),
            (f"{icon} {label} ", "violet"),
        ]
        if hint:
            segs.append((hint, "gray"))
        self.body.append(("segs", segs))
        self.scroll = 0
        self.render()

    def tool_result(self, text, is_error=False):
        lines = (text or "").strip().splitlines()
        preview = lines[0] if lines else ""
        if len(preview) > 76:
            preview = preview[:73] + "…"
        if is_error:
            segs = [("     ✗ ", "red"), (preview, "red")]
        else:
            extra = f"  (+{len(lines) - 1} строк)" if len(lines) > 1 else ""
            segs = [("     ✓ ", "green"), (preview, "gray"), (extra, "dim")]
        self.body.append(("segs", segs))
        self.scroll = 0
        self.render()

    def error(self, text):
        for ln in text.split("\n"):
            self._add_line(ln, "red")
        self.render()

    def info(self, text):
        for ln in text.split("\n"):
            self._add_line(ln, "gray")
        self.render()

    def refresh_header(self):
        self.render()

    def confirm(self, command: str) -> bool:
        """
        Диалог подтверждения опасной команды внутри curses.
        Показывает предупреждение в ленте и ждёт y/n (или д/н) — никакой
        печати в обход экрана, поэтому интерфейс не ломается.
        """
        self.body.append(
            ("segs", [("  ⚠ Опасная команда: ", "red"), (command, "yellow")])
        )
        self._add_line("    Разрешить выполнение? [y — да, n — нет]", "red")
        self.scroll = 0
        self.render()

        while True:
            try:
                ch = self.scr.get_wch()
            except curses.error:
                continue
            if not isinstance(ch, str):
                continue
            low = ch.lower()
            if low in ("y", "д"):
                self._add_line("    Разрешено пользователем.", "green")
                self.render()
                return True
            if low in ("n", "н", "\x03", "\x1b"):  # n / Ctrl+C / Esc — отказ
                self._add_line("    Отклонено пользователем.", "red")
                self.render()
                return False

    def ask_password(self, prompt: str = "[sudo] пароль") -> str:
        """
        Скрытый ввод пароля прямо в curses (не покидая полноэкранный режим).
        Символы не показываются — только точки-заглушки, как в sudo.
        Enter — подтвердить, Esc/Ctrl+C — отмена (возвращает None).
        """
        buf = ""
        self.body.append(("line", f"  {prompt}: ", "yellow"))
        idx = len(self.body) - 1
        self.scroll = 0
        self.render()

        while True:
            try:
                ch = self.scr.get_wch()
            except curses.error:
                continue
            if isinstance(ch, str):
                if ch in ("\n", "\r"):
                    self.body[idx] = ("line", f"  {prompt}: (принято)", "dim")
                    self.render()
                    return buf
                if ch in ("\x1b", "\x03"):  # Esc / Ctrl+C — отмена
                    self.body[idx] = ("line", f"  {prompt}: (отменено)", "red")
                    self.render()
                    return None
                if ch in ("\x7f", "\b"):
                    buf = buf[:-1]
                elif ch.isprintable():
                    buf += ch
            # Заглушки вместо символов — видно длину, но не сам пароль
            self.body[idx] = ("line", f"  {prompt}: {'•' * len(buf)}", "yellow")
            self.render()

    def call_model(self, fn):
        """Выполняет блокирующий запрос в фоне, анимируя спиннер в главном потоке."""
        result = {}

        def worker():
            try:
                result["value"] = fn()
            except Exception as e:  # noqa: BLE001 — прокинем наружу после join
                result["error"] = e

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        i = 0
        while t.is_alive():
            self.thinking = _SPIN_FRAMES[i % len(_SPIN_FRAMES)]
            i += 1
            self.render()
            t.join(timeout=0.09)
        self.thinking = None
        self.render()
        if "error" in result:
            raise result["error"]
        return result["value"]

    # ── Прокрутка ────────────────────────────────────────────────────
    def scroll_up(self, n=3):
        self.scroll += n

    def scroll_down(self, n=3):
        self.scroll = max(0, self.scroll - n)

    # ── Чтение строки ввода ──────────────────────────────────────────
    def read_line(self):
        """Читает строку с поддержкой Юникода (кириллица), стрелок, истории."""
        self.input_buf = ""
        self.cursor = 0
        self.hist_idx = len(self.cmd_history)
        self.render()

        while True:
            try:
                ch = self.scr.get_wch()
            except curses.error:
                continue
            except KeyboardInterrupt:
                raise

            if isinstance(ch, str):
                if ch in ("\n", "\r"):
                    line = self.input_buf
                    if line.strip():
                        self.cmd_history.append(line)
                    return line
                elif ch in ("\x7f", "\b"):            # Backspace
                    if self.cursor > 0:
                        self.input_buf = (
                            self.input_buf[: self.cursor - 1]
                            + self.input_buf[self.cursor :]
                        )
                        self.cursor -= 1
                elif ch == "\x03":                    # Ctrl+C
                    raise KeyboardInterrupt
                elif ch == "\x01":                    # Ctrl+A — в начало
                    self.cursor = 0
                elif ch == "\x05":                    # Ctrl+E — в конец
                    self.cursor = len(self.input_buf)
                elif ch == "\x15":                    # Ctrl+U — очистить
                    self.input_buf = ""
                    self.cursor = 0
                elif ch.isprintable():
                    self.input_buf = (
                        self.input_buf[: self.cursor] + ch + self.input_buf[self.cursor :]
                    )
                    self.cursor += 1
            else:
                self._handle_key(ch)
            self.render()

    def _handle_key(self, key):
        if key == curses.KEY_BACKSPACE:
            if self.cursor > 0:
                self.input_buf = (
                    self.input_buf[: self.cursor - 1] + self.input_buf[self.cursor :]
                )
                self.cursor -= 1
        elif key == curses.KEY_DC:                    # Delete
            self.input_buf = (
                self.input_buf[: self.cursor] + self.input_buf[self.cursor + 1 :]
            )
        elif key == curses.KEY_LEFT:
            self.cursor = max(0, self.cursor - 1)
        elif key == curses.KEY_RIGHT:
            self.cursor = min(len(self.input_buf), self.cursor + 1)
        elif key == curses.KEY_HOME:
            self.cursor = 0
        elif key == curses.KEY_END:
            self.cursor = len(self.input_buf)
        elif key == curses.KEY_UP:                    # история назад
            if self.cmd_history and self.hist_idx > 0:
                self.hist_idx -= 1
                self.input_buf = self.cmd_history[self.hist_idx]
                self.cursor = len(self.input_buf)
        elif key == curses.KEY_DOWN:                  # история вперёд
            if self.hist_idx < len(self.cmd_history) - 1:
                self.hist_idx += 1
                self.input_buf = self.cmd_history[self.hist_idx]
                self.cursor = len(self.input_buf)
            else:
                self.hist_idx = len(self.cmd_history)
                self.input_buf = ""
                self.cursor = 0
        elif key == curses.KEY_PPAGE:                 # PageUp
            self.scroll_up(5)
        elif key == curses.KEY_NPAGE:                 # PageDown
            self.scroll_down(5)
        elif key == curses.KEY_MOUSE:
            self._handle_mouse()
        elif key == curses.KEY_RESIZE:
            pass  # render() сам возьмёт новые размеры

    def _handle_mouse(self):
        try:
            _id, _x, _y, _z, bstate = curses.getmouse()
        except curses.error:
            return
        wheel_up = curses.BUTTON4_PRESSED
        wheel_down = getattr(curses, "BUTTON5_PRESSED", 0x200000)
        if bstate & wheel_up:
            self.scroll_up(3)
        elif bstate & wheel_down:
            self.scroll_down(3)
