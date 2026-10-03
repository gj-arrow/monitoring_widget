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

- **CPU** — usage percentage
- **RAM** — used and total
- **GPU** — utilization, temperature, and VRAM
- **Network** — download and upload throughput
- A sparkline of the last 60 seconds on every row
- Colour that means something: it only appears when a metric crosses a threshold
- Always above other windows, with nothing to switch off

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
| Ctrl + middle-click | Quit |

A bare middle-click does nothing. It used to quit, and that was a way to lose the
panel by accident: the wheel button is the one a hand lands on while reaching for
the opacity wheel or the right button, a single unannounced press closed the whole
application, and the log could not say so afterwards because the exit wrote no
record either. Ctrl is the one modifier that opens the quit path — Shift, Alt and
Win+middle do nothing, so it takes a two-button gesture on purpose.

| Quit | The other way out: **Quit** in the menu, from the panel or the tray icon |

The menu holds **Reset position**, **Opacity**, **Scale**, **Write history to file**, and **Quit**.

**There is no "Always on top" switch, because the panel is always on top.** It used to be
there, and it did nothing you could see: it removed a window flag that was not what was
keeping the panel visible, since a borderless tool window is topmost anyway. A switch that
reports no effect is worse than no switch, so the panel's position in the z-order is now
stated once, in the window flags, and cannot be switched off.

### Size

**Scale** offers three steps: **75%**, **85%** and **100%** (the default). Each one is a
proportional shrink of the whole panel — every dimension, every corner radius, every font
size — so the design is the same picture at a different size rather than a smaller one with
the same text crammed in. At 75% the panel is 210 × 210 instead of 280 × 280, and at 85% it
is 238 × 238.

Steps larger than the current size were offered and declined, so 100% is the top of the
list. There is no step between the three and no "Custom" row: the Opacity submenu needs one
because the mouse wheel moves in finer steps than its labels, so a value between two labels
is a value this application produces. Nothing here can put the scale anywhere but on a row,
and a `settings.json` naming a scale the panel does not offer is discarded with the same
warning as any other unusable field.

The panel keeps its top-left corner when the size changes, so **making it smaller never
moves it**: a panel snapped to the top-right keeps its top-left and simply gains margin on
the right (10 px → 54 px at 85% → 84 px at 75%). **Reset position** or a double-click
re-snaps it to the corner.

**Making it bigger does move it**, and it has to: a panel 74 px narrower than it is about to
be cannot keep a top-left that close to the right edge without hanging off the screen, so it
is pulled back to stay on-screen — flush against the edge rather than 10 px inside it.
**Reset position** or a double-click restores that 10 px.

### Above fullscreen games

The panel is above other windows, and it puts itself back **once a second**, so anything
that takes the z-order — a game launching, a launcher, another always-on-top window — is
undone within about a second. It never takes focus doing so: the call that moves the panel
carries `SWP_NOACTIVATE` precisely so it cannot pull the keyboard away from a game.

Two cases are worth stating plainly, because only one of them can be helped:

| How the game runs | The panel |
|---|---|
| **Borderless fullscreen** (the default for most modern titles) | Stays on top. The game is an ordinary maximised window and respects the topmost z-order. |
| **Exclusive fullscreen** | Still there, still on top of every window — and not visible, because the game owns the display's output and nothing composites over it. The panel never hides itself; **borderless** fullscreen is the mode where this does not apply. |

Exclusive fullscreen hands the display's output to the game: whatever it draws *is* what
the screen shows, with nothing composited over it. That is a property of the graphics
mode, not a window flag, so it cannot be raised above from a widget — a topmost window, a
periodic re-assertion or any other trick loses to a mode that owns the output. If your game
offers both modes, choose borderless fullscreen.

Nothing in the panel hides or suppresses itself while a game is running, and nothing watches
for one: it stays at the corner you dragged it to, at the opacity you chose.

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
network throughput, download then upload, as `IN 7.7 KB/s  OUT 7.0 KB/s`.

There is no `SYSTEM` label and no sample-age figure. The dot already states the worst
case, and a second number competing with it in the same 23-pixel strip earned its space
by implying a freshness the panel does not otherwise track.

