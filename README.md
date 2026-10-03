# System Monitor Widget / Виджет Системного Монитора

<div align="center">
  <img src="https://img.shields.io/badge/Platform-Windows-41CD52.svg" alt="Windows">
  <img src="https://img.shields.io/badge/Python-3.8+-blue.svg" alt="Python">
  <img src="https://img.shields.io/badge/PyQt6-6.x-green.svg" alt="PyQt6">
</div>

---

## 🇺🇸 English

### Overview

A lightweight system monitor for Windows, displaying real-time CPU, RAM, and GPU usage
in a frameless panel that sits in a corner of the screen.

### Features

- **CPU** — usage percentage, with the derived clock beside the nominal it came from
- **RAM** — used and total
- **GPU** — utilization, temperature, and VRAM
- **Network** — download and upload throughput
- A sparkline of the last 60 seconds on every row
- Colour that means something: it only appears when a metric crosses a threshold

### How to Run

1. **Install Python 3.8+** (if not already installed)

2. **Create virtual environment:**
   ```bash
   python -m venv .venv
   ```

3. **Activate the environment (Windows):**
   ```bash
   .venv\Scripts\activate
   ```

4. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

5. **Install test dependencies** (only needed to run the tests):
   ```bash
   pip install -r requirements-dev.txt
   ```

6. **Run from the project root:**
   ```bash
   python main.py
   ```

### Controls

| Action | Description |
|--------|-------------|
| Drag | Move the panel anywhere on screen |
| Double-click | Snap it back to the top-right corner |
| Mouse wheel | Adjust panel opacity |
| Right-click | Open the menu |
| Tray icon, click | Open the menu |
| Tray icon, double-click | Snap the panel back to the top-right corner |
| Middle-click | Quit |

The menu holds **Reset position**, **Opacity**, **Always on top**, **Write history to file**,
and **Quit**.

### Color Coding

Colour encodes load, and nothing else. A row is drawn in the calm blue until it has
something to say; there is no colour switch to flip for its own sake.

| Metric | Normal | Warn | Critical |
|--------|--------|------|----------|
| CPU | < 80% | ≥ 80% | ≥ 95% |
| RAM | < 80% | ≥ 80% | ≥ 95% |
| GPU | < 80% | ≥ 80% | ≥ 95% |
| VRAM | < 80% | ≥ 80% | ≥ 95% |
| GPU temperature | < 70 °C | ≥ 70 °C | ≥ 80 °C |
| CPU frequency | — no thresholds, never coloured by load — | | |

Two states, not one. A row goes amber at the warning threshold and red at the critical
one, so "busy" and "in trouble" are not the same colour.

**A metric that could not be measured shows `--`, not a guess.** If a probe fails, or a
counter comes back discontinuous, or a reading is out of any plausible range, the panel
prints a dash and says nothing. It will not render a number it does not trust, and a
missing reading is not treated as an alarm.

**The random-colour mode is gone.** It coloured rows for decoration rather than for
meaning, which made a coloured row impossible to read as a warning. Colour now encodes
load and only appears when a metric crosses a threshold.

### Header

The header carries the status dot — the worst state across all rows — and current
network throughput, download then upload, as `DN 7.7 KB/s  UP 7.0 KB/s`.

There is no `SYSTEM` label and no sample-age figure. The dot already states the worst
case, and a second number competing with it in the same 23-pixel strip earned its space
by implying a freshness the panel does not otherwise track.

### Why there is no acrylic backdrop

The panel composites **directly against the desktop**: a translucent fill, a hairline
border and its own drop shadow, all drawn by `painter.py`. A Windows *system backdrop* —
Mica or acrylic — cannot be layered on top of that, and this was measured rather than
assumed. On Windows 11 25H2, asking DWM for one changed **538,328 of the 547,600 pixels**
inside the window rect, and what appeared was an opaque, hard-edged, **square-cornered**
slab filling the 8-pixel transparent margin around the panel, with the drop shadow gone
entirely. That slab is a border the panel never drew, and it does not go away when the
setting is turned off, because the panel used to skip writing the attribute on the way
out.

The margin has to stay transparent, and that is the whole constraint: the drop shadow is
drawn *outside* the panel's rounded rect and fades to nothing against whatever is behind
it. A backdrop material fills exactly the window rect, so no backdrop value respects this
design — Mica and "let the system decide" included. Suppressing the border does not help
either: setting the DWM border colour to *none* changed **0** of those pixels, because
the slab is the material itself rather than a border drawn at the window edge.

So the setting was removed rather than retuned, and the panel keeps the translucent fill
that the opacity wheel, the rounded corners and the shadow are all built on.

### Requirements

- Windows 10/11
- Python 3.8+
- NVIDIA GPU (for GPU metrics via pynvml; without one the GPU and VRAM rows show `--`)

