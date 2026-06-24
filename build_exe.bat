@echo off
REM Скрипт для сборки exe файла приложения
REM Убедитесь, что PyInstaller установлен в .venv\Scripts

REM Активируем виртуальное окружение и устанавливаем зависимости
call .venv\Scripts\activate.bat
pip install pyinstaller

REM Создаем временную директорию для промежуточных файлов
if not exist "build" mkdir build
if not exist "dist" mkdir dist

REM Собираем exe файл
pyinstaller --onefile --noconsole --name Monitor ^
    --add-data "overlay.py;." ^
    --add-data "metrics.py;." ^
    --clean ^
    --distpath dist ^
    --workpath build .\main.py

echo.
echo Сборка завершена!
echo Исполняемый файл находится в: dist\Monitor.exe
echo.
pause
