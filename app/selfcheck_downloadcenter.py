"""Isolated frozen UI check; caller creates a temporary profile before invoking."""
from pathlib import Path
from PySide6.QtCore import Qt
from .models import SearchResult

def run(app,win,log):
    manager=win.downloads;schedule=manager._schedule;manager._schedule=lambda:None;ids=[]
    try:
        for i,state in enumerate(['done','failed','queued']):
            key=manager.add(SearchResult(title='下载中心验收 '+str(i),url='https://www.youtube.com/watch?v=fixture'+str(i)),{},dict(mode='full',height=720,container='mp4'),str(manager.root/'fixture-files'))
            manager.jobs[key]['state']=state;ids.append(key)
        saved=Path(manager.jobs[ids[0]]['folder'])/'keep.txt';saved.write_text('preserved',encoding='utf-8')
        win._show_download_center();popup=win._download_center;app.processEvents()
        assert popup.windowFlags() & Qt.WindowType.WindowMinimizeButtonHint
        from PySide6.QtWidgets import QFrame,QPushButton
        assert popup.findChild(QFrame,'DialogFooter') is None
        assert not any(b.text() in ('最小化','隐藏到后台') for b in popup.findChildren(QPushButton))
        popup.showMinimized();app.processEvents();assert popup.isMinimized() and not win.isMinimized()
        win._show_download_center();app.processEvents();assert win._download_center is popup and not popup.isMinimized()
        popup.hide();app.processEvents();assert not popup.isVisible() and not popup._finished
        manager.jobs[ids[2]].update(state='running',percent=37);manager.emit_changed();app.processEvents()
        win._show_download_center();app.processEvents();assert win._download_center is popup
        assert popup.cards[ids[2]].progress.value()==37
        popup.cards[ids[0]].clear.click();assert ids[0] not in manager.jobs and ids[0] not in popup.cards
        popup.clear_all.click();assert set(manager.jobs)=={ids[2]} and not popup.clear_all.isEnabled()
        assert saved.read_text(encoding='utf-8')=='preserved'
        popup.close();app.processEvents();assert popup._finished
        win._show_download_center();app.processEvents();assert win._download_center is not popup
        manager.jobs[ids[2]]['state']='cancelled';manager.emit_changed();app.processEvents()
        win._download_center.clear_all.click();assert not manager.jobs and not win._download_center.cards
        win._download_center.close()
        log('download center ok: native minimize, independent parent, hide/restore, progress, single/all history clearing, files and active task preserved')
    finally:manager._schedule=schedule