### Logging Files

Both logs are written next to the executable, not to the working directory — a frozen
build started from a shortcut has a working directory nobody chose.

- `app_debug.log` — lifecycle and errors only, **rotating at 512 KB with 2 backups**
  (so at most ~1.5 MB, however long it runs). Per-metric warnings are kept; the routine
  chatter from probes that failed and recovered is not, and it was most of what used to
  fill the file.
- `metrics_history.log` — an optional CSV trace, **one row per sample**. **Off by
  default**; turn it on from the menu with **Write history to file**, and off again the
  same way. If the file's columns do not match the running build, it is rotated aside
  and a fresh trace is started rather than having two schemas mixed in one file.

### Known Limitations

- GPU monitoring requires an NVIDIA graphics card
- The CPU clock is derived from a performance-counter ratio; on some machines that
  counter is unavailable and the frequency shows `--`

---

## 🇷🇺 Русский

### Обзор

Лёгкий системный монитор для Windows, отображающий использование CPU, RAM и GPU в
реальном времени в frameless-панели, которая стоит в углу экрана.

### Особенности

- **CPU** — использование в процентах, а также реальная частота рядом с номинальной,
  из которой она выведена
- **RAM** — занято и всего
- **GPU** — утилизация, температура и объём VRAM
- **Сеть** — скорость приёма и передачи
- График последних 60 секунд в каждой строке
- Цвет, который что-то значит: он появляется только при пересечении порога

### Запуск

1. **Установите Python 3.8+** (если ещё нет)

2. **Создайте виртуальное окружение:**
   ```bash
   python -m venv .venv
   ```

3. **Активируйте окружение (Windows):**
   ```bash
   .venv\Scripts\activate
   ```

4. **Установите зависимости:**
   ```bash
   pip install -r requirements.txt
   ```

5. **Установите зависимости для тестов** (нужны только для запуска тестов):
   ```bash
   pip install -r requirements-dev.txt
   ```

6. **Запустите из корня проекта:**
   ```bash
   python main.py
   ```

### Управление

| Действие | Описание |
|----------|----------|
| Перетаскивание | Переместить панель в любое место экрана |
| Двойной клик | Вернуть панель в правый верхний угол |
| Колесо мыши | Изменить прозрачность панели |
| ПКМ (правая кнопка) | Открыть меню |
| Значок в трее, клик | Открыть меню |
| Значок в трее, двойной клик | Вернуть панель в правый верхний угол |
| Центральная кнопка | Закрыть приложение |

В меню есть **Reset position**, **Opacity**, **Always on top**, **Write history to file**
и **Quit**.

### Цветовое кодирование

Цвет кодирует нагрузку и ничего больше. Строка рисуется спокойным синим, пока ей
нечего сообщить; переключателя цвета ради самого цвета больше нет.

| Параметр | Норма | Предупреждение | Критично |
|----------|-------|----------------|----------|
| CPU | < 80% | ≥ 80% | ≥ 95% |
| RAM | < 80% | ≥ 80% | ≥ 95% |
| GPU | < 80% | ≥ 80% | ≥ 95% |
| VRAM | < 80% | ≥ 80% | ≥ 95% |
| Температура GPU | < 70 °C | ≥ 70 °C | ≥ 80 °C |
| Частота CPU | — без порогов, никогда не окрашивается по нагрузке — | | |

Состояний два, а не одно. Строка становится янтарной на пороге предупреждения и
красной на критическом, поэтому «занято» и «проблема» — это разные цвета.

**Показатель, который не удалось измерить, отображается как `--`, а не как догадка.**
Если зонд не сработал, счётчик вернул разрывное значение или чтение вышло за любые
разумные пределы, панель печатает прочерк и молчит. Она не выводит число, которому не
доверяет, и отсутствие показателя не считается тревогой.

**Режим случайных цветов удалён.** Он красил строки ради украшения, а не ради смысла,
после чего цветную строку уже нельзя было прочитать как предупреждение. Теперь цвет
кодирует нагрузку и появляется только при пересечении порога.

### Заголовок

В заголовке находятся индикатор состояния — худшее состояние среди всех строк — и
текущая пропускная способность сети, сначала приём, потом передача, в виде
`DN 7.7 KB/s  UP 7.0 KB/s`.

Метки `SYSTEM` и возраста последнего замера больше нет. Индикатор уже сообщает о худшем
случае, а вторая цифра конкурировала с ним в той же полосе высотой 23 пикселя и
оправдывала своё место, создавая впечатление свежести, которую панель больше нигде не
отслеживает.

### Почему нет акриловой подложки

