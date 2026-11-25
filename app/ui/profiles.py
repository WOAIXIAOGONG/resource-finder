"""Local-save entry screen and the login / logout window lifecycle."""
from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import (QCheckBox, QDialogButtonBox, QFormLayout,
                               QLineEdit, QMessageBox)
from .controls import WheelSafeComboBox
from .dialog_design import StyledDialog, label, primary, install_message_style
from .. import config
from ..profiles import ProfileStore


class ProfileDialog(StyledDialog):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store, self.session = store, None
        self.setWindowTitle('资源搜寻器 · 本地存档')
        root = self.make_root('进入你的资源空间', '输入用户名，继续上次配置或创建一个新的本地存档。', 'library', 560)
        form = QFormLayout()
        self.name_edit = QLineEdit(self)
        self.name_edit.setMaxLength(32)
        self.name_edit.setPlaceholderText('输入用户名，例如：小明')
        form.addRow('用户名', self.name_edit)
        self.existing = WheelSafeComboBox(self)
        self.existing.addItem('选择已有存档（可选）', '')
        try:
            for name in store.names():
                self.existing.addItem(name, name)
        except OSError:
            pass
        self.existing.currentIndexChanged.connect(self._choose)
        form.addRow('已有存档', self.existing)
        root.addLayout(form)
        self.import_check = QCheckBox('新建时导入旧版已有配置', self)
        self.import_check.setVisible(store.legacy_path() is not None)
        root.addWidget(self.import_check)
        root.addWidget(label('无需联网、无需密码。新存档默认使用初始配置；站点、黑名单、历史和设置分别保存。', parent=self))
        root.addWidget(label('存档位置：' + str(store.root), parent=self))
        self.error_label = label('', 'DialogNotice', self)
        self.error_label.hide()
        root.addWidget(self.error_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        self.enter_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.enter_button.setText('进入 / 新建存档')
        primary(self.enter_button)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('退出工具')
        buttons.accepted.connect(self._enter)
        buttons.rejected.connect(self.reject)
        self.name_edit.returnPressed.connect(self._enter)
        self.name_edit.textChanged.connect(self._name_changed)
        self.finish_layout(root, buttons)
        self._name_changed()

    def _choose(self):
        name = self.existing.currentData()
        if name:
            self.name_edit.setText(name)

    def _name_changed(self):
        try:
            exists = self.store.exists(self.name_edit.text())
        except ValueError:
            exists = False
        self.enter_button.setText('进入存档' if exists else '新建并进入')
        self.import_check.setEnabled(not exists)
        self.enter_button.setEnabled(bool(self.name_edit.text().strip()))

    def _enter(self):
        if self.session:
            return
        try:
            self.session = self.store.open(self.name_edit.text(),
                self.import_check.isEnabled() and self.import_check.isChecked())
        except (ValueError, OSError, KeyError, TypeError) as exc:
            self.error_label.setText('无法进入存档：' + str(exc))
            self.error_label.show()
            return
        self.accept()


class ProfileController(QObject):
    def __init__(self, app, store=None):
        super().__init__(app)
        self.app, self.store = app, store or ProfileStore()
        self.session = self.window = self.dialog = None
        app.setQuitOnLastWindowClosed(False)
        install_message_style()

    def start(self):
        self.dialog = ProfileDialog(self.store)
        self.dialog.accepted.connect(self._entered)
        self.dialog.rejected.connect(self.app.quit)
        self.dialog.show()
        self.dialog.name_edit.setFocus()

    def _entered(self):
        from .mainwindow import MainWindow
        self.session = self.dialog.session
        config.use_profile(str(self.session.directory))
        try:
            self.window = MainWindow(self.session.config)
        except Exception as exc:
            self.session.release()
            self.session = None
            config.use_profile(None)
            QMessageBox.warning(self.dialog, '无法打开工具', str(exc))
            self.dialog.deleteLater()
            QTimer.singleShot(0, self.start)
            return
        self.window.session_closed.connect(self._closed)
        if not self.window.settings.window_geometry:
            available = self.window.screen().availableGeometry()
            self.window.resize(min(self.window.width(), available.width()-60),
                               min(self.window.height(), available.height()-80))
        self.window.show()
        self.dialog.deleteLater()
        self.dialog = None

    def _closed(self, logout):
        window, self.window = self.window, None
        if window:
            window.deleteLater()
        if self.session:
            self.session.release()
            self.session = None
        config.use_profile(None)
        if logout:
            QTimer.singleShot(0, self.start)
        else:
            self.app.quit()
