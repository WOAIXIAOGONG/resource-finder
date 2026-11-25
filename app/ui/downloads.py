"""Download choices and queue, sharing the application's dialog design."""
import os
from pathlib import Path
from PySide6.QtCore import Qt,QUrl,QTimer
from PySide6.QtGui import QDesktopServices,QFontMetrics
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,QPushButton,QLineEdit,
    QFileDialog,QCheckBox,QFrame,QProgressBar,QMessageBox,QPlainTextEdit)
from .controls import WheelSafeComboBox as QComboBox
from .dialog_design import StyledDialog,primary
from ..downloads import platform
from .widgets import Spinner
from .icons import icon
from . import theme

class DownloadTaskCard(QFrame):
    def __init__(self,center,job):
        super().__init__(center);self.center=center;self.identity=job['id'];self.setObjectName('DownloadTask')
        layout=QVBoxLayout(self);layout.setContentsMargins(18,16,18,16);layout.setSpacing(10)
        top=QHBoxLayout();layout.addLayout(top)
        self.symbol=QLabel(self);self.symbol.setObjectName('DownloadSymbol');self.symbol.setFixedSize(34,34)
        self.symbol.setAlignment(Qt.AlignmentFlag.AlignCenter);top.addWidget(self.symbol)
        self.title=QLabel(self);self.title.setObjectName('DownloadTitle');self.title.setMinimumWidth(0)
        self.title.setTextFormat(Qt.TextFormat.PlainText);top.addWidget(self.title,1)
        self.badge=QLabel(self);self.badge.setObjectName('DownloadState');top.addWidget(self.badge)
        self.meta=QLabel(self);self.meta.setObjectName('DialogHint');self.meta.setTextFormat(Qt.TextFormat.PlainText)
        self.meta.setWordWrap(True);layout.addWidget(self.meta)
        self.progress=QProgressBar(self);self.progress.setTextVisible(False);self.progress.setFixedHeight(5);layout.addWidget(self.progress)
        bottom=QHBoxLayout();layout.addLayout(bottom)
        self.detail=QLabel(self);self.detail.setObjectName('DialogHint');bottom.addWidget(self.detail,1)
        self.action=QPushButton(self);self.action.clicked.connect(self.activate);bottom.addWidget(self.action)
        self.reason=QPushButton('查看原因',self);self.reason.setObjectName('Flat');self.reason.clicked.connect(self.show_error);bottom.addWidget(self.reason)
        self.folder=QPushButton('打开文件夹',self);self.folder.setObjectName('Flat')
        self.folder.clicked.connect(self.open_folder);bottom.addWidget(self.folder)
        self.clear=QPushButton('清除记录',self);self.clear.setObjectName('Flat')
        self.clear.setToolTip('只清除此条记录，保留已下载文件');self.clear.clicked.connect(lambda:self.center.clear_history(self.identity));bottom.addWidget(self.clear)
        self.update_job(job)
    def activate(self):
        state=self.job['state']
        if state in ('running','queued'):self.center.manager.cancel(self.identity)
        elif state=='login_required':self.center.recover(self.identity)
        elif state in ('failed','cancelled'):self.center.retry(self.identity)
    def show_error(self):QMessageBox.information(self,'下载详情',self.job.get('error',''))
    def open_folder(self):QDesktopServices.openUrl(QUrl.fromLocalFile(self.job['folder']))
    def resizeEvent(self,event):
        super().resizeEvent(event);self.update_title()
    def update_title(self):
        if hasattr(self,'job'):self.title.setText(QFontMetrics(self.title.font()).elidedText(self.job['row']['title'],Qt.TextElideMode.ElideRight,max(60,self.title.width())))
    def update_job(self,job):
        self.job=job;state=job['state'];self.title.setToolTip(job['row']['title']);self.update_title()
        colors=theme.palette(getattr(getattr(self.center.parent(),'settings',None),'theme','dark'))
        self.symbol.setPixmap(icon('download',colors['accent'],20).pixmap(20,20))
        labels={'queued':'等待中','running':'下载中','cancelling':'正在取消','done':'已完成','failed':'下载失败','cancelled':'已取消','login_required':'需要登录'}
        self.badge.setText(labels.get(state,state));tone='success' if state=='done' else 'error' if state in ('failed','login_required') else 'active' if state in ('running','queued') else 'muted'
        if self.badge.property('tone')!=tone:
            self.badge.setProperty('tone',tone);self.badge.style().unpolish(self.badge);self.badge.style().polish(self.badge)
        from urllib.parse import urlsplit
        opts=job.get('options',{});kind=opts.get('mode','');parts=[job['row'].get('source') or urlsplit(job['row']['url']).hostname or '资源']
        parts.append({'full':'完整视频','silent':'无声视频','audio':'音频','sound':'音效','image':'原图','text':'文章正文','ugoira':'动图','web_video':'网页视频','web_image':'网页图片','web_sound':'网页音频'}.get(kind,'资源'))
        if opts.get('height'):parts.append(str(opts['height'])+'p')
        fmt=opts.get('audio_format') if kind in ('audio','sound','web_sound') else opts.get('text_format','txt') if kind=='text' else opts.get('container') if kind in ('full','silent','web_video') else ''
        extraction=job.get('extraction_label') or opts.get('extraction_label')
        if extraction:parts.append(extraction)
        if fmt:parts.append(str(fmt).upper())
        self.meta.setText(' · '.join(parts));self.meta.setToolTip(job['row']['url'])
        active=state in ('running','cancelling');percent=job.get('percent',0)
        self.progress.setVisible(active);self.progress.setRange(0,0 if active and percent<0 else 100)
        if percent>=0:self.progress.setValue(min(100,int(percent)))
        detail=job.get('phase','')
        if state=='running' and percent>=0:detail+=' · %d%%'%percent
        if state=='running' and job.get('speed'):detail+=' · %.1f MB/s'%(job['speed']/1048576)
        if state=='done':detail='%d 个文件已保存'%len(job.get('files',[]))
        elif state in ('failed','cancelled','login_required','cancelling','queued'):detail=labels[state]
        self.detail.setText(detail[:100]);self.detail.setToolTip(detail)
        self.action.setVisible(state!='done');self.action.setEnabled(state!='cancelling')
        self.action.setText('取消' if state in ('running','queued','cancelling') else '处理登录' if state=='login_required' else '重新下载')
        self.reason.setVisible(bool(job.get('error')));self.folder.setToolTip(job['folder'])
        self.clear.setVisible(state in ('done','failed','cancelled','login_required'))
        self.clear.setEnabled(self.identity in self.center.manager.clearable_ids())

