"""Nonblocking release check and user-confirmed update UI."""
import sys
import threading
from PySide6.QtCore import QObject, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QCheckBox,QHBoxLayout,QMessageBox,QProgressBar,QPushButton,QTextEdit
from .dialog_design import StyledDialog,label,primary
from ..config import APP_VERSION
from ..updates import UpdateSettings,ReleaseClient,prepare_update,launch_update,UpdateError,UpdateCancelled

class UpdateSignals(QObject):
    result=Signal(str,object)
    progress=Signal(int,int)

class UpdateDialog(StyledDialog):
    def __init__(self,parent,automatic=False):
        super().__init__(parent)
        self.setWindowTitle('检查更新')
        self.store=UpdateSettings();self.settings=parent.settings
        self.automatic=automatic;self.cancel=threading.Event();self.busy=False;self.release=None
        self.signals=UpdateSignals(self);self.signals.result.connect(self.finished_work)
        self.signals.progress.connect(self.on_progress)
        cancel=self.cancel;self.destroyed.connect(lambda:cancel.set())
        root=self.make_root('软件更新','当前版本 '+APP_VERSION+' · 用户确认后才会替换程序','refresh',650)
        self.status=label('通过 GitHub 检查正式发布版本');root.addWidget(self.status)
        self.notes=QTextEdit(self);self.notes.setReadOnly(True);self.notes.setMinimumHeight(130);root.addWidget(self.notes);self.notes.hide()
        self.progress=QProgressBar(self);self.progress.hide();root.addWidget(self.progress)
        self.auto=QCheckBox('启动时自动检查新版本',self);self.auto.setChecked(self.store.automatic)
        self.auto.toggled.connect(self.save_automatic);root.addWidget(self.auto)
        buttons=QHBoxLayout();self.close_button=QPushButton('暂不更新',self);self.close_button.clicked.connect(self.reject)
        self.check_button=QPushButton('重新检查',self);self.check_button.clicked.connect(self.check)
        self.install_button=QPushButton('下载并更新',self);primary(self.install_button);self.install_button.setEnabled(False);self.install_button.clicked.connect(self.install)
        self.download_page=QPushButton('打开下载页',self)
        self.download_page.clicked.connect(lambda:QDesktopServices.openUrl(QUrl('https://github.com/'+self.store.repository+'/releases/latest')))
        for b in (self.close_button,self.download_page,self.check_button,self.install_button):buttons.addWidget(b)
        self.finish_layout(root,buttons)

    def save_automatic(self,value):
        try:self.store.save(automatic=value)
        except Exception:self.status.setText('无法保存自动检查设置')
    def set_busy(self,value):
        self.busy=value
        for button in (self.check_button,):button.setEnabled(not value)
        self.install_button.setEnabled(not value and bool(self.release))
    def work(self,kind,fn):
        self.set_busy(True);self.cancel.clear()
        def run():
            try:result=fn();action=kind
            except UpdateCancelled:result='已取消更新';action='error'
            except UpdateError as exc:result=str(exc);action='error'
            except Exception:result='更新操作未完成，未安装新版本。请重试或检查目录写入权限。';action='error'
            try:self.signals.result.emit(action,result)
            except RuntimeError:pass
        threading.Thread(target=run,name='release-'+kind,daemon=True).start()
    def check(self):
        if self.busy:return
        self.release=None;self.notes.hide();self.status.setText('正在检查 GitHub 公开发布…');self.progress.hide()
        def run():
            client=ReleaseClient(self.settings,self.store.repository,'',self.cancel)
            try:return client.latest()
            finally:client.close()
        self.work('checked',run)
    def install(self):
        if self.busy or not self.release:return
        if not getattr(sys,'frozen',False):self.status.setText('开发源码模式不替换文件，请使用 dist 内的成品更新');return
        answer=QMessageBox.question(self,'确认更新','下载校验成功后将关闭本工具并安装新版。当前搜索和下载会停止，存档会保留。是否继续？',QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)
        if answer!=QMessageBox.StandardButton.Yes:return
        self.status.setText('正在下载和校验更新包…');self.progress.setRange(0,100);self.progress.setValue(0);self.progress.show()
        release=dict(self.release)
        self.work('prepared',lambda:prepare_update(self.settings,release,'',self.store.repository,cancel=self.cancel,
                  progress=lambda a,b:self.signals.progress.emit(a,b)))
    def on_progress(self,received,total):
        self.progress.setValue(int(received*100/max(1,total)))
        self.status.setText('正在下载 %.1f / %.1f MB，完成后校验…'%(received/1048576,total/1048576))
    def finished_work(self,kind,result):
        self.set_busy(False)
        if self.cancel.is_set():return
        if kind=='checked':
            self.release=result
            if hasattr(self.parent(),'update_btn'):
                self.parent().update_btn.setText('发现新版 · '+result['version'] if result else '检查更新 · '+APP_VERSION)
                self.parent().update_btn.setToolTip('发现可用更新' if result else '已完成版本检查')
            if result:
                self.status.setText('发现新版本 '+result['version']);self.notes.setPlainText(result['notes'] or '此版本没有附加说明。')
                self.notes.show();self.install_button.setEnabled(True);self.show();self.raise_()
            else:self.status.setText('当前已是最新正式版本 '+APP_VERSION)
        elif kind=='prepared':
            try:
                launch_update(result)
                self.status.setText('安装程序已启动，正在退出本工具…')
                self.parent().close()
            except Exception:self.status.setText('无法启动更新程序，现有程序未替换。请检查目录权限后重试。')
        else:
            self.status.setText(result)
            parent=self.parent()
            if parent and hasattr(parent,'update_btn'):
                parent.update_btn.setText('检查更新 · 暂不可用');parent.update_btn.setToolTip(result)
        if self.automatic and not self.isVisible():self.automatic=False
    def reject(self):
        self.cancel.set();super().reject()
