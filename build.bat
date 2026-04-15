@echo off
chcp 65001 >nul
echo ============================================
echo   ESP Flasher — Сборка в один .exe
echo ============================================
echo.

echo [1/3] Устанавливаю зависимости...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo Ошибка установки зависимостей!
    pause
    exit /b 1
)

echo.
echo [2/3] Собираю .exe (один файл)...
pyinstaller --onefile --windowed --name "ESP_Flasher" --icon=NONE main.py
if %errorlevel% neq 0 (
    echo Ошибка сборки!
    pause
    exit /b 1
)

echo.
echo [3/3] Готово!
echo.
echo Файл: dist\ESP_Flasher.exe
echo.
echo Перенесите его куда угодно — он работает без установки!
echo.
pause
