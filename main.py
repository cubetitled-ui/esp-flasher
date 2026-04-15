"""
ESP Flasher — простая программа для прошивки ESP32
Для новичков: вставил ссылку или выбрал файл — всё остальное само.
"""

import sys
import os
import re
import json
import zipfile
import shutil
import subprocess
import threading
import tempfile
import time
import requests
import serial
import serial.tools.list_ports

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path


# ======================== Админ-права ========================

def is_admin() -> bool:
    """Проверяет, запущена ли программа с правами администратора."""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run_as_admin():
    """Перезапускает текущий скрипт с правами администратора."""
    import ctypes
    script = os.path.abspath(sys.argv[0])
    params = " ".join(sys.argv[1:])
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, f'"{script}" {params}', None, 1
    )


if not is_admin() and not getattr(sys, "frozen", False):
    # Для .exe из PyInstaller проверка ниже, в App.__init__
    pass


# ======================== Утилиты ========================

def resource_path(relative: str) -> str:
    """Путь к ресурсам (для PyInstaller onefile)."""
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)  # type: ignore
    else:
        base = Path(__file__).resolve().parent
    return str(base / relative)


class Logger:
    """Простой логгер, пишет в Text-виджет и в консоль."""

    def __init__(self, text_widget: tk.Text):
        self.text = text_widget
        self._lock = threading.Lock()

    def _write(self, msg: str):
        with self._lock:
            self.text.insert(tk.END, msg + "\n")
            self.text.see(tk.END)
        print(msg, flush=True)

    def info(self, msg: str):
        self._write(f"[INFO] {msg}")

    def ok(self, msg: str):
        self._write(f"[✓] {msg}")

    def warn(self, msg: str):
        self._write(f"[!] {msg}")

    def error(self, msg: str):
        self._write(f"[✗] {msg}")


# ======================== Драйверы ========================
# CH340 — GitHub mirror, работает.
# CP210x — на Win 10/11 драйвер уже встроен через Windows Update.
#   Если плата видна в COM-портах — драйвер уже стоит.
#   Если нет — пробуем pnputil / Windows Update.
DRIVER_URLS = {
    "cp210x": [],  # Windows Update — встроенный
    "ch340": [
        "https://github.com/HobbyComponents/CH340-Drivers/archive/refs/heads/master.zip",
    ],
}

DRIVER_URLS_FALLBACK = {
    "cp210x": [],
    "ch340": [],
}


def _is_driver_installed(display_name_sub: str) -> bool:
    """Проверяет, установлен ли драйвер через реестр Windows."""
    try:
        import winreg
        paths = [
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        ]
        for reg_path in paths:
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path)
                i = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(key, i)
                        subkey = winreg.OpenKey(key, subkey_name)
                        name = winreg.QueryValueEx(subkey, "DisplayName")[0]
                        if display_name_sub.lower() in name.lower():
                            return True
                        i += 1
                    except OSError:
                        break
            except OSError:
                continue
    except Exception:
        pass
    return False


def _download_with_retry(urls: list[str], dest: str, log: Logger) -> bool:
    """Скачивает файл, перебирая зеркала, пока одно не сработает."""
    for i, url in enumerate(urls):
        try:
            log.info(f"  Зеркало {i+1}/{len(urls)}: {url[:60]}...")
            resp = requests.get(url, timeout=20, allow_redirects=True)
            resp.raise_for_status()
            # Проверяем что это не HTML-страница
            content_type = resp.headers.get("Content-Type", "")
            if "text/html" in content_type and len(urls) > 1:
                log.warn("  Это HTML-страница, пробую следующее зеркало...")
                continue
            with open(dest, "wb") as f:
                f.write(resp.content)
            log.ok("  Драйвер скачан")
            return True
        except Exception as e:
            log.warn(f"  Зеркало не ответило: {e}")
    return False


def _try_windows_update_cp210x(log: Logger) -> bool:
    """Пробует найти драйвер CP210x через Windows Update."""
    log.info("  Пробую Windows Update для CP210x...")
    try:
        # Создаём временный .inf для поиска в Windows Update
        result = subprocess.run(
            'pnputil /enum-devices /connected',
            shell=True, capture_output=True, text=True, timeout=30
        )
        output = (result.stdout + result.stderr).lower()
        if "cp210" in output or "silicon" in output or "usb serial" in output:
            log.ok("  CP210x уже определяется системой")
            return True
    except Exception:
        pass
    return False