def open_login_helper(parent):
    from ..config import app_dir
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(app_dir())/'download-login-helper')))
    QMessageBox.information(parent,'登录助手','浏览器登录状态无法自动读取时，可按文件夹内的使用说明导出当前平台登录文件，再点击“导入登录文件”。\n\n文件仅在本机使用，请勿分享。')

class LoginRecovery(QWidget):
    """Shown only after an explicit login gate and failed automatic fallback."""
    def __init__(self,manager,row,retry,parent=None):
        super().__init__(parent);self.manager=manager;self.row=row;self.retry=retry
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0)
        video=platform(row) in ('youtube','bilibili')
        message=('平台需要有效登录。请在浏览器登录后继续；工具会自动尝试读取可用状态。' if video else
                 '平台需要有效登录。当前来源需在登录后导入该平台登录文件，工具会自动重试。')
        note=QLabel(message,self)
        note.setWordWrap(True);layout.addWidget(note)
        bar=QHBoxLayout();layout.addLayout(bar)
        self.resume=QPushButton('已登录，重试' if video else '重新尝试',self);self.resume.clicked.connect(lambda:retry());bar.addWidget(self.resume)
        pick=QPushButton('导入登录文件',self);pick.clicked.connect(self.import_file);bar.addWidget(pick)
        helper=QPushButton('登录助手',self);helper.clicked.connect(lambda:open_login_helper(self));bar.addWidget(helper)
        from ..download_access import access_key
        helper.setVisible(access_key(row) in ('youtube.com','bilibili.com','pixiv.net'))
    def import_file(self):
        path,_=QFileDialog.getOpenFileName(self,'导入当前平台登录文件','','Cookie 文件 (*.txt);;所有文件 (*)')
        if path:
            try:self.manager.remember_access(self.row,{'file':path})
            except Exception as e:QMessageBox.warning(self,'未保存登录文件',str(e));return
            self.retry()

