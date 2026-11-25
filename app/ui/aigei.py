"""Explicit official OAuth connection; no automatic browser launch."""
import threading
from PySide6.QtCore import Signal,Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QLineEdit,QWidget
from .dialog_design import StyledDialog,primary
from .widgets import Spinner
from ..aigei_mcp import OAuthFlow

class AigeiDialog(StyledDialog):
    ready=Signal(str)
    completed=Signal(str)
    def __init__(self,client,parent=None):
        super().__init__(parent);self.client=client;self.cancel=threading.Event();self.busy=False
        self.setWindowTitle('连接爱给 MCP')
        root=self.make_root('爱给官方 MCP','仅用于查询素材信息，下载由独立网页解析器处理','settings',640)
        text=QLabel('使用你自己的浏览器登录爱给并同意授权。工具不读取浏览器密码或 Cookie。授权只保存在当前存档，换 Windows 用户或设备后需重新连接。搜索/详情使用官方调用额度。此连接仅开放信息查询，不调用下载或预览下载工具。',self)
        text.setWordWrap(True);root.addWidget(text)
        row=QHBoxLayout();self.spinner=Spinner(self,size=22);self.spinner.hide();row.addWidget(self.spinner)
        self.status=QLabel('已连接爱给账号' if client and client.connected() else '尚未连接',self)
        self.status.setWordWrap(True);self.status.setTextFormat(Qt.TextFormat.PlainText);row.addWidget(self.status,1);root.addLayout(row)
        self.link=QLineEdit(self);self.link.setReadOnly(True);self.link.setPlaceholderText('点击“开始连接”后，在常用浏览器打开授权链接');root.addWidget(self.link)
        actions=QHBoxLayout();root.addLayout(actions)
        self.copy=QPushButton('复制授权链接',self);self.copy.setEnabled(False);self.copy.clicked.connect(lambda:QGuiApplication.clipboard().setText(self.link.text()));actions.addWidget(self.copy)
        self.connect_button=QPushButton('开始连接',self);primary(self.connect_button);self.connect_button.setEnabled(client is not None);self.connect_button.clicked.connect(self.start);actions.addWidget(self.connect_button)
        self.disconnect_button=QPushButton('断开当前存档授权',self);self.disconnect_button.setEnabled(bool(client and client.connected()));self.disconnect_button.clicked.connect(self.disconnect_account);actions.addWidget(self.disconnect_button)
        footer=QHBoxLayout();footer.addStretch();close=QPushButton('关闭',self);close.clicked.connect(self.reject);footer.addWidget(close);self.finish_layout(root,footer)
        self.resize(680,500)
        self.ready.connect(self.on_ready);self.completed.connect(self.on_completed);self.finished.connect(lambda *_:self.cancel.set())
    def start(self):
        if self.busy:return
        self.cancel=threading.Event();self.busy=True;self.connect_button.setEnabled(False);self.disconnect_button.setEnabled(False)
        self.link.clear();self.copy.setEnabled(False);self.spinner.show();self.spinner.start();self.status.setText('正在准备官方授权…')
        def work():
            flow=None
            try:
                flow=OAuthFlow(self.client,self.cancel);url=flow.prepare();self.ready.emit(url);flow.finish();error=''
            except Exception as exc:error=str(exc)
            finally:
                if flow:flow.close()
            try:self.completed.emit(error)
            except RuntimeError:pass
        threading.Thread(target=work,daemon=True,name='aigei-oauth').start()
    def on_ready(self,url):
        if self.cancel.is_set():return
        self.link.setText(url);self.copy.setEnabled(True);self.status.setText('请复制链接，在你常用的浏览器打开并同意授权。完成后这里会自动更新，等待上限3分钟。')
    def on_completed(self,error):
        if self.cancel.is_set():return
        self.busy=False;self.spinner.stop();self.spinner.hide();self.connect_button.setEnabled(True)
        self.copy.setEnabled(False);self.link.clear();self.disconnect_button.setEnabled(self.client.connected())
        self.status.setText(error or '授权已保存，后续搜索可通过 MCP 查询素材信息。网页提取下载不依赖此授权。')
    def disconnect_account(self):
        try:self.client.disconnect();self.status.setText('已移除此存档的本机授权。需要时可重新连接。');self.disconnect_button.setEnabled(False)
        except Exception:self.status.setText('本机授权未移除，请检查存档目录是否可写')