def install_driver_silent(driver_name: str, work_dir: str, log: Logger) -> bool:
    """Скачивает и устанавливает драйвер (CP210x или CH340)."""
    # Проверяем установку
    check_names = {"cp210x": "CP210x", "ch340": "CH340"}
    if _is_driver_installed(check_names.get(driver_name, driver_name)):
        log.info(f"Драйвер {driver_name.upper()} уже установлен")
        return True

    # Проверяем админ-права
    if not is_admin():
        log.warn("Нужны права администратора для установки драйверов.")
        log.warn("Перезапустите программу от имени администратора (правый клик → Запуск от имени администратора).")
        return False

    # CP210x — на Win 10/11 часто уже есть в системе
    if driver_name == "cp210x":
        if _try_windows_update_cp210x(log):
            return True

    # Получаем список зеркал
    urls = DRIVER_URLS.get(driver_name, [])
    # Добавляем fallback-зеркала
    if driver_name in DRIVER_URLS_FALLBACK:
        urls = urls + DRIVER_URLS_FALLBACK[driver_name]

    # CP210x без URL — только Windows Update
    if driver_name == "cp210x" and not urls:
        log.info("CP210x: драйвер встроен в Windows 10/11. Переподключите плату.")
        return _try_windows_update_cp210x(log)

    if not urls:
        log.error(f"Нет ссылок на драйвер {driver_name}")
        return False

    zip_path = os.path.join(work_dir, f"{driver_name}_driver.zip")
    log.info(f"Скачиваю драйвер {driver_name}...")

    if not _download_with_retry(urls, zip_path, log):
        log.error(f"Не удалось скачать драйвер {driver_name} ни с одного зеркала")
        return False

    extract_dir = os.path.join(work_dir, f"{driver_name}_drv")
    os.makedirs(extract_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)
    except Exception as e:
        log.error(f"Не удалось распаковать драйвер: {e}")
        return False

    log.info(f"Устанавливаю драйвер {driver_name}...")

    # Ищем .inf и ставим через pnputil
    installed_any = False
    try:
        for root, _dirs, files in os.walk(extract_dir):
            for fn in files:
                if fn.lower().endswith(".inf"):
                    inf_path = os.path.join(root, fn)
                    log.info(f"  Ставлю: {os.path.basename(inf_path)}")
                    result = subprocess.run(
                        f'pnputil /add-driver "{inf_path}" /install',
                        shell=True, capture_output=True, text=True, timeout=60
                    )
                    out = (result.stdout + " " + result.stderr).lower()
                    if result.returncode == 0 or "успешн" in out or "success" in out:
                        log.ok(f"  Драйвер установлен")
                        installed_any = True
                    else:
                        log.warn(f"  pnputil: {result.stdout[:200]}")
    except Exception as e:
        log.error(f"Ошибка установки: {e}")

    if installed_any:
        log.ok(f"Драйвер {driver_name} установлен")
        return True

    log.warn(f"Не удалось установить {driver_name}. Установите вручную:")
    log.warn(f"  1. Откройте папку: {extract_dir}")
    log.warn(f"  2. Правый клик на .inf → Установить")
    return False


# ======================== COM-порты и ESP32 ========================

def find_esp32_ports() -> list[dict]:
    """Ищет подключённые ESP32 по COM-портам."""
    ports = []
    for port in serial.tools.list_ports.comports():
        hwid_lower = port.hwid.lower()
        desc_lower = port.description.lower()
        # Типичные VID для ESP32-плат
        if any(v in hwid_lower for v in ["10c4", "1a86", "ea60", "7523"]):
            ports.append({
                "port": port.device,
                "description": port.description,
                "hwid": port.hwid,
            })
        elif "usb serial" in desc_lower or "usb uart" in desc_lower:
            ports.append({
                "port": port.device,
                "description": port.description,
                "hwid": port.hwid,
            })
    return ports


def ensure_drivers(log: Logger, work_dir: str):
    """Проверяет и при необходимости ставит драйверы."""
    log.info("Проверяю драйверы...")
    ports = find_esp32_ports()
    if ports:
        log.ok(f"Нашёл ESP32 на порту {ports[0]['port']}")
        return True

    log.warn("ESP32 не найдена. Пробую установить драйверы...")
    install_driver_silent("cp210x", work_dir, log)
    install_driver_silent("ch340", work_dir, log)
    time.sleep(2)  # даём системе обновить порты

    ports = find_esp32_ports()
    if ports:
        log.ok(f"Нашёл ESP32 на порту {ports[0]['port']} после установки драйверов")
        return True

    log.warn("ESP32 не найдена даже после установки драйверов. Продолжаю — может, подключите позже.")
    return False


# ======================== GitHub / ZIP / BIN ========================

