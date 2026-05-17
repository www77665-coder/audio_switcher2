import sys
import json
import ctypes
from pathlib import Path

from PySide6.QtCore import Qt, QObject, QAbstractNativeEventFilter
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu, QWidget, QVBoxLayout, QLabel,
    QPushButton, QHBoxLayout, QKeySequenceEdit, QMessageBox, QCheckBox
)

import comtypes
from comtypes import GUID, COMMETHOD, HRESULT, IUnknown
from ctypes import POINTER, c_float, wintypes, cast
from pycaw.pycaw import AudioUtilities, IMMDeviceEnumerator, EDataFlow, ERole
from comtypes import CLSCTX_ALL


# -----------------------------
# Windows Hotkey API
# -----------------------------
user32 = ctypes.windll.user32
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

HOTKEY_ID_PREV = 1001
HOTKEY_ID_NEXT = 1002
WM_HOTKEY = 0x0312

VK_CODE_MAP = {
    Qt.Key_F1: 0x70, Qt.Key_F2: 0x71, Qt.Key_F3: 0x72, Qt.Key_F4: 0x73,
    Qt.Key_F5: 0x74, Qt.Key_F6: 0x75, Qt.Key_F7: 0x76, Qt.Key_F8: 0x77,
    Qt.Key_F9: 0x78, Qt.Key_F10: 0x79, Qt.Key_F11: 0x7A, Qt.Key_F12: 0x7B,
}

CONFIG_PATH = Path.home() / "AppData" / "Roaming" / "AudioOutputHotkeySwitcher"
CONFIG_FILE = CONFIG_PATH / "config.json"


def ensure_config_dir():
    CONFIG_PATH.mkdir(parents=True, exist_ok=True)


def default_config():
    return {
        "prev_hotkey": "F9",
        "next_hotkey": "F10",
        "start_with_windows": False
    }


def load_config():
    ensure_config_dir()
    if not CONFIG_FILE.exists():
        cfg = default_config()
        save_config(cfg)
        return cfg
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        cfg = default_config()
        save_config(cfg)
        return cfg


def save_config(cfg):
    ensure_config_dir()
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_qkeysequence_to_mod_vk(seq):
    if seq.count() == 0:
        return None, None
    key_int = seq[0]
    qt_key = key_int & 0x01FFFFFF
    mods = Qt.KeyboardModifiers(key_int & 0xFE000000)

    mod_flags = 0
    if mods & Qt.ControlModifier:
        mod_flags |= MOD_CONTROL
    if mods & Qt.AltModifier:
        mod_flags |= MOD_ALT
    if mods & Qt.ShiftModifier:
        mod_flags |= MOD_SHIFT
    if mods & Qt.MetaModifier:
        mod_flags |= MOD_WIN

    vk = None
    if qt_key in VK_CODE_MAP:
        vk = VK_CODE_MAP[qt_key]
    else:
        if Qt.Key_A <= qt_key <= Qt.Key_Z:
            vk = ord(chr(qt_key))
        elif Qt.Key_0 <= qt_key <= Qt.Key_9:
            vk = ord(chr(qt_key))
    return mod_flags, vk


# -----------------------------
# Audio Device Switch
# -----------------------------
class IPolicyConfig(IUnknown):
    _iid_ = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetMixFormat", (["in"], wintypes.LPCWSTR, "pszDeviceName"), (["out"], POINTER(ctypes.c_void_p), "ppFormat")),
        COMMETHOD([], HRESULT, "GetDeviceFormat", (["in"], wintypes.LPCWSTR, "pszDeviceName"), (["in"], wintypes.BOOL, "bDefault"), (["out"], POINTER(ctypes.c_void_p), "ppFormat")),
        COMMETHOD([], HRESULT, "ResetDeviceFormat", (["in"], wintypes.LPCWSTR, "pszDeviceName")),
        COMMETHOD([], HRESULT, "SetDeviceFormat", (["in"], wintypes.LPCWSTR, "pszDeviceName"), (["in"], ctypes.c_void_p, "pEndpointFormat"), (["in"], ctypes.c_void_p, "MixFormat")),
        COMMETHOD([], HRESULT, "GetProcessingPeriod", (["in"], wintypes.LPCWSTR, "pszDeviceName"), (["in"], wintypes.BOOL, "bDefault"), (["out"], POINTER(c_float), "pmftDefaultPeriod"), (["out"], POINTER(c_float), "pmftMinimumPeriod")),
        COMMETHOD([], HRESULT, "SetProcessingPeriod", (["in"], wintypes.LPCWSTR, "pszDeviceName"), (["in"], POINTER(c_float), "pmftPeriod")),
        COMMETHOD([], HRESULT, "GetShareMode", (["in"], wintypes.LPCWSTR, "pszDeviceName"), (["out"], POINTER(ctypes.c_void_p), "pMode")),
        COMMETHOD([], HRESULT, "SetShareMode", (["in"], wintypes.LPCWSTR, "pszDeviceName"), (["in"], ctypes.c_void_p, "mode")),
        COMMETHOD([], HRESULT, "GetPropertyValue", (["in"], wintypes.LPCWSTR, "pszDeviceName"), (["in"], ctypes.c_void_p, "key"), (["out"], ctypes.c_void_p, "pv")),
        COMMETHOD([], HRESULT, "SetPropertyValue", (["in"], wintypes.LPCWSTR, "pszDeviceName"), (["in"], ctypes.c_void_p, "key"), (["in"], ctypes.c_void_p, "pv")),
        COMMETHOD([], HRESULT, "SetDefaultEndpoint", (["in"], wintypes.LPCWSTR, "wszDeviceId"), (["in"], wintypes.DWORD, "role")),
        COMMETHOD([], HRESULT, "SetEndpointVisibility", (["in"], wintypes.LPCWSTR, "wszDeviceId"), (["in"], wintypes.BOOL, "bVisible")),
    ]