class DownloadDialog(StyledDialog):
    def __init__(self,manager,row,parent=None):
        super().__init__(parent);self.manager=manager;self.row=row;self.probe='';self.info=None;self.auth_snapshot={}
        self._is_aigei_sound=platform(row)=='aigei-web' and row.resource_type=='sound'
        self._browser_ready=manager.aigei_browser.active;self._auto_starting=False
        self._extract_error=False;self._attempted_auto=self._browser_ready
        self.setWindowTitle('下载资源');root=self.make_root('下载资源',row.title,'download',640)
        status_row=QHBoxLayout();self.spinner=Spinner(self,size=22);status_row.addWidget(self.spinner)
        self.status=QLabel('先解析当前资源，获取实际可用的画质与文件。',self);self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText);status_row.addWidget(self.status,1);root.addLayout(status_row)
        self.error_detail='';self.details=QPushButton('查看失败详情',self);self.details.hide()
        self.details.clicked.connect(lambda:QMessageBox.information(self,'解析详情',self.error_detail));root.addWidget(self.details)
        self.recovery=LoginRecovery(manager,row,self.analyze,self);root.addWidget(self.recovery);self.recovery.hide()
        self.auto_extract=QPushButton('一键打开并提取',self);primary(self.auto_extract)
        self.auto_extract.setToolTip('使用独立Edge/Chrome窗口自动打开网页、点击播放并提取，无需安装扩展')
        self.auto_extract.clicked.connect(self.analyze_automatically)
        root.addWidget(self.auto_extract);self.auto_extract.setVisible(self._is_aigei_sound and not self._browser_ready)
        form=QFormLayout();root.addLayout(form)
        self.parse=QPushButton('重新解析',self);self.parse.clicked.connect(self.analyze);status_row.addWidget(self.parse)
        self.mode=QComboBox(self);form.addRow('下载内容',self.mode)
        self.quality=QComboBox(self);form.addRow('清晰度 / 范围',self.quality)
        self.format=QComboBox(self);form.addRow('保存格式',self.format)
        self.subtitles=QCheckBox('同时下载可用中文字幕和英文字幕（含自动字幕）',self);form.addRow('',self.subtitles)
        self.subtitles.setVisible(row.resource_type=='video')
        self.directory=QLineEdit(manager.preferences.get('directory',''),self)
        choose=QPushButton('选择目录',self);choose.clicked.connect(self.pick_directory)
        dest=QHBoxLayout();dest.addWidget(self.directory,1);dest.addWidget(choose);form.addRow('保存到',dest)
        note=QLabel('默认直接下载，必要时自动处理登录。加入队列后可继续搜索。',self);note.setWordWrap(True);root.addWidget(note)
        footer=QHBoxLayout();footer.addStretch();close=QPushButton('关闭',self);close.clicked.connect(self.reject)
        self.start=QPushButton('加入下载队列',self);primary(self.start);self.start.setEnabled(False);self.start.clicked.connect(self.submit)
        footer.addWidget(close);footer.addWidget(self.start);self.finish_layout(root,footer)
        self.mode.currentIndexChanged.connect(self.update_formats);manager.resolved.connect(self.resolved)
        manager.inspecting.connect(self.inspecting);self.finished.connect(self.cleanup)
        self.browser_timer=QTimer(self);self.browser_timer.setInterval(2000)
        self.browser_timer.timeout.connect(manager.check_browser_state)
        if self._is_aigei_sound:
            manager.browser_state.connect(self.browser_state_changed);self.browser_timer.start();manager.check_browser_state()
        self.analyze()
    def update_auto_button(self):
        independent_info=bool(self.info and not any(v.get('automatic_browser') for v in self.info.get('web_variants',[])))
        self.auto_extract.setVisible(self._is_aigei_sound and not self._auto_starting and (self._extract_error or (not self._browser_ready and not independent_info)))
        self.auto_extract.setText('一键重试' if self._extract_error and self._attempted_auto else '一键打开并提取')
    def browser_state_changed(self,ready):
        # Queued signals may arrive after a newer extraction has connected.
        ready=self.manager.aigei_browser.active
        if ready==self._browser_ready:return
        self._browser_ready=ready
        if not ready and not self._auto_starting and self.info and any(v.get('automatic_browser') for v in self.info.get('web_variants',[])):
            self.invalidate();self.status.setText('提取页面已关闭，请点击“一键打开并提取”重新连接。')
            self._extract_error=False
        self.update_auto_button()
    def set_busy(self,busy):
        self.spinner.setVisible(busy)
        self.spinner.start() if busy else self.spinner.stop()
        self.parse.setEnabled(not busy)
        self.parse.setVisible(not busy and not self._is_aigei_sound)
        self.auto_extract.setEnabled(not busy)
    def invalidate(self,*_):
        self.manager.cancel_probe(self.probe);self.probe='';self.info=None;self.start.setEnabled(False);self.set_busy(False)
        for widget in (self.mode,self.quality,self.format,self.subtitles):widget.setEnabled(False)
    def pick_directory(self):
        path=QFileDialog.getExistingDirectory(self,'选择下载目录',self.directory.text())
        if path:self.directory.setText(path)
    def analyze(self):
        self._auto_starting=False
        self._extract_error=False
        self.invalidate();self.recovery.hide();self.details.hide();self.status.setText('正在解析资源与可用格式…');self.set_busy(True)
        self.probe=self.manager.analyze(self.row)
        # The user may start one-click extraction immediately, cancelling a slow
        # anonymous HTML probe instead of waiting for that probe to fail first.
        self.auto_extract.setEnabled(platform(self.row)=='aigei-web' and self.row.resource_type=='sound')
        self.update_auto_button()
    def analyze_automatically(self):
        self.invalidate();self.recovery.hide();self.details.hide()
        self.status.setText('正在准备独立浏览器，自动打开并提取资源…');self.set_busy(True)
        self._auto_starting=True;self._attempted_auto=True;self._extract_error=False;self.status.setToolTip('');self.update_auto_button()
        self.probe=self.manager.analyze(self.row,auto_browser=True)
    def inspecting(self,identity,message):
        if identity==self.probe:self.status.setText(message)
    def resolved(self,identity,info,error):
        if identity!=self.probe:return
        self.probe='';self.set_busy(False);self._auto_starting=False
        self._browser_ready=self.manager.aigei_browser.active
        if error:
            self.info=None;self.start.setEnabled(False)
            self.error_detail=error;self.details.setVisible(not self._is_aigei_sound)
            if self._is_aigei_sound:
                self._extract_error=True;self.status.setToolTip(error)
                self.status.setText(('未能完成提取：'+error.split('\n')[0][:180]) if self._attempted_auto else '点击下方按钮，自动打开资源网页并识别音频。')
            else:self.status.setText('解析失败：'+error.split('\n')[0][:180])
            self.recovery.setVisible(bool(info.get('needs_login')) and not self._is_aigei_sound);self.update_auto_button();return
        self._extract_error=False;self.status.setToolTip('')
        self.info=info;self.status.setText(info.get('note','')+' 共 %d 个资源。'%info.get('count',1))
        for widget in (self.mode,self.quality,self.format):widget.setEnabled(True)
        self.mode.blockSignals(True);self.mode.clear()
        if info.get('web_variants'):
            mode={'video':'web_video','image':'web_image','sound':'web_sound'}[self.row.resource_type]
            self.mode.addItem({'web_video':'网页视频提取','web_image':'网页图片提取','web_sound':'网页音频提取'}[mode],mode)
        elif info['kind']=='video':
            if info['has_video']:
                self.mode.addItem('完整视频（画面 + 可用音轨）','full');self.mode.addItem('仅视频画面（无音轨）','silent')
            if info['has_audio']:self.mode.addItem('仅音频','audio')
        elif info['kind']=='sound':self.mode.addItem('音效（原文件 / 格式转换）','sound')
        elif info['kind']=='text':self.mode.addItem('文章正文','text')
        elif info['kind']=='ugoira':self.mode.addItem('动图','ugoira')
        else:self.mode.addItem('原始图片','image')
        self.mode.blockSignals(False);self.update_formats();self.start.setEnabled(True)
        self.update_auto_button()
    def update_formats(self,*_):
        if not self.info:return
        kind=self.mode.currentData();self.quality.clear();self.format.clear()
        self.subtitles.setEnabled(kind in ('full','silent','audio'))
        self.subtitles.setVisible(kind in ('full','silent','audio'))
        if self.info.get('web_variants'):
            for variant in self.info['web_variants']:self.quality.addItem(variant['label'],variant['key'])
            if kind=='web_video':
                self.format.addItem('MP4','mp4');self.format.addItem('MKV','mkv')
            elif kind=='web_sound':
                best='保留播放格式（不重编码）' if any(v.get('browser_playback') for v in self.info['web_variants']) else '原始音频格式'
                for name,value in [(best,'best'),('MP3','mp3'),('M4A','m4a'),('WAV','wav'),('FLAC','flac')]:self.format.addItem(name,value)
            else:self.format.addItem('保留网页图片格式','original')
        elif kind in ('full','silent'):
            self.quality.addItem('当前可用最高画质',0)
            for height in self.info['heights']:self.quality.addItem(str(height)+'p（不超过此分辨率）',height)
            for name,value in [('MP4','mp4'),('MKV（保留更多编码兼容性）','mkv')]:self.format.addItem(name,value)
        elif kind in ('audio','sound'):
            self.quality.addItem('当前可用最佳音轨',0)
            for name,value in [('原始音频格式（不重编码）','best'),('MP3','mp3'),('M4A','m4a'),('WAV','wav'),('FLAC','flac')]:self.format.addItem(name,value)
        elif kind=='image':
            self.quality.addItem('全部图片（%d 张）'%self.info['count'],'all')
            if self.info['count']>1:self.quality.addItem('仅第一张','first')
            self.format.addItem('保持原始格式','original')
        elif kind=='ugoira':
            self.quality.addItem('当前作品','all')
            for title,key in [('MP4 动画','mp4'),('GIF 动图','gif'),('原始帧ZIP + 帧时长JSON','zip')]:self.format.addItem(title,key)
        else:
            self.quality.addItem('当前文章 / 当前章节','all');self.format.addItem('TXT（纯文本）','txt');self.format.addItem('HTML（离线阅读）','html')
    def submit(self):
        if not self.info:return
        mode=self.mode.currentData()
        options={'mode':mode,'kind':self.info['kind'],'height':self.quality.currentData() if mode in ('full','silent') else 0,
                 'container':self.format.currentData() if mode in ('full','silent') else 'mp4',
                 'audio_format':self.format.currentData() if mode in ('audio','sound') else 'best','pages':self.quality.currentData(),
                 'subtitles':self.subtitles.isEnabled() and self.subtitles.isChecked(),'require_audio':self.info.get('has_audio',False),
                 'ugoira_format':self.format.currentData() if mode=='ugoira' else 'zip'}
        if mode=='text':options['text_format']=self.format.currentData()
        if self.info.get('web_variants'):
            options.update(aigei_variant=self.quality.currentData(),extraction_label=self.quality.currentText())
            selected=next((v for v in self.info['web_variants'] if v['key']==self.quality.currentData()),{})
            if selected.get('automatic_browser'):options['aigei_automatic']=True
            if mode=='web_video':options['container']=self.format.currentData()
            if mode=='web_sound':options['audio_format']=self.format.currentData()
        try:
            if not self.directory.text().strip():raise ValueError('请选择保存目录')
            self.manager.add(self.row,{},options,self.directory.text().strip())
        except Exception as e:self.status.setText(str(e));return
        self.accept()
    def cleanup(self,*_):
        self.browser_timer.stop()
        if self._is_aigei_sound:
            try:self.manager.browser_state.disconnect(self.browser_state_changed)
            except RuntimeError:pass
        self.manager.cancel_probe(self.probe);self.probe='';self.set_busy(False)
        try:self.manager.resolved.disconnect(self.resolved)
        except RuntimeError:pass
        try:self.manager.inspecting.disconnect(self.inspecting)
        except RuntimeError:pass

