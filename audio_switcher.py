# -*- coding: utf-8 -*-
import ctypes
import json
import os
import sys
import threading
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Qt
from PySide6.QtGui import QAction, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QInputDialog,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
)

# =========================
# Windows 常量 / API
# =========================

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WM_HOTKEY = 0x0312

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

# MMDevice API GUID（仅用于切换默认设备）
# 说明：这里采用调用第三方工具 SoundVolumeView 的方式切换默认设备，更稳定简单。
#       本脚本只负责设备列表展示和热键逻辑。
# 你需要把 SoundVolumeView.exe 放到脚本同目录（或加入 PATH）。


# =========================
# 工具函数
# =========================

def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def config_path():
    return os.path.join(app_dir(), "config.json")


def load_config():
    path = config_path()
    if not os.path.exists(path):
        return {
            "hotkey": "Ctrl+Alt+S",
            "device_a": "",
            "device_b": "",
            "autorun": False
        }
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "hotkey": "Ctrl+Alt+S",
            "device_a": "",
            "device_b": "",
            "autorun": False
        }


def save_config(cfg):
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def find_soundvolumeview():
    exe_name = "SoundVolumeView.exe"
    local = os.path.join(app_dir(), exe_name)
    if os.path.exists(local):
        return local
    return exe_name  # 走 PATH


def run_cmd(cmd):
    import subprocess
    p = subprocess.run(cmd, capture_output=True, text=True, shell=False)
    return p.returncode, p.stdout, p.stderr


def list_render_devices():
    """
    用 SoundVolumeView /sjson 导出 JSON，然后筛选输出设备。
    返回: [{'name': xxx, 'id': xxx}, ...]
    """
    svv = find_soundvolumeview()
    temp_json = os.path.join(app_dir(), "_svv_devices.json")
    code, out, err = run_cmd([svv, "/sjson", temp_json])
    if code != 0:
        raise RuntimeError(f"SoundVolumeView 调用失败: {err or out}")

    if not os.path.exists(temp_json):
        raise RuntimeError("未生成设备列表 JSON。")

    with open(temp_json, "r", encoding="utf-8", errors="ignore") as f:
        data = json.load(f)

    try:
        os.remove(temp_json)
    except Exception:
        pass

    devices = []
    for item in data:
        # Direction: "Render" 表示播放设备
        if str(item.get("Direction", "")).lower() != "render":
            continue
        name = item.get("Device Name") or item.get("Name") or ""
        dev_id = item.get("Command-Line Friendly ID") or item.get("Item ID") or ""
        if name and dev_id:
            devices.append({"name": name, "id": dev_id})

    return devices


def set_default_device(device_id):
    """
    通过 SoundVolumeView 切默认设备。
    role 0/1/2 全设，保证系统和通信场景都切过去。
    """
    svv = find_soundvolumeview()
    # 0 = Console, 1 = Multimedia, 2 = Communications
    for role in ("0", "1", "2"):
        code, out, err = run_cmd([svv, "/SetDefault", device_id, role])
        if code != 0:
            raise RuntimeError(f"设置默认设备失败(role={role}): {err or out}")


def get_default_device_id():
    """
    从 SoundVolumeView 导出中找 Default = Yes 的 Render 设备。
    """
    devices = list_render_devices()
    svv = find_soundvolumeview()
    temp_json = os.path.join(app_dir(), "_svv_devices2.json")
    code, out, err = run_cmd([svv, "/sjson", temp_json])
    if code != 0:
        return ""

    try:
        with open(temp_json, "r", encoding="utf-8", errors="ignore") as f:
            data = json.load(f)
    except Exception:
        return ""
    finally:
        try:
            os.remove(temp_json)
        except Exception:
            pass

    default_ids = set()
    for item in data:
        if str(item.get("Direction", "")).lower() != "render":
            continue
        if str(item.get("Default", "")).lower() == "yes":
            dev_id = item.get("Command-Line Friendly ID") or item.get("Item ID") or ""
            if dev_id:
                default_ids.add(dev_id)

    for d in devices:
        if d["id"] in default_ids:
            return d["id"]
    return ""