GITHUB_RAW_RE = re.compile(
    r"https?://github\.com/([^/]+)/([^/]+?)/(?:releases/download|raw)/(.+)"
)


def normalize_github_url(url: str) -> str:
    """Убирает /tree/branch и /blob/branch из GitHub URL."""
    # https://github.com/user/repo/tree/main  →  https://github.com/user/repo
    # https://github.com/user/repo/blob/main/firmware.ino  →  https://github.com/user/repo
    cleaned = re.sub(r'/tree/[^/]+.*$', '', url)
    cleaned = re.sub(r'/blob/[^/]+/.*$', '', cleaned)
    # Убираем trailing slash
    cleaned = cleaned.rstrip('/')
    # Убираем .git
    cleaned = re.sub(r'\.git$', '', cleaned)
    return cleaned


def guess_bin_url(github_url: str) -> str | None:
    """Пытается найти .bin URL из GitHub-ссылки."""
    # Прямая ссылка на .bin
    if ".bin" in github_url.lower():
        return github_url

    # Нормализуем URL
    url = normalize_github_url(github_url)

    # Превращаем страницу релиза в download-ссылку
    match = GITHUB_RAW_RE.match(github_url)
    if match:
        return github_url

    # Если это просто страница GitHub — пробуем API releases
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if m:
        owner, repo = m.group(1), m.group(2)
        api = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
        try:
            resp = requests.get(api, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            for asset in data.get("assets", []):
                if asset["name"].lower().endswith(".bin") or asset["name"].lower().endswith(".esp32.bin"):
                    return asset["browser_download_url"]
            # Пробуем найти bin в body
            body = data.get("body", "")
            for link in re.findall(r"https?://[^\s]+\.bin[^\s]*", body):
                return link
        except Exception:
            pass
    return None


def download_bin(url: str, dest: str, log: Logger) -> bool:
    """Скачивает файл по ссылке (bin, zip, etc)."""
    fname = Path(dest).name
    log.info(f"Скачиваю {fname}...")
    try:
        resp = requests.get(url, timeout=120, stream=True)
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)
        size = os.path.getsize(dest)
        log.ok(f"Скачано {fname}: {size // 1024} КБ")
        return True
    except Exception as e:
        log.error(f"Ошибка скачивания: {e}")
        return False


def extract_from_zip(zip_path: str, dest_dir: str, log: Logger) -> list[str]:
    """Распаковывает zip, возвращает список .bin файлов."""
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(dest_dir)
    except Exception as e:
        log.error(f"Не удалось распаковать zip: {e}")
        return []

    bins = []
    for root, _dirs, files in os.walk(dest_dir):
        for fn in files:
            if fn.lower().endswith(".bin"):
                bins.append(os.path.join(root, fn))
    if not bins:
        log.warn("В zip-архиве нет .bin файла")
    return bins


def find_ino_file(directory: str) -> str | None:
    """Ищет .ino файл в директории."""
    for root, _dirs, files in os.walk(directory):
        for fn in files:
            if fn.lower().endswith(".ino"):
                return os.path.join(root, fn)
    return None


# ======================== Arduino CLI ========================

ARDUINO_CLI_URL = "https://github.com/arduino/arduino-cli/releases/download/v1.4.1/arduino-cli_1.4.1_Windows_64bit.zip"


def ensure_arduino_cli(work_dir: str, log: Logger) -> str | None:
    """Скачивает и распаковывает arduino-cli.exe."""
    cli_path = os.path.join(work_dir, "arduino-cli.exe")
    if os.path.exists(cli_path):
        return cli_path

    log.info("Скачиваю Arduino CLI...")
    zip_path = os.path.join(work_dir, "arduino-cli.zip")
    try:
        resp = requests.get(ARDUINO_CLI_URL, timeout=60)
        resp.raise_for_status()
        with open(zip_path, "wb") as f:
            f.write(resp.content)
        with zipfile.ZipFile(zip_path, "r") as zf:
            # Внутри может быть папка — ищем .exe
            for info in zf.infolist():
                if info.filename.endswith("arduino-cli.exe"):
                    zf.extract(info, work_dir)
                    # Переименовываем если в папке
                    extracted = os.path.join(work_dir, info.filename)
                    if extracted != cli_path:
                        shutil.move(extracted, cli_path)
                    return cli_path
    except Exception as e:
        log.error(f"Не удалось скачать Arduino CLI: {e}")
    return None


