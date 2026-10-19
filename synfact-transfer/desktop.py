"""Synfact Transfer desktop application."""
import base64
import io
import json
from pathlib import Path
import sys
import threading
import multiprocessing

if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()

from app import make_server


def main():
    from PySide6.QtCore import QLocale, QTimer, QUrl, Qt
    from PySide6.QtGui import QDesktopServices, QIcon
    from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow, QMessageBox
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineUrlRequestInterceptor
    from PySide6.QtWebEngineWidgets import QWebEngineView

    QLocale.setDefault(QLocale(QLocale.Language.English))
    application = QApplication(sys.argv[:1])
    application.setApplicationName('Synfact Transfer')
    application.setApplicationVersion('0.0.2')
    application.setOrganizationName('Synfact')
    server = make_server(0)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    origin = f'http://127.0.0.1:{server.server_port}'

    class LocalRequests(QWebEngineUrlRequestInterceptor):
        def interceptRequest(self, info):
            url = info.requestUrl()
            allowed = ((url.scheme() == 'http' and url.host() == '127.0.0.1'
                        and url.port() == server.server_port) or url.scheme() in ('data', 'blob', 'about'))
            if not allowed:
                info.block(True)

    class LocalPage(QWebEnginePage):
        def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
            if (is_main_frame and url.toString() == 'https://ko-fi.com/ggsm80'
                    and navigation_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked):
                QDesktopServices.openUrl(url)
                return False
            return (url.scheme() == 'http' and url.host() == '127.0.0.1'
                    and url.port() == server.server_port and url.path() == '/')

        def createWindow(self, window_type):
            return None

        def chooseFiles(self, mode, old_files, accepted_types):
            accepted = {item.lower() for item in accepted_types}
            if accepted & {'.wav', 'audio/wav', 'audio/x-wav', 'audio/wave'}:
                extensions, title, file_filter = {'.wav'}, 'Choose WAV samples', 'WAV audio (*.wav *.WAV)'
            elif accepted & {'.stprj'}:
                extensions, title, file_filter = {'.stprj'}, 'Choose project', 'Project files (*.stprj *.STPRJ)'
            elif accepted & {'.stsnd', '.dnsnd'}:
                extensions, title, file_filter = {'.stsnd', '.dnsnd'}, 'Choose sound', 'Sound files (*.stsnd *.STSND *.dnsnd *.DNSND)'
            else:
                extensions, title, file_filter = {'.syx', '.smz4'}, 'Choose firmware or mods', 'Firmware and mods (*.syx *.SYX *.smz4 *.SMZ4)'
            options = QFileDialog.Option.DontUseNativeDialog
            if mode == QWebEnginePage.FileSelectionMode.FileSelectOpenMultiple:
                files, _ = QFileDialog.getOpenFileNames(self.parent(), title, '', file_filter, options=options)
            else:
                filename, _ = QFileDialog.getOpenFileName(self.parent(), title, '', file_filter, options=options)
                files = [filename] if filename else []
            return [filename for filename in files if Path(filename).suffix.lower() in extensions]

    class TransferView(QWebEngineView):
        def dragEnterEvent(self, event):
            if event.mimeData().hasUrls() and any(u.isLocalFile() for u in event.mimeData().urls()):
                event.acceptProposedAction()
            else:
                super().dragEnterEvent(event)

        def dragMoveEvent(self, event):
            if event.mimeData().hasUrls():
                event.acceptProposedAction()
            else:
                super().dragMoveEvent(event)

        def dropEvent(self, event):
            if not event.mimeData().hasUrls():
                return super().dropEvent(event)
            files = []
            try:
                for url in event.mimeData().urls():
                    if not url.isLocalFile():
                        continue
                    path = Path(url.toLocalFile())
                    if not path.is_file():
                        raise ValueError('Drop files, not folders.')
                    if path.stat().st_size > 64 * 1024 * 1024:
                        raise ValueError('File exceeds the 64 MB limit.')
                    files.append({'name': path.name, 'data': base64.b64encode(path.read_bytes()).decode('ascii')})
                if files:
                    point = event.position()
                    payload = json.dumps(files)
                    self.page().runJavaScript(f'window.receiveNativeFiles({payload}, {point.x()}, {point.y()})')
                event.acceptProposedAction()
            except Exception as error:
                self.page().runJavaScript('message(' + json.dumps(str(error)) + ')')

    class Window(QMainWindow):
        def __init__(self):
            super().__init__()
            self.closing = self.finished = False
            self.setWindowTitle('Synfact Transfer 0.0.2')
            self.resize(1120, 780)
            self.setMinimumSize(800, 600)
            self.setWindowIcon(QIcon(str(Path(__file__).parent / 'assets' / 'synfact.svg')))
            self.profile = QWebEngineProfile(self)
            self.interceptor = LocalRequests(self.profile)
            self.profile.setUrlRequestInterceptor(self.interceptor)
            self.profile.downloadRequested.connect(self.download)
            self.view = TransferView(self)
            self.view.setAcceptDrops(True)
            self.page = LocalPage(self.profile, self.view)
            self.view.setPage(self.page)
            self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
            self.setCentralWidget(self.view)
            self.shutdown_timer = QTimer(self)
            self.shutdown_timer.setInterval(100)
            self.shutdown_timer.timeout.connect(self.wait_for_worker)
            self.view.setUrl(QUrl(origin + '/'))

        def download(self, request):
            filename, _ = QFileDialog.getSaveFileName(self, 'Save file',
                str(Path.home() / request.downloadFileName()),
                options=QFileDialog.Option.DontUseNativeDialog)
            if not filename:
                request.cancel()
                return
            destination = Path(filename)
            request.setDownloadDirectory(str(destination.parent))
            request.setDownloadFileName(destination.name)
            request.accept()

        def closeEvent(self, event):
            if self.finished:
                event.accept()
                return
            event.ignore()
            if self.closing:
                return
            active = any(j['status'] in ('running', 'queued') for j in server.service.snapshot()['jobs'])
            if active:
                choice = QMessageBox.question(self, 'Transfer in progress',
                    'Cancel pending transfers and close after the current operation stops?',
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No)
                if choice != QMessageBox.StandardButton.Yes:
                    return
            self.closing = True
            self.view.setEnabled(False)
            self.setWindowTitle('Synfact Transfer · Closing…')
            server.service.begin_stop()
            self.shutdown_timer.start()

        def wait_for_worker(self):
            if server.service.worker.is_alive():
                return
            self.shutdown_timer.stop()
            self.finished = True
            self.close()

    window = Window()
    window.show()
    try:
        return application.exec()
    finally:
        server.shutdown()
        serving.join(timeout=5)
        server.service.stop()
        server.server_close()
        window.view.setPage(QWebEnginePage(window.view))
        window.page.deleteLater()
        application.processEvents()


if __name__ == '__main__':
    multiprocessing.freeze_support()
    raise SystemExit(main())