def parse_qkeysequence_to_mod_vk(seq: QKeySequence):
    """
    兼容 PySide6:
    seq[0] 是 QKeyCombination，不能再和 int 做位运算。
    """
    if not seq or seq.count() == 0:
        return None, None

    combo = seq[0]
    mods = combo.keyboardModifiers()
    key = combo.key()

    mod = 0
    if mods & Qt.KeyboardModifier.ControlModifier:
        mod |= MOD_CONTROL
    if mods & Qt.KeyboardModifier.AltModifier:
        mod |= MOD_ALT
    if mods & Qt.KeyboardModifier.ShiftModifier:
        mod |= MOD_SHIFT
    if mods & Qt.KeyboardModifier.MetaModifier:
        mod |= MOD_WIN

    vk = int(key)
    return mod, vk


def set_autorun(enabled: bool):
    import winreg
    app_name = "AudioSwitcher"
    exe_path = sys.executable if getattr(sys, "frozen", False) else f'"{sys.executable}" "{os.path.abspath(__file__)}"'
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS) as key:
        if enabled:
            winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, exe_path)
        else:
            try:
                winreg.DeleteValue(key, app_name)
            except FileNotFoundError:
                pass


# =========================
# 热键事件过滤
# =========================

class HotkeyEventFilter(QAbstractNativeEventFilter):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def nativeEventFilter(self, eventType, message):
        if eventType != b"windows_generic_MSG":
            return False, 0
        msg = wintypes.MSG.from_address(int(message))
        if msg.message == WM_HOTKEY:
            self.callback()
            return True, 0
        return False, 0


# =========================
# 主程序
# =========================

