"""小說電台（獨立視窗版）

開一個自己的程式視窗，不用開瀏覽器。背後一樣是 novel_radio.py 的本機伺服器，
只是改成在程式裡面跑，關掉視窗就整個結束。

執行：pythonw app.py   （用 pythonw 就不會多一個黑色視窗）
"""

import threading
import traceback

import novel_radio as nr

APP_PORT = 8777  # 跟瀏覽器版（8765）分開，兩個同時開也不會打架
LOG = nr.HERE / "app-error.log"


def fail(msg: str):
    """pythonw 沒有黑色視窗可以印錯誤，所以寫進檔案並跳出提示框。"""
    LOG.write_text(msg, encoding="utf-8")
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, f"小說電台無法啟動。\n\n詳細原因寫在：\n{LOG}", "小說電台", 0x10)
    except Exception:
        pass


def main():
    try:
        import webview
    except ImportError:
        fail("沒有安裝 pywebview。請雙擊「啟動小說電台.bat」，它會自動安裝。")
        return

    try:
        server = nr.Server((nr.HOST, APP_PORT), nr.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    except OSError:
        server = None  # 已經有一個小說電台視窗開著，新視窗直接共用它的伺服器

    try:
        # 讓工作列把它當成獨立程式，顯示貓咪圖示，而不是 Python 的圖示
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("NovelRadio.App")
    except Exception:
        pass

    webview.create_window(
        "小說電台", f"http://{nr.HOST}:{APP_PORT}/",
        width=1280, height=860, min_size=(420, 640), background_color="#0B0B0D",
    )
    # private_mode=False 才會記住書架、進度和設定
    webview.start(private_mode=False, storage_path=str(nr.HERE / ".app-data"),
                  icon=str(nr.icon_path("ico")))
    if server:
        server.shutdown()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        fail(traceback.format_exc())
