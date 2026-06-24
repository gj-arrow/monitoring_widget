# System Monitor Widget / Виджет Системного Монитора

<div align="center">
  <img src="https://img.shields.io/badge/Platform-Windows-41CD52.svg" alt="Windows">
  <img src="https://img.shields.io/badge/Python-3.8+-blue.svg" alt="Python">
  <img src="https://img.shields.io/badge/PyQt6-6.x-green.svg" alt="PyQt6">
</div>

---

## 🇺🇸 English

### Overview

A lightweight system monitor for Windows, displaying real-time CPU, RAM, and GPU usage.

### Features

- **CPU** usage percentage
- **RAM** — current and total memory consumption
- **GPU** — utilization, temperature, and VRAM
- Color-coded indicators for load status

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
   pip install psutil pynvml wmi PyQt6
   ```

5. **Run from project root:**
   ```bash
   .venv\Scripts\python widget\main.py
   ```

### Controls

| Action | Description |
|--------|-------------|
| Right-click | Enable random colors for display |
| Double-click | Restore default colors |
| Middle-click | Quit application |
| Drag | Move widget on screen |

### Color Coding

| Metric | Green (<80%) | Red (≥80%) |
|--------|--------------|------------|
| CPU | ✅ | ❌ |
| RAM | ✅ | ❌ |
| GPU | ✅ | ❌ |
| VRAM | ✅ | ❌ |

### Requirements

- Windows 10/11
- Python 3.8+
- NVIDIA GPU (for GPU metrics via pynvml)

### Logging Files

- `app_debug.log` — Errors and startup messages
- `metrics_history.log` — Collected metrics history

### Known Limitations

- GPU monitoring requires NVIDIA graphics card
- GPU data may require switching task manager to Windows process

---

## 🇷🇺 Русский

### Обзор

Лёгкий системный монитор для Windows, отображающий использование CPU, RAM и GPU в реальном времени.

### Особенности

- **CPU** — использование в процентах
- **RAM** — текущее и общее потребление памяти
- **GPU** — утилизация, температура и объем VRAM
- Красивый отображение с цветовым индикатором нагрузки

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
   pip install psutil pynvml wmi PyQt6
   ```

5. **Запустите из корня проекта:**
   ```bash
   .venv\Scripts\python widget\main.py
   ```

### Управление

| Действие | Описание |
|----------|----------|
| ПКМ (правая кнопка) | Включить случайные цвета |
| Двойной клик | Вернуть стандартные цвета |
| Центральная кнопка | Закрыть приложение |
| Перетаскивание | Переместить виджет |

### Цветовое кодирование

| Параметр | Зеленый (<80%) | Красный (≥80%) |
|----------|----------------|-----------------|
| CPU | ✅ Используется | ❌ Высокая нагрузка |
| RAM | ✅ | ❌ |
| GPU | ✅ | ❌ |
| VRAM | ✅ | ❌ |

### Требования

- Windows 10/11
- Python 3.8+
- NVIDIA GPU (для сбора метрик GPU)

### Файлы лога

- `app_debug.log` — Ошибки и сообщения о запуске
- `metrics_history.log` — История собранных метрик

### Ограничения

- GPU мониторинг требует NVIDIA видеокарты
- Для получения данных GPU может потребоваться переключение диспетчера задач на процессор Windows

---

## 📁 Project Structure / Структура проекта

```
widget/
├── main.py          # Entry point / Точка входа
├── metrics.py       # Metrics retrieval / Сбор метрик
├── overlay.py       # UI component / Компонент интерфейса
└── README.md        # Documentation / Документация
```

## 📦 Dependencies / Зависимости

- `psutil` — System resources (CPU, RAM)
- `pynvml` — NVIDIA GPU monitoring
- `wmi` — Fallback metrics collection
- `PyQt6` — GUI framework

---

<div align="center">
  <strong>📞 Support / Поддержка:</strong> Create an issue in the repository if you have questions or suggestions.
</div>