Панель смешивается **напрямую с рабочим столом**: полупрозрачная заливка, волосяная рамка
и собственная тень, всё это рисует `painter.py`. Системную подложку Windows — Mica или
acrylic — поверх этого наложить нельзя, и это проверено измерением, а не рассуждением.
На Windows 11 25H2 запрос такой подложки у DWM изменил **538 328 из 547 600 пикселей**
внутри прямоугольника окна, и появилась непрозрачная, жёстко обрезанная
**прямоугольная** пластина в 8-пиксельной прозрачной рамке вокруг панели, а тень
исчезла полностью. Эта пластина — рамка, которую панель никогда не рисовала, и она не
убиралась при выключении настройки, потому что атрибут раньше просто не записывался.

Прозрачная рамка нужна обязательно: тень рисуется *снаружи* скруглённого прямоугольника
панели и плавно уходит в ноль по тому, что лежит под ней. Системная подложка заливает
ровно прямоугольник окна, поэтому ни одно значение подложки не совместимо с этой
конструкцией — включая Mica и «пусть решит система». Не помогает и отключение рамки:
цвет рамки DWM, установленный в «нет», изменил **0** пикселей, потому что пластина —
это сама подложка, а не рамка на краю окна.

Поэтому настройку убрали, а не перенастроили: панель сохраняет полупрозрачную заливку,
на которой построены и колёсико прозрачности, и скруглённые углы, и тень.

### Требования

- Windows 10/11
- Python 3.8+
- NVIDIA GPU (для метрик GPU через pynvml; без него строки GPU и VRAM показывают `--`)

### Файлы лога

Оба лога пишутся рядом с исполняемым файлом, а не в рабочий каталог: у frozen-сборки,
запущенной из ярлыка, рабочий каталог никто не выбирал.

- `app_debug.log` — только жизненный цикл и ошибки, **с ротацией на 512 КБ и 2
  резервными копиями** (то есть не более ~1,5 МБ, сколько бы он ни работал).
  Предупреждения по метрикам сохраняются; рутинная болтовня от зондов, которые
  сработали и оправились, — нет, и именно она заполняла файл.
- `metrics_history.log` — необязательный CSV-трейс, **одна строка на замер**. **По
  умолчанию выключен**; включается в меню пунктом **Write history to file** и там же
  выключается. Если столбцы файла не совпадают с текущей сборкой, файл откладывается
  в сторону и начинается новый трейс, а не смешиваются две схемы в одном файле.

### Ограничения

- Мониторинг GPU требует видеокарты NVIDIA
- Частота CPU выводится из отношения счётчика производительности; на некоторых машинах
  этот счётчик недоступен и частота показывает `--`

---

## 📁 Project Structure / Структура проекта

All modules live at the project root / Все модули лежат в корне проекта:

| File | Role / Роль |
|------|-------------|
| `main.py` | Entry point, tray menu, sampling thread / Точка входа, меню в трее, поток опроса |
| `metrics.py` | `Snapshot`, `SystemProbe`, `GpuProbe` — retrieval / сбор метрик |
| `painter.py` | Pure `QPainter` rendering, no widget state / Чистая отрисовка, без состояния виджета |
| `overlay.py` | Panel window and input handling / Окно панели и обработка ввода |
| `theme.py` | Palette, thresholds, geometry, typography / Палитра, пороги, геометрия, типографика |
| `history.py` | Ring buffer, resampling, optional CSV / Кольцевой буфер, передискретизация, необязательный CSV |
| `settings.py` | Persisted preferences / Сохранённые настройки |
| `tests/` | Test suite / Набор тестов |

`painter.py` holds no widget state, no timers and no I/O, which is what lets the tests
draw a panel into a `QImage` with no window on screen and compare it against a reference
image.

`painter.py` не хранит состояния виджета, таймеры и не делает ввода-вывода — благодаря
этому тесты рисуют панель в `QImage` без окна на экране и сравнивают с эталоном.

## 📦 Dependencies / Зависимости

Runtime / Время выполнения — `requirements.txt`:

- `psutil` — CPU, RAM, network counters / счётчики CPU, RAM, сети
- `pynvml` — NVIDIA GPU monitoring / мониторинг GPU NVIDIA
- `wmi` — CPU clock, perf counters, GPU fallback / частота CPU, счётчики, запасной путь для GPU
- `PyQt6` — GUI framework / графический интерфейс

Tests / Тесты — `requirements-dev.txt` (`-r requirements.txt` plus pinned pytest /
`requirements.txt` и зафиксированный pytest).

## 🔨 Building / Сборка

```bash
build_exe.bat
```

Output / Результат: `dist\Monitor.exe`. The build is described in one place, `Monitor.spec`,
and the script only invokes it / Сборка описана в одном месте, в `Monitor.spec`, а скрипт
только вызывает его.

---

<div align="center">
  <strong>📞 Support / Поддержка:</strong> Create an issue in the repository if you have questions or suggestions.
</div>