def install_esp32_core(cli_path: str, work_dir: str, log: Logger) -> bool:
    """Устанавливает ядро ESP32 через Arduino CLI."""
    data_dir = os.path.join(work_dir, "arduino_data")
    os.makedirs(data_dir, exist_ok=True)

    # Инициализация
    subprocess.run(
        f'"{cli_path}" init', shell=True, cwd=work_dir,
        capture_output=True, timeout=30
    )

    log.info("Устанавливаю ядро ESP32...")
    # Добавляем URL ядра
    subprocess.run(
        f'"{cli_path}" core update-index --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json '
        f'--config-dir "{data_dir}"',
        shell=True, cwd=work_dir, capture_output=True, timeout=120
    )

    result = subprocess.run(
        f'"{cli_path}" core install esp32:esp32 '
        f'--additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json '
        f'--config-dir "{data_dir}"',
        shell=True, cwd=work_dir, capture_output=True, text=True, timeout=600
    )

    if result.returncode == 0:
        log.ok("Ядро ESP32 установлено")
        return True
    else:
        log.error(f"Ошибка установки ядра: {result.stderr}")
        return False


def compile_ino(
    cli_path: str, work_dir: str, ino_path: str, output_bin: str, log: Logger
) -> bool:
    """Компилирует .ino в .bin."""
    data_dir = os.path.join(work_dir, "arduino_data")
    sketch_dir = os.path.join(work_dir, "sketch")
    os.makedirs(sketch_dir, exist_ok=True)

    # Копируем .ino в папку с именем скетча
    sketch_name = Path(ino_path).stem
    sketch_ino = os.path.join(sketch_dir, f"{sketch_name}.ino")
    shutil.copy2(ino_path, sketch_ino)

    log.info(f"Компилирую {Path(ino_path).name}...")

    result = subprocess.run(
        f'"{cli_path}" compile '
        f'--fqbn esp32:esp32:esp32 '
        f'--config-dir "{data_dir}" '
        f'"{sketch_dir}"',
        shell=True, cwd=work_dir, capture_output=True, text=True, timeout=300
    )

    if result.returncode != 0:
        log.error("Ошибка компиляции:")
        # Выводим только человеческие ошибки
        stderr = result.stderr
        if stderr:
            for line in stderr.splitlines():
                if "error" in line.lower() or "ошибка" in line.lower():
                    log.error(f"  {line.strip()}")
        return False

    # Ищем скомпилированный bin
    build_dir = os.path.join(sketch_dir, "build")
    for root, _dirs, files in os.walk(build_dir if os.path.exists(build_dir) else sketch_dir):
        for fn in files:
            if fn.lower().endswith(".bin"):
                src = os.path.join(root, fn)
                shutil.copy2(src, output_bin)
                log.ok(f"Компиляция завершена: {Path(output_bin).name}")
                return True

    log.error("Файл .bin не найден после компиляции")
    return False


# ======================== PlatformIO ========================

PIO_CMD = "platformio"  # пробуем system-wide


def find_platformio_ini(directory: str) -> str | None:
    """Ищет platformio.ini в директории (рекурсивно)."""
    for root, _dirs, files in os.walk(directory):
        for fn in files:
            if fn.lower() == "platformio.ini":
                return os.path.join(root, fn)
    return None