class DownloadCenter(StyledDialog):
    def __init__(self,manager,parent=None):
        super().__init__(parent);self.manager=manager;self.setWindowTitle('下载中心')
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowMinimizeButtonHint | Qt.WindowType.WindowCloseButtonHint)
        self._finished=False
        root=self.make_root('下载中心','管理正在下载的任务与已保存的资源','download',880)
        stats=QHBoxLayout();root.addLayout(stats);self.stats={}
        for key,title in [('active','进行中'),('done','已完成'),('attention','待处理')]:
            box=QFrame(self);box.setObjectName('DownloadStat');layout=QHBoxLayout(box)
            value=QLabel('0',box);value.setObjectName('DownloadCount');layout.addWidget(value)
            label=QLabel(title,box);label.setObjectName('DialogHint');layout.addWidget(label);stats.addWidget(box,1);self.stats[key]=value
        bar=QHBoxLayout();root.addLayout(bar);hint=QLabel('下载任务',self);hint.setObjectName('DialogStepTitle');bar.addWidget(hint,1)
        self.filter=QComboBox(self)
        for title,key in [('全部任务','all'),('进行中','active'),('已完成','done'),('待处理','attention')]:self.filter.addItem(title,key)
        bar.addWidget(self.filter);self.filter.currentIndexChanged.connect(self.refresh)
        self.clear_all=QPushButton('清除全部记录',self);self.clear_all.setObjectName('Flat')
        self.clear_all.setToolTip('清除所有已结束的记录，保留下载文件和正在执行的任务')
        self.clear_all.clicked.connect(lambda:self.clear_history());bar.addWidget(self.clear_all)
        self.cards={};self.tasks=QVBoxLayout();self.tasks.setSpacing(12);root.addLayout(self.tasks)
        self.empty=QLabel('还没有下载任务\n在资源封面上点击右键，选择“下载资源”即可开始。',self)
        self.empty.setObjectName('DownloadEmpty');self.empty.setWordWrap(True);self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter);root.addWidget(self.empty)
        note=QLabel('最多同时下载 2 项 · 最小化或关闭此窗口会继续下载\n清除只移除已结束记录，保留文件；退出工具或切换账号才停止下载',self);note.setObjectName('DialogHint');note.setWordWrap(True);root.addWidget(note)
        self.finish_layout(root);self.resize(880,720);manager.changed.connect(self.refresh);self.finished.connect(self.cleanup);self.refresh()
    def cleanup(self,*_):
        self._finished=True
        try:self.manager.changed.disconnect(self.refresh)
        except RuntimeError:pass
    def refresh(self):
        jobs=list(reversed(self.manager.snapshot()))
        existing={j['id'] for j in jobs}
        for identity in list(self.cards):
            if identity not in existing:
                card=self.cards.pop(identity);self.tasks.removeWidget(card);card.hide();card.deleteLater()
        self.clear_all.setEnabled(bool(self.manager.clearable_ids()))
        def group(j):return 'active' if j['state'] in ('running','queued','cancelling') else 'done' if j['state']=='done' else 'attention'
        for key,label in self.stats.items():label.setText(str(sum(group(j)==key for j in jobs)))
        selected=self.filter.currentData();visible=0
        for i,j in enumerate(jobs):
            card=self.cards.get(j['id'])
            if card is None:
                card=self.cards[j['id']]=DownloadTaskCard(self,j);self.tasks.insertWidget(i,card)
            else:card.update_job(j)
            show=selected=='all' or selected==group(j);card.setVisible(show);visible+=int(show)
        self.empty.setVisible(not visible)
        self.empty.setText('当前分类没有任务' if jobs else '还没有下载任务\n在资源封面上点击右键，选择“下载资源”即可开始。')
    def clear_history(self,identity=None):
        try:self.manager.clear_history(identity)
        except Exception as error:QMessageBox.warning(self,'记录未清除','保存记录失败，请重试：'+str(error))
    def retry(self,identity):
        self.manager.retry(identity)
    def recover(self,identity):
        from ..models import SearchResult
        job=self.manager.jobs[identity];row=SearchResult(**{k:v for k,v in job['row'].items() if k in SearchResult.__dataclass_fields__})
        dialog=StyledDialog(self);dialog.setWindowTitle('继续下载')
        root=dialog.make_root('继续下载',row.title,'download',600)
        def retry():self.retry(identity);dialog.accept()
        root.addWidget(LoginRecovery(self.manager,row,retry,dialog))
        footer=QHBoxLayout();footer.addStretch();close=QPushButton('稍后处理',dialog);close.clicked.connect(dialog.reject);footer.addWidget(close)
        dialog.finish_layout(root,footer);dialog.exec()