class AudioManager:
    CLSID_PolicyConfigClient = GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}")

    def __init__(self):
        self._policy = comtypes.CoCreateInstance(
            self.CLSID_PolicyConfigClient, interface=IPolicyConfig, clsctx=CLSCTX_ALL
        )

    def list_render_devices(self):
        devices = AudioUtilities.GetSpeakers()
        enumerator = devices.Activate(
            IMMDeviceEnumerator._iid_, CLSCTX_ALL, None
        )
        enumerator = cast(enumerator, POINTER(IMMDeviceEnumerator))
        collection = enumerator.EnumAudioEndpoints(EDataFlow.eRender.value, 1)  # DEVICE_STATE_ACTIVE=1
        count = collection.GetCount()
        result = []
        for i in range(count):
            dev = collection.Item(i)
            dev_id = dev.GetId()
            friendly = AudioUtilities.CreateDevice(dev).FriendlyName
            result.append((dev_id, friendly))
        return result

    def get_default_render_device_id(self):
        devices = AudioUtilities.GetSpeakers()
        return devices.id

    def set_default_device(self, device_id):
        for role in (ERole.eConsole.value, ERole.eMultimedia.value, ERole.eCommunications.value):
            self._policy.SetDefaultEndpoint(device_id, role)

    def switch(self, direction=1):
        devs = self.list_render_devices()
        if not devs:
            return None
        current_id = self.get_default_render_device_id()
        idx = 0
        for i, (did, _) in enumerate(devs):
            if did == current_id:
                idx = i
                break
        new_idx = (idx + direction) % len(devs)
        new_id, new_name = devs[new_idx]
        self.set_default_device(new_id)
        return new_name


# -----------------------------
# Native event filter for WM_HOTKEY
# -----------------------------
class HotkeyEventFilter(QAbstractNativeEventFilter):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def nativeEventFilter(self, event_type, message):
        if event_type != b"windows_generic_MSG":
            return False, 0
        msg = ctypes.wintypes.MSG.from_address(message.__int__())
        if msg.message == WM_HOTKEY:
            self.callback(msg.wParam)
            return True, 0
        return False, 0


# -----------------------------
# Main App
# -----------------------------
class SettingsWindow(QWidget):
    def __init__(self, app_ref):
        super().__init__()
        self.app_ref = app_ref
        self.setWindowTitle("音频输出快捷切换 - 设置")
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("上一个设备快捷键："))
        self.prev_edit = QKeySequenceEdit()
        layout.addWidget(self.prev_edit)

        layout.addWidget(QLabel("下一个设备快捷键："))
        self.next_edit = QKeySequenceEdit()
        layout.addWidget(self.next_edit)

        self.auto_start_chk = QCheckBox("开机启动（当前版本仅保存选项）")
        layout.addWidget(self.auto_start_chk)

        btn_row = QHBoxLayout()
        self.btn_apply = QPushButton("保存并应用")
        self.btn_cancel = QPushButton("取消")
        btn_row.addWidget(self.btn_apply)
        btn_row.addWidget(self.btn_cancel)
        layout.addLayout(btn_row)

        self.btn_apply.clicked.connect(self.apply_changes)
        self.btn_cancel.clicked.connect(self.hide)

        self.load_from_config()

    def load_from_config(self):
        cfg = self.app_ref.config
        self.prev_edit.setKeySequence(cfg.get("prev_hotkey", "F9"))
        self.next_edit.setKeySequence(cfg.get("next_hotkey", "F10"))
        self.auto_start_chk.setChecked(cfg.get("start_with_windows", False))

    def apply_changes(self):
        prev_seq = self.prev_edit.keySequence()
        next_seq = self.next_edit.keySequence()

        if prev_seq.toString() == "" or next_seq.toString() == "":
            QMessageBox.warning(self, "错误", "快捷键不能为空")
            return
        if prev_seq.toString() == next_seq.toString():
            QMessageBox.warning(self, "错误", "上一个/下一个快捷键不能相同")
            return

        old_cfg = dict(self.app_ref.config)

        self.app_ref.config["prev_hotkey"] = prev_seq.toString()
        self.app_ref.config["next_hotkey"] = next_seq.toString()
        self.app_ref.config["start_with_windows"] = self.auto_start_chk.isChecked()

        ok, msg = self.app_ref.register_hotkeys()
        if not ok:
            self.app_ref.config = old_cfg
            self.app_ref.register_hotkeys()
            QMessageBox.critical(self, "快捷键冲突", msg)
            return

        save_config(self.app_ref.config)
        QMessageBox.information(self, "成功", "设置已保存并生效")
        self.hide()