def ensure_platformio(work_dir: str, log: Logger) -> bool:
    """Устанавливает PlatformIO через pip."""
    try:
        result = subprocess.run(
            f"{PIO_CMD} --version", shell=True, capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            ver = result.stdout.strip()
            log.ok(f"PlatformIO найден: {ver}")
            return True
        else:
            log.info(f"PlatformIO не найден в системе (returncode={result.returncode})")
    except FileNotFoundError:
        log.info("PlatformIO не найден в PATH")
    except Exception as e:
        log.info(f"Ошибка проверки PlatformIO: {e}")

    log.info("Устанавливаю PlatformIO (это может занять несколько минут)...")
    result = subprocess.run(
        "pip install platformio", shell=True, capture_output=True, text=True, timeout=300
    )
    if result.returncode == 0:
        log.ok("PlatformIO установлен")
        return True

    log.error("Не удалось установить PlatformIO")
    return False


def compile_platformio(pio_ini_path: str, output_bin: str, log: Logger) -> bool:
    """Компилирует PlatformIO проект."""
    project_dir = os.path.dirname(pio_ini_path)
    log.info(f"Компилирую PlatformIO проект...")
    log.info(f"  Папка: {project_dir}")

    result = subprocess.run(
        f'platformio run -d "{project_dir}"',
        shell=True, capture_output=True, text=True, timeout=600
    )

    # Выводим ВСЕ логи для отладки
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    if stdout:
        log.info(f"  stdout: {stdout[:500]}")
    if stderr:
        log.info(f"  stderr: {stderr[:500]}")

    if result.returncode != 0:
        log.error("Ошибка компиляции PlatformIO:")
        full_output = stdout + "\n" + stderr
        for line in full_output.splitlines():
            if any(kw in line.lower() for kw in ["error", "fatal", "ошибка", "не зна", "not found", "failed"]):
                log.error(f"  {line.strip()}")
        return False

    # Ищем firmware.bin в .pio/build/*/
    pio_dir = os.path.join(project_dir, ".pio")
    for root, _dirs, files in os.walk(pio_dir):
        for fn in files:
            if fn.lower() == "firmware.bin":
                src = os.path.join(root, fn)
                shutil.copy2(src, output_bin)
                log.ok(f"PlatformIO компиляция завершена: {Path(output_bin).name}")
                return True

    log.error("firmware.bin не найден в .pio/build")
    return False


# ======================== Esptool ========================


def ensure_esptool(work_dir: str, log: Logger) -> str | None:
    """Устанавливает esptool через pip в локальную директорию."""
    # Пробуем системный esptool
    try:
        result = subprocess.run(
            "esptool.py --version", shell=True, capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            log.ok("esptool найден")
            return "esptool.py"
    except Exception:
        pass

    log.info("Устанавливаю esptool...")
    target = os.path.join(work_dir, "esptool_env")
    result = subprocess.run(
        f'pip install esptool --target "{target}"',
        shell=True, capture_output=True, text=True, timeout=120
    )
    if result.returncode == 0:
        # Ищем esptool.py
        for root, _dirs, files in os.walk(target):
            for fn in files:
                if fn == "esptool.py" or fn == "__main__.py":
                    if "esptool" in root.lower():
                        log.ok("esptool установлен")
                        # Возвращаем python -m esptool
                        return "esptool.py"

    # Пробуем pip install глобально
    result2 = subprocess.run(
        "pip install esptool", shell=True, capture_output=True, text=True, timeout=120
    )
    if result2.returncode == 0:
        log.ok("esptool установлен глобально")
        return "esptool.py"

    log.error("Не удалось установить esptool")
    return None


def flash_bin(esptool_cmd: str, port: str, bin_path: str, log: Logger) -> bool:
    """Прошивает bin файл на ESP32."""
    log.info(f"Стираю память ESP32...")
    result = subprocess.run(
        f'{esptool_cmd} --port {port} erase_flash',
        shell=True, capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        log.warn(f"Стирание не удалось (продолжаю): {result.stderr}")

    log.info(f"Записываю прошивку...")
    # Стандартный адрес для ESP32
    result = subprocess.run(
        f'{esptool_cmd} --port {port} --baud 921600 '
        f'write_flash -z 0x10000 "{bin_path}"',
        shell=True, capture_output=True, text=True, timeout=300
    )

    if result.returncode != 0:
        log.error(f"Ошибка записи: {result.stderr}")
        return False

    log.ok("Прошивка записана!")

    # Сброс платы
    log.info("Перезагружаю плату...")
    try:
        ser = serial.Serial(port, baudrate=115200, timeout=1)
        ser.setDTR(False)
        ser.setRTS(True)
        time.sleep(0.2)
        ser.setRTS(False)
        time.sleep(0.2)
        ser.setDTR(True)
        ser.close()
        log.ok("Плата перезапущена")
    except Exception:
        log.warn("Не удалось перезагрузить плату автоматически — нажмите RESET вручную")

    return True


# ======================== Главный процесс ========================

class AppState:
    NONE = "none"       # серый — ничего
    SEARCHING = "searching"  # жёлтый — ищу плату
    COMPILING = "compiling"  # жёлтый — компиляция
    FLASHING = "flashing"    # синий — прошивка
    DONE = "done"            # зелёный — готово
    ERROR = "error"          # красный — ошибка


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("ESP Flasher — Прошивка ESP32 для новичков")
        self.root.geometry("750x600")
        self.root.resizable(True, True)

        # Проверка прав администратора
        if not is_admin():
            result = messagebox.askyesno(
                "Нужны права администратора",
                "Для установки драйверов нужны права администратора.\n\n"
                "Перезапустить программу от имени администратора?\n\n"
                "(Если драйверы уже стоят — можно нажать «Нет»)"
            )
            if result:
                run_as_admin()
                sys.exit(0)
            # Если пользователь отказался — продолжаем, но драйверы не ставим

        self.work_dir = os.path.join(tempfile.gettempdir(), "esp_flasher_work")
        os.makedirs(self.work_dir, exist_ok=True)

        self.log_text_widget: tk.Text | None = None
        self.logger: Logger | None = None
        self.state = AppState.NONE
        self.busy = False

        self._build_ui()
        self._set_state(AppState.NONE, "Выберите файл или вставьте ссылку")

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=15)
        main.pack(fill=tk.BOTH, expand=True)

        # Заголовок
        title = tk.Label(
            main, text="🔧 ESP Flasher",
            font=("Segoe UI", 22, "bold")
        )
        title.pack(pady=(0, 5))

        subtitle = tk.Label(
            main, text="Вставил ссылку или выбрал файл — всё остальное само",
            font=("Segoe UI", 10), fg="#666"
        )
        subtitle.pack(pady=(0, 4))

        # Бейдж админ-прав
        admin_status = "🛡️ Администратор" if is_admin() else "⚠️ Без прав админа (драйверы не ставятся)"
        admin_color = "#2E7D32" if is_admin() else "#E65100"
        admin_label = tk.Label(
            main, text=admin_status, font=("Segoe UI", 9, "bold"), fg=admin_color
        )
        admin_label.pack(pady=(0, 11))

        # Статус-бар (цветной)
        self.status_frame = tk.Frame(main)
        self.status_frame.pack(fill=tk.X, pady=(0, 10))

        self.status_icon = tk.Label(
            self.status_frame, text="●", font=("Segoe UI", 18), fg="#999"
        )
        self.status_icon.pack(side=tk.LEFT)

        self.status_label = tk.Label(
            self.status_frame, text="Готов к работе",
            font=("Segoe UI", 12), fg="#666"
        )
        self.status_label.pack(side=tk.LEFT, padx=10)

        # Поле ссылки
        link_frame = tk.Frame(main)
        link_frame.pack(fill=tk.X, pady=(0, 8))

        tk.Label(link_frame, text="🔗 Ссылка на GitHub или прямой .bin:", font=("Segoe UI", 9)).pack(anchor=tk.W)
        self.url_entry = ttk.Entry(link_frame, font=("Segoe UI", 10))
        self.url_entry.pack(fill=tk.X, pady=(2, 0))
        self.url_entry.insert(0, "Вставь ссылку сюда и нажми Enter...")
        self.url_entry.bind("<FocusIn>", self._on_url_focus_in)
        # Enter = сразу прошить
        self.url_entry.bind("<Return>", lambda e: self._on_enter())

        # Кнопки
        btn_frame = tk.Frame(main)
        btn_frame.pack(fill=tk.X, pady=(5, 10))

        self.btn_select = ttk.Button(
            btn_frame, text="📁 Выбрать файл (.bin / .zip / .ino)", command=self._select_file
        )
        self.btn_select.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))

        self.btn_flash = ttk.Button(
            btn_frame, text="⚡ Прошить!", command=self._start_flash, state=tk.DISABLED
        )
        self.btn_flash.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(5, 0))
        self.btn_flash.configure(style="Accent.TButton")

        # Прогресс
        self.progress = ttk.Progressbar(main, mode="indeterminate")
        self.progress.pack(fill=tk.X, pady=(0, 8))

        # Лог
        tk.Label(main, text="Лог:", font=("Segoe UI", 9)).pack(anchor=tk.W)
        log_frame = tk.Frame(main)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text_widget = tk.Text(
            log_frame, height=16, font=("Consolas", 9),
            state=tk.DISABLED, wrap=tk.WORD, bg="#f9f9f9"
        )
        scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text_widget.yview)
        self.log_text_widget.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text_widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.logger = Logger(self.log_text_widget)

        # Стиль для кнопки
        style = ttk.Style()
        style.configure("Accent.TButton", font=("Segoe UI", 11, "bold"))

    def _set_state(self, state: str, message: str):
        self.state = state
        colors = {
            AppState.NONE: "#999",
            AppState.SEARCHING: "#e6a817",
            AppState.COMPILING: "#e6a817",
            AppState.FLASHING: "#2196F3",
            AppState.DONE: "#4CAF50",
            AppState.ERROR: "#f44336",
        }
        messages = {
            AppState.NONE: message,
            AppState.SEARCHING: "Ищу ESP32...",
            AppState.COMPILING: "Компилирую...",
            AppState.FLASHING: "Прошиваю...",
            AppState.DONE: "Готово! ✅",
            AppState.ERROR: message,
        }
        self.status_icon.configure(fg=colors.get(state, "#999"))
        self.status_label.configure(text=messages.get(state, message), fg=colors.get(state, "#999"))

    def _log_enable(self):
        if self.log_text_widget:
            self.log_text_widget.configure(state=tk.NORMAL)

    def _log_disable(self):
        if self.log_text_widget:
            self.log_text_widget.configure(state=tk.DISABLED)

    def _on_url_focus_in(self, event=None):
        """Убираю подсказку при фокусе."""
        if self.url_entry.get() == "Вставь ссылку сюда и нажми Enter...":
            self.url_entry.delete(0, tk.END)

    def _on_enter(self):
        """Enter в поле ссылки — сразу запускаю прошивку."""
        url = self.url_entry.get().strip()
        if not url or url == "Вставь ссылку сюда и нажми Enter...":
            return
        if self.busy:
            return
        self.selected_file = None
        self._log_enable()
        self.logger.info(f"Ссылка принята: {url}")
        self._start_flash()

    def _select_file(self):
        path = filedialog.askopenfilename(
            title="Выберите файл прошивки",
            filetypes=[
                ("Файлы прошивок", "*.bin *.zip *.ino"),
                ("Все файлы", "*.*"),
            ]
        )
        if path:
            self.selected_file = path
            self.url_entry.delete(0, tk.END)
            self.url_entry.insert(0, "Вставь ссылку сюда и нажми Enter...")
            self.btn_flash.configure(state=tk.NORMAL)
            self.logger.info(f"Выбран файл: {Path(path).name}")

    def _start_flash(self):
        if self.busy:
            return

        self.busy = True
        self.btn_flash.configure(state=tk.DISABLED)
        self.btn_select.configure(state=tk.DISABLED)
        self.progress.start(10)

        thread = threading.Thread(target=self._flash_worker, daemon=True)
        thread.start()

    def _flash_worker(self):
        self._log_enable()
        log = self.logger
        log.info("=" * 50)
        log.info("Запускаю процесс прошивки...")

        bin_path = None

        try:
            # 1. Определяем источник
            url = self.url_entry.get().strip()
            self.selected_file = getattr(self, "selected_file", None)

            if url and not self.selected_file:
                log.info(f"Работаю со ссылкой: {url}")
                self._set_state(AppState.SEARCHING, "Скачиваю...")

                if ".bin" in url.lower():
                    bin_path = os.path.join(self.work_dir, "firmware.bin")
                    if not download_bin(url, bin_path, log):
                        self._set_state(AppState.ERROR, "Не удалось скачать файл")
                        return
                else:
                    # 1. Пробуем найти bin через GitHub API releases
                    bin_url = guess_bin_url(url)
                    if bin_url:
                        bin_path = os.path.join(self.work_dir, "firmware.bin")
                        if not download_bin(bin_url, bin_path, log):
                            self._set_state(AppState.ERROR, "Не удалось скачать прошивку")
                            return
                    else:
                        # 2. Релиз пустой — скачиваю репозиторий и ищу platformio.ini / .ino
                        log.info("В релизах нет .bin — скачиваю исходники...")
                        # Нормализуем: убираем /tree/xxx /blob/xxx
                        norm_url = normalize_github_url(url)
                        log.info(f"Репозиторий: {norm_url}")
                        m = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", norm_url)
                        if not m:
                            self._set_state(AppState.ERROR, f"Не понял ссылку: {url}")
                            return
                        owner, repo = m.group(1), m.group(2)
                        src_zip = os.path.join(self.work_dir, "repo_source.zip")
                        src_url = f"https://github.com/{owner}/{repo}/archive/refs/heads/main.zip"
                        # Пробуем main, потом master
                        downloaded = False
                        for branch in ["main", "master"]:
                            try_url = f"https://github.com/{owner}/{repo}/archive/refs/heads/{branch}.zip"
                            log.info(f"  Пробую ветку: {branch}")
                            if download_bin(try_url, src_zip, log):
                                downloaded = True
                                break
                        if not downloaded:
                            self._set_state(AppState.ERROR, "Не удалось скачать репозиторий")
                            return

                        log.info(f"Распаковываю репозиторий...")

                        extract_dir = os.path.join(self.work_dir, "repo_source")
                        bins = extract_from_zip(src_zip, extract_dir, log)
                        if bins:
                            bin_path = bins[0]
                            log.ok(f"Найден BIN в репозитории: {Path(bin_path).name}")
                        else:
                            # Проверяю PlatformIO
                            pio_ini = find_platformio_ini(extract_dir)
                            if pio_ini:
                                log.ok("Найден PlatformIO проект в репозитории!")
                                if not ensure_platformio(self.work_dir, log):
                                    self._set_state(AppState.ERROR, "Не удалось установить PlatformIO")
                                    return
                                self._set_state(AppState.COMPILING, "Компилирую PlatformIO...")
                                bin_path = os.path.join(self.work_dir, "pio_firmware.bin")
                                if not compile_platformio(pio_ini, bin_path, log):
                                    self._set_state(AppState.ERROR, "Ошибка компиляции PlatformIO")
                                    return
                            else:
                                # Проверяю .ino
                                ino = find_ino_file(extract_dir)
                                if ino:
                                    log.ok(f"Найден скетч: {Path(ino).name}")
                                    cli = ensure_arduino_cli(self.work_dir, log)
                                    if not cli:
                                        self._set_state(AppState.ERROR, "Не удалось скачать Arduino CLI")
                                        return
                                    install_esp32_core(cli, self.work_dir, log)
                                    self._set_state(AppState.COMPILING, "Компилирую...")
                                    bin_path = os.path.join(self.work_dir, "compiled_firmware.bin")
                                    if not compile_ino(cli, self.work_dir, ino, bin_path, log):
                                        self._set_state(AppState.ERROR, "Ошибка компиляции")
                                        return
                                else:
                                    self._set_state(AppState.ERROR, "В репозитории нет .bin, .ino или platformio.ini")
                                    return

            elif self.selected_file:
                ext = Path(self.selected_file).suffix.lower()

                if ext == ".bin":
                    bin_path = self.selected_file
                    log.ok(f"BIN файл: {Path(bin_path).name}")

                elif ext == ".zip":
                    log.info("Распаковываю zip...")
                    extract_dir = os.path.join(self.work_dir, "zip_extracted")
                    bins = extract_from_zip(self.selected_file, extract_dir, log)
                    if bins:
                        bin_path = bins[0]
                        log.ok(f"Найден BIN: {Path(bin_path).name}")
                    else:
                        # Проверяю PlatformIO
                        pio_ini = find_platformio_ini(extract_dir)
                        if pio_ini:
                            log.ok("Найден PlatformIO проект!")
                            if not ensure_platformio(self.work_dir, log):
                                self._set_state(AppState.ERROR, "Не удалось установить PlatformIO")
                                return
                            self._set_state(AppState.COMPILING, "Компилирую PlatformIO...")
                            bin_path = os.path.join(self.work_dir, "pio_firmware.bin")
                            if not compile_platformio(pio_ini, bin_path, log):
                                self._set_state(AppState.ERROR, "Ошибка компиляции PlatformIO")
                                return
                        else:
                            self._set_state(AppState.ERROR, "В zip-архиве нет .bin файла и нет platformio.ini")
                            return

                elif ext == ".ino":
                    # Нужен Arduino CLI
                    cli = ensure_arduino_cli(self.work_dir, log)
                    if not cli:
                        self._set_state(AppState.ERROR, "Не удалось скачать Arduino CLI")
                        return

                    install_esp32_core(cli, self.work_dir, log)

                    self._set_state(AppState.COMPILING, "Компилирую...")
                    bin_path = os.path.join(self.work_dir, "compiled_firmware.bin")
                    if not compile_ino(cli, self.work_dir, self.selected_file, bin_path, log):
                        self._set_state(AppState.ERROR, "Ошибка компиляции")
                        return
                else:
                    self._set_state(AppState.ERROR, "Неподдерживаемый тип файла")
                    return
            else:
                self._set_state(AppState.ERROR, "Выберите файл или вставьте ссылку")
                return

            # 2. Ищем ESP32
            self._set_state(AppState.SEARCHING, "Ищу ESP32...")
            log.info("Ищу ESP32...")

            drivers_ok = ensure_drivers(log, self.work_dir)
            ports = find_esp32_ports()

            if not ports:
                log.error("ESP32 не найдена! Проверьте подключение USB-кабеля.")
                self._set_state(AppState.ERROR, "ESP32 не найдена — подключите кабель USB")
                return

            port = ports[0]["port"]
            log.ok(f"Нашёл ESP32 на порту {port}")

            # 3. Esptool
            esptool = ensure_esptool(self.work_dir, log)
            if not esptool:
                self._set_state(AppState.ERROR, "Не удалось установить esptool")
                return

            # 4. Прошивка
            self._set_state(AppState.FLASHING, "Прошиваю...")
            log.info("Начинаю прошивку...")

            if flash_bin(esptool, port, bin_path, log):
                self._set_state(AppState.DONE, "Готово! ✅")
                log.ok("ВСЁ ОК! Плата прошита и перезапущена 🎉")
            else:
                self._set_state(AppState.ERROR, "Ошибка прошивки")
                log.error("Прошивка не завершена")

        except Exception as e:
            log.error(f"Неожиданная ошибка: {e}")
            self._set_state(AppState.ERROR, str(e))
        finally:
            self.progress.stop()
            self.busy = False
            self.btn_select.configure(state=tk.NORMAL)
            self._log_disable()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = App()
    app.run()