class AudioSwitcher(QObject):
    def __init__(self, app: QApplication):
        super().__init__()
        self.app = app
        self.cfg = load_config()
        self.devices = []
        self.hotkey_id = 1001
        self.event_filter = HotkeyEventFilter(self.on_hotkey)

        self.tray = QSystemTrayIcon()
        self.tray.setIcon(QIcon())
        self.tray.setToolTip("Audio Switcher")

        self.menu = QMenu()
        self.action_switch = QAction("切换设备")
        self.action_switch.triggered.connect(self.switch_device)

        self.action_pick_a = QAction("选择设备 A")
        self.action_pick_a.triggered.connect(lambda: self.pick_device("device_a"))

        self.action_pick_b = QAction("选择设备 B")
        self.action_pick_b.triggered.connect(lambda: self.pick_device("device_b"))

        self.action_set_hotkey = QAction("设置热键")
        self.action_set_hotkey.triggered.connect(self.set_hotkey)

        self.action_autorun = QAction("开机自启")
        self.action_autorun.setCheckable(True)
        self.action_autorun.setChecked(bool(self.cfg.get("autorun", False)))
        self.action_autorun.triggered.connect(self.toggle_autorun)

        self.action_refresh = QAction("刷新设备列表")
        self.action_refresh.triggered.connect(self.refresh_devices)

        self.action_quit = QAction("退出")
        self.action_quit.triggered.connect(self.quit_app)

        self.menu.addAction(self.action_switch)
        self.menu.addSeparator()
        self.menu.addAction(self.action_pick_a)
        self.menu.addAction(self.action_pick_b)
        self.menu.addAction(self.action_set_hotkey)
        self.menu.addAction(self.action_autorun)
        self.menu.addSeparator()
        self.menu.addAction(self.action_refresh)
        self.menu.addSeparator()
        self.menu.addAction(self.action_quit)

        self.tray.setContextMenu(self.menu)
        self.tray.show()

        self.refresh_devices(show_message=False)
        self.register_hotkeys()

        self.tray.showMessage(
            "Audio Switcher",
            f"已启动，热键: {self.cfg.get('hotkey', 'Ctrl+Alt+S')}",
            QSystemTrayIcon.Information,
            2000
        )

    def refresh_devices(self, show_message=True):
        try:
            self.devices = list_render_devices()
            if show_message:
                self.tray.showMessage("Audio Switcher", f"已刷新设备，共 {len(self.devices)} 个。", QSystemTrayIcon.Information, 1500)
        except Exception as e:
            QMessageBox.critical(None, "错误", f"刷新设备失败：\n{e}")

    def device_name_by_id(self, device_id):
        for d in self.devices:
            if d["id"] == device_id:
                return d["name"]
        return "(未选择)"

    def pick_device(self, key_name):
        if not self.devices:
            QMessageBox.warning(None, "提示", "没有可用播放设备，请先刷新。")
            return
        names = [f'{d["name"]} | {d["id"]}' for d in self.devices]
        current_id = self.cfg.get(key_name, "")
        current_index = 0
        for i, d in enumerate(self.devices):
            if d["id"] == current_id:
                current_index = i
                break

        selected, ok = QInputDialog.getItem(
            None,
            "选择设备",
            f"请选择 {key_name}:",
            names,
            current_index,
            False
        )
        if ok and selected:
            idx = names.index(selected)
            self.cfg[key_name] = self.devices[idx]["id"]
            save_config(self.cfg)
            self.tray.showMessage("Audio Switcher", f"{key_name} 已设置为：{self.devices[idx]['name']}", QSystemTrayIcon.Information, 1500)

    def set_hotkey(self):
        current = self.cfg.get("hotkey", "Ctrl+Alt+S")
        text, ok = QInputDialog.getText(None, "设置热键", "输入热键（例：Ctrl+Alt+S）", text=current)
        if ok and text.strip():
            seq = QKeySequence(text.strip())
            mod, vk = parse_qkeysequence_to_mod_vk(seq)
            if mod is None or vk is None:
                QMessageBox.warning(None, "提示", "热键格式无效。")
                return
            self.unregister_hotkeys()
            self.cfg["hotkey"] = text.strip()
            save_config(self.cfg)
            if not self.register_hotkeys():
                QMessageBox.critical(None, "错误", "热键注册失败，可能被占用。")
            else:
                self.tray.showMessage("Audio Switcher", f"热键已更新：{text.strip()}", QSystemTrayIcon.Information, 1500)

    def register_hotkeys(self):
        seq = QKeySequence(self.cfg.get("hotkey", "Ctrl+Alt+S"))
        mod, vk = parse_qkeysequence_to_mod_vk(seq)
        if mod is None or vk is None:
            return False

        self.app.installNativeEventFilter(self.event_filter)
        if not user32.RegisterHotKey(None, self.hotkey_id, mod, vk):
            return False
        return True

    def unregister_hotkeys(self):
        try:
            user32.UnregisterHotKey(None, self.hotkey_id)
        except Exception:
            pass
        try:
            self.app.removeNativeEventFilter(self.event_filter)
        except Exception:
            pass

    def on_hotkey(self):
        threading.Thread(target=self.switch_device, daemon=True).start()

    def switch_device(self):
        try:
            device_a = self.cfg.get("device_a", "")
            device_b = self.cfg.get("device_b", "")
            if not device_a or not device_b:
                self.tray.showMessage("Audio Switcher", "请先设置设备 A 和设备 B。", QSystemTrayIcon.Warning, 2000)
                return

            current = get_default_device_id()
            target = device_b if current == device_a else device_a
            set_default_device(target)
            target_name = self.device_name_by_id(target)
            self.tray.showMessage("Audio Switcher", f"已切换到：{target_name}", QSystemTrayIcon.Information, 1200)
        except Exception as e:
            self.tray.showMessage("Audio Switcher", f"切换失败：{e}", QSystemTrayIcon.Critical, 3000)

    def toggle_autorun(self):
        enabled = self.action_autorun.isChecked()
        try:
            set_autorun(enabled)
            self.cfg["autorun"] = bool(enabled)
            save_config(self.cfg)
        except Exception as e:
            QMessageBox.critical(None, "错误", f"设置开机自启失败：\n{e}")
            self.action_autorun.setChecked(not enabled)

    def quit_app(self):
        self.unregister_hotkeys()
        self.tray.hide()
        self.app.quit()


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    try:
        _ = AudioSwitcher(app)
    except Exception as e:
        QMessageBox.critical(None, "启动失败", str(e))
        return 1

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