class AppController(QObject):
    def __init__(self, qt_app):
        super().__init__()
        self.qt_app = qt_app
        self.config = load_config()
        self.audio = AudioManager()
        self.settings_win = SettingsWindow(self)

        self.tray = QSystemTrayIcon()
        self.tray.setIcon(QIcon())
        self.tray.setToolTip("音频输出快捷切换")

        menu = QMenu()
        act_settings = QAction("设置", menu)
        act_reload = QAction("重载设备列表", menu)
        act_exit = QAction("退出", menu)

        act_settings.triggered.connect(self.show_settings)
        act_reload.triggered.connect(self.reload_devices)
        act_exit.triggered.connect(self.exit_app)

        menu.addAction(act_settings)
        menu.addAction(act_reload)
        menu.addSeparator()
        menu.addAction(act_exit)
        self.tray.setContextMenu(menu)
        self.tray.show()

        self.event_filter = HotkeyEventFilter(self.on_hotkey)
        self.qt_app.installNativeEventFilter(self.event_filter)

        ok, msg = self.register_hotkeys()
        if not ok:
            QMessageBox.critical(None, "启动失败", msg)

        self.tray.showMessage("音频切换器", "已启动：F9 上一个，F10 下一个（可在设置修改）", QSystemTrayIcon.Information, 2500)

    def show_settings(self):
        self.settings_win.load_from_config()
        self.settings_win.show()
        self.settings_win.raise_()
        self.settings_win.activateWindow()

    def reload_devices(self):
        try:
            devs = self.audio.list_render_devices()
            self.tray.showMessage("音频切换器", f"检测到 {len(devs)} 个输出设备", QSystemTrayIcon.Information, 1500)
        except Exception as e:
            self.tray.showMessage("音频切换器", f"重载失败: {e}", QSystemTrayIcon.Critical, 2500)

    def unregister_hotkeys(self):
        user32.UnregisterHotKey(None, HOTKEY_ID_PREV)
        user32.UnregisterHotKey(None, HOTKEY_ID_NEXT)

    def register_hotkeys(self):
        self.unregister_hotkeys()

        prev_seq = self.settings_win.prev_edit.keySequence() if self.settings_win else None
        next_seq = self.settings_win.next_edit.keySequence() if self.settings_win else None

        if prev_seq is None or prev_seq.toString() == "":
            prev_seq = self.config.get("prev_hotkey", "F9")
            from PySide6.QtGui import QKeySequence
            prev_seq = QKeySequence(prev_seq)

        if next_seq is None or next_seq.toString() == "":
            next_seq = self.config.get("next_hotkey", "F10")
            from PySide6.QtGui import QKeySequence
            next_seq = QKeySequence(next_seq)

        p_mod, p_vk = parse_qkeysequence_to_mod_vk(prev_seq)
        n_mod, n_vk = parse_qkeysequence_to_mod_vk(next_seq)

        if p_vk is None or n_vk is None:
            return False, "只支持 F键/字母/数字 作为主键"

        ok_prev = user32.RegisterHotKey(None, HOTKEY_ID_PREV, p_mod, p_vk)
        if not ok_prev:
            return False, f"快捷键 {prev_seq.toString()} 注册失败，可能被其他软件占用"

        ok_next = user32.RegisterHotKey(None, HOTKEY_ID_NEXT, n_mod, n_vk)
        if not ok_next:
            user32.UnregisterHotKey(None, HOTKEY_ID_PREV)
            return False, f"快捷键 {next_seq.toString()} 注册失败，可能被其他软件占用"

        return True, "ok"

    def on_hotkey(self, hotkey_id):
        try:
            if hotkey_id == HOTKEY_ID_PREV:
                name = self.audio.switch(direction=-1)
                if name:
                    self.tray.showMessage("已切换", f"上一个：{name}", QSystemTrayIcon.Information, 1200)
            elif hotkey_id == HOTKEY_ID_NEXT:
                name = self.audio.switch(direction=1)
                if name:
                    self.tray.showMessage("已切换", f"下一个：{name}", QSystemTrayIcon.Information, 1200)
        except Exception as e:
            self.tray.showMessage("错误", f"切换失败: {e}", QSystemTrayIcon.Critical, 2500)

    def exit_app(self):
        self.unregister_hotkeys()
        self.tray.hide()
        self.qt_app.quit()


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    controller = AppController(app)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