### Why the CPU row shows no clock

The CPU row carries its load percentage and nothing else. It used to carry a
derived clock beside the nominal it came from, and the clock is gone because it
did not say anything.

It was built from the OS counter `PercentProcessorPerformance`, which is the ratio
of the actual clock to the nominal one. Measured on the machine that decided it —
a Ryzen 5 5600X, nominal 4501 MHz — that counter reads **99.1 % at 9 % load and
99.2 % at 100 %**. The core does not slow down in proportion to the work it is
given; the part drops *voltage* instead. So the whole load sweep moves the figure
by about 1 %, and a row's auxiliary slot was being spent on a number that could
not distinguish an idle machine from a busy one.

It was also being read as something it was not. The form on screen was
`4.48 / 4.50 GHz` with no unit on the second figure, which reads as a percentage
of nominal — and did, twice.

Reading it was not free either: the counter came from a WMI query that measured
**~530 ms of wall time and ~140 ms of process CPU per sample, about 7 % of a
core**, every two seconds. The query is gone along with the figure.

Nothing took the space. The CPU row reads `CPU 34 %`, and the value sits on the
same right edge as the `/ 32.0 GB` and `58 °C` of the rows below it.

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
- A game in **exclusive** fullscreen covers the panel, because the game owns the display
  output; borderless fullscreen does not. See [Above fullscreen games](#above-fullscreen-games)

---

## 🇷🇺 Русский

### Обзор

Лёгкий системный монитор для Windows, отображающий использование CPU, RAM и GPU в
реальном времени в frameless-панели, которая стоит в углу экрана.

### Особенности

- **CPU** — использование в процентах
- **RAM** — занято и всего
- **GPU** — утилизация, температура и объём VRAM
- **Сеть** — скорость приёма и передачи
- График последних 60 секунд в каждой строке
- Цвет, который что-то значит: он появляется только при пересечении порога
- Всегда поверх других окон, и выключать это не нужно

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
| Ctrl + центральная кнопка | Закрыть приложение |

Обычная центральная кнопка ничего не делает. Раньше она закрывала приложение, и это был
способ потерять панель случайно: центральную кнопку нажимают, потянувшись к колесу
прозрачности или к правой кнопке, одно незаметное нажатие закрывало всё приложение, а в
логе после этого ничего не оставалось — выход тоже ничего не писал. Теперь выход открывает
только Ctrl; Shift, Alt и Win в сочетании с центральной кнопкой не делают ничего, то есть
нужно осознанное нажатие двух кнопок.

| Выход | Второй способ: **Quit** в меню, с панели или из значка в трее |

В меню есть **Reset position**, **Opacity**, **Scale**, **Write history to file** и **Quit**.

**Переключателя «Always on top» больше нет, потому что панель всегда сверху.** Раньше он
был и не делал ничего заметного: снимал флаг окна, который и не удерживал панель наверху,
ведь frameless-окно типа tool и так поверх остальных. Переключатель, который ничего не
меняет, хуже его отсутствия, поэтому положение панели в z-порядке теперь задано один раз
во флагах окна и не выключается.

### Размер

В подменю **Scale** три шага: **75%**, **85%** и **100%** (по умолчанию). Каждый — это
пропорциональное уменьшение всей панели: всех размеров, всех скруглений и всех размеров
шрифта, — то есть тот же рисунок в другом размере, а не тот же текст, впихнутый в меньшую
рамку. При 75% панель 210 × 210 вместо 280 × 280, при 85% — 238 × 238.

Шагов больше текущего размера предлагали, и их отклонили, поэтому 100% — верхняя строка
списка. Промежуточных значений нет, и строки «Custom» тоже: в подменю Opacity она нужна
потому, что колесо мыши меняет прозрачность шагами мельче, чем расстояния между подписями,
и значение между двумя подписями приложение производит само. Здесь ничто не может поставить
масштаб не на строку списка, а `settings.json` с масштабом, которого панель не предлагает,
отбрасывается с тем же предупреждением, что и любое другое негодное значение.

При смене размера панель сохраняет свой левый верхний угол, поэтому **уменьшение никогда не
двигает панель**: прижатая к правому верхнему углу, она просто набирает отступ справа
(10 px → 54 px при 85% → 84 px при 75%). **Reset position** или двойной клик возвращают её
в угол.

**Увеличение панель двигает**, и иначе нельзя: панель на 74 px уже, чем станет, не может
сохранить левый верхний угол так близко к правому краю и не уехать за экран, поэтому её
подтягивают обратно — вплотную к краю, а не в 10 px от него. **Reset position** или двойной
клик возвращают эти 10 px.

### Поверх игр в полноэкранном режиме

Панель поверх других окон, и она возвращается наверх **раз в секунду**, поэтому всё, что
перехватило z-порядок — запуск игры, лаунчер, другое окно поверх всех, — возвращается на
своё место примерно за секунду. Фокус она при этом не забирает: у вызова, который её
поднимает, стоит флаг `SWP_NOACTIVATE` именно для того, чтобы клавиатура осталась у игры.

Два случая стоит назвать прямо, потому что помочь можно только с одним:

| Как запущена игра | Панель |
|---|---|
| **Пограничный полноэкранный режим** (по умолчанию у большинства современных игр) | Остаётся сверху. Игра — обычное развёрнутое окно и уважает верхний z-порядок. |
| **Эксклюзивный полноэкранный режим** | Панель на месте и по-прежнему выше всех окон — но её не видно, потому что игра владеет выводом дисплея и поверх ничего не выводится. Панель никогда не прячется сама; режим, к которому это не относится, — **пограничный**. |

В эксклюзивном режиме игра забирает вывод дисплея: то, что она рисует, и есть изображение
экрана, поверх ничего не выводится. Это свойство графического режима, а не флага окна,
поэтому виджет не может оказаться выше: окно поверх всех, периодическое переподнятие или
любой другой приём проигрывают режиму, который владеет выводом. Если игра даёт выбор,
выбирайте пограничный полноэкранный режим.

Ничто в панели не прячется и не приглушается на время игры, и никто за играми не следит:
панель остаётся в том углу, куда её перетащили, с той прозрачностью, которую выбрали.

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
`IN 7.7 KB/s  OUT 7.0 KB/s`.

Метки `SYSTEM` и возраста последнего замера больше нет. Индикатор уже сообщает о худшем
случае, а вторая цифра конкурировала с ним в той же полосе высотой 23 пикселя и
оправдывала своё место, создавая впечатление свежести, которую панель больше нигде не
отслеживает.

### Почему в строке CPU нет частоты

В строке CPU есть только загрузка в процентах. Раньше рядом с ней была
выведенная частота рядом с номинальной, и убрана она потому, что ничего
не говорила.

Она считалась из системного счётчика `PercentProcessorPerformance` — это отношение
фактической частоты к номинальной. Измерено на машине, где это решили (Ryzen 5
5600X, номинальная 4501 МГц): счётчик читает **99,1 % при загрузке 9 % и 99,2 %
при загрузке 100 %**. Ядро не замедляется пропорционально выданной работе; часть
снижает *напряжение*. За весь диапазон нагрузки цифра меняется примерно на 1 %,
то есть вспомогательное место в строке занимала величина, не отличающая
холостое состояние от загруженного.

Её к тому же читали не тем, чем она была. На экране это выглядело как
`4.48 / 4.50 GHz` без единицы у второй цифры — читается как процент от
номинала, и читалось именно так дважды.

И чтение не было бесплатным: счётчик брался запросом WMI, который на этой машине
занимал **~530 мс по времени и ~140 мс процессорного времени на замер, около
7 % ядра**, каждые две секунды. Запрос ушёл вместе с цифрой.

Место не занял ничего: строка читается как `CPU 34 %`, и значение стоит у того
же правого края, что `/ 32.0 GB` и `58 °C` у строк ниже.

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
- Игра в **эксклюзивном** полноэкранном режиме закрывает панель, потому что игра владеет
  выводом дисплея; пограничный полноэкранный режим — нет.
  См. [Поверх игр в полноэкранном режиме](#поверх-игр-в-полноэкранном-режиме)

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
- `wmi` — GPU fallback for machines with no usable NVML handle / запасной путь для GPU,
  когда нет рабочей ручки NVML
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
