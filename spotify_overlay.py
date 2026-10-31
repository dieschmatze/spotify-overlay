import asyncio
import ctypes
from ctypes import wintypes
from io import BytesIO
import threading
import time
import tkinter as tk
from PIL import Image, ImageOps, ImageTk

from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)
from winrt.windows.storage.streams import DataReader

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.SetWindowPos.argtypes = (
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, wintypes.UINT,
)
_user32.SetWindowPos.restype = wintypes.BOOL


class SpotifyOverlay:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Spotify Overlay")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.92)
        self.root.configure(bg="#171820")
        self.root.geometry("540x226")
        self.root.resizable(False, False)

        self.card = tk.Frame(self.root, bg="#171820", highlightthickness=1,
                             highlightbackground="#454750")
        self.card.pack(fill="both", expand=True, padx=3, pady=3)
        self.card.columnconfigure(1, weight=1)

        self.cover_frame = tk.Frame(self.card, width=140, height=140, bg="#1DB954")
        self.cover_frame.grid(row=0, column=0, rowspan=3, padx=(12, 10), pady=9)
        self.cover_frame.grid_propagate(False)
        self.cover = tk.Label(self.cover_frame, text="♫", font=("Segoe UI", 48, "bold"),
                              fg="white", bg="#1DB954")
        self.cover.pack(fill="both", expand=True)

        self.header = tk.Label(self.card, text="JETZT BEI SPOTIFY", font=("Segoe UI", 9, "bold"),
                               fg="#1DB954", bg="#171820", anchor="w")
        self.header.grid(row=0, column=1, sticky="ew", pady=(11, 0))
        self.spotify_logo = tk.Canvas(self.card, width=40, height=40, bg="#171820",
                                      highlightthickness=0, bd=0)
        self.spotify_logo.grid(row=0, column=2, sticky="e", padx=(0, 2), pady=(7, 0))
        self.spotify_logo.create_oval(2, 2, 38, 38, fill="#1DB954", outline="")
        for points in (
            (8, 14, 13, 12, 21, 13, 31, 17),
            (9, 20, 15, 18, 23, 19, 30, 22),
            (11, 26, 17, 25, 23, 26, 28, 29),
        ):
            self.spotify_logo.create_line(*points, smooth=True, splinesteps=12,
                                          fill="#171820", width=2.5, capstyle=tk.ROUND)
        self.title = tk.Label(self.card, text="Spotify wird gesucht …", font=("Segoe UI", 12, "bold"),
                              fg="white", bg="#171820", anchor="w")
        self.title.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(3, 0))
        self.artist = tk.Label(self.card, text="", font=("Segoe UI", 10),
                               fg="#B9BBC3", bg="#171820", anchor="w")
        self.artist.grid(row=2, column=1, columnspan=2, sticky="ew", pady=(1, 5))

        close = tk.Button(self.card, text="×", font=("Segoe UI", 15), bd=0,
                          fg="#B9BBC3", bg="#171820", activebackground="#30313a",
                          activeforeground="white", cursor="hand2", command=self.root.destroy)
        close.grid(row=0, column=3, sticky="ne", padx=(0, 7), pady=(3, 0))

        self.progress = tk.Canvas(self.card, height=4, bg="#3A3B43", highlightthickness=0)
        self.progress.grid(row=3, column=0, columnspan=4, sticky="ew", padx=12, pady=(3, 0))
        self.progress_fill = self.progress.create_rectangle(0, 0, 0, 4, fill="#1DB954", width=0)

        self.elapsed = tk.Label(self.card, text="0:00", font=("Segoe UI", 8),
                                fg="#B9BBC3", bg="#171820", anchor="w")
        self.elapsed.grid(row=4, column=0, columnspan=3, sticky="w", padx=12, pady=(3, 7))
        self.duration = tk.Label(self.card, text="0:00", font=("Segoe UI", 8),
                                 fg="#B9BBC3", bg="#171820", anchor="e")
        self.duration.grid(row=4, column=3, sticky="e", padx=(0, 12), pady=(3, 7))

        self._track_key = None
        self._cover_photo = None
        self._fetching = False
        self._position = 0.0
        self._duration = 0.0
        self._position_time = time.monotonic()
        self._playing = False

        for widget in (self.card, self.cover_frame, self.cover, self.header,
                       self.spotify_logo, self.title, self.artist):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag)

        self.root.update_idletasks()
        self._hwnd = int(self.root.wm_frame(), 0)
        screen_width = self.root.winfo_screenwidth()
        self.root.geometry(f"540x226+{screen_width - 558}+18")
        self._keep_on_top()
        self._refresh()
        self._tick_progress()

    def _drag_start(self, event):
        self._drag_x = event.x_root - self.root.winfo_x()
        self._drag_y = event.y_root - self.root.winfo_y()

    def _drag(self, event):
        self.root.geometry(f"+{event.x_root - self._drag_x}+{event.y_root - self._drag_y}")

    def _keep_on_top(self):
        # Reassert topmost without activating the widget or stealing game input.
        try:
            flags = 0x0001 | 0x0002 | 0x0010 | 0x0040  # NOSIZE | NOMOVE | NOACTIVATE | SHOWWINDOW
            _user32.SetWindowPos(self._hwnd, -1, 0, 0, 0, 0, flags)
        except (AttributeError, OSError, tk.TclError):
            pass
        self.root.after(1000, self._keep_on_top)

    @staticmethod
    async def _read_spotify_track(cached_track_key):
        manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
        sessions = manager.get_sessions()
        spotify = [s for s in sessions if "spotify" in s.source_app_user_model_id.lower()]
        session = next((s for s in spotify if s.get_playback_info().playback_status == PlaybackStatus.PLAYING),
                       spotify[0] if spotify else None)
        if session is None:
            return {
                "title": "Kein Spotify-Titel gefunden",
                "artist": "Spotify öffnen und Musik starten",
                "key": None,
                "cover_changed": cached_track_key is not None,
                "position": 0.0,
                "duration": 0.0,
                "playing": False,
            }

        properties = await session.try_get_media_properties_async()
        if properties is None:
            return {
                "title": "Titel gerade nicht verfügbar",
                "artist": "Spotify wird erneut abgefragt",
                "key": None,
                "cover_changed": cached_track_key is not None,
                "position": 0.0,
                "duration": 0.0,
                "playing": False,
            }

        playing = session.get_playback_info().playback_status == PlaybackStatus.PLAYING
        title = properties.title or "Unbekannter Titel"
        artist = properties.artist or ("Wird abgespielt" if playing else "Pausiert")
        track_key = (session.source_app_user_model_id, title, artist, properties.album_title)
        timeline = session.get_timeline_properties()
        position = max(0.0, timeline.position.total_seconds())
        duration = max(0.0, (timeline.end_time - timeline.start_time).total_seconds())

        cover_bytes = None
        cover_changed = track_key != cached_track_key
        thumbnail = properties.thumbnail if cover_changed else None
        if thumbnail is not None:
            stream = None
            reader = None
            try:
                stream = await thumbnail.open_read_async()
                size = int(stream.size)
                if 0 < size <= 10 * 1024 * 1024:
                    reader = DataReader(stream.get_input_stream_at(0))
                    await reader.load_async(size)
                    image_data = bytearray(size)
                    reader.read_bytes(image_data)
                    cover_bytes = bytes(image_data)
            except Exception:
                cover_bytes = None
            finally:
                if reader is not None:
                    reader.close()
                if stream is not None:
                    stream.close()

        return {
            "title": title,
            "artist": artist,
            "key": track_key,
            "cover_changed": cover_changed,
            "cover": cover_bytes,
            "position": position,
            "duration": duration,
            "playing": playing,
        }

    def _refresh(self):
        if self._fetching:
            self.root.after(2000, self._refresh)
            return
        self._fetching = True

        def fetch():
            try:
                result = asyncio.run(self._read_spotify_track(self._track_key))
            except Exception:
                result = {"error": True}
            try:
                self.root.after(0, lambda: self._show_track(result))
            except tk.TclError:
                pass

        threading.Thread(target=fetch, daemon=True).start()
        self.root.after(2000, self._refresh)

    def _show_track(self, result):
        self._fetching = False
        if result.get("error"):
            self.artist.configure(text="Spotify wird erneut abgefragt …")
            return

        title, artist = result["title"], result["artist"]
        self.title.configure(text=title)
        self.artist.configure(text=artist)
        self.root.title(f"{title} — {artist} | Spotify Overlay")
        if result["cover_changed"]:
            self._set_cover(result.get("cover"))
        self._track_key = result["key"]
        self._position = result["position"]
        self._duration = result["duration"]
        self._position_time = time.monotonic()
        self._playing = result["playing"]
        self._draw_progress()

    def _set_cover(self, cover_data):
        self._cover_photo = None
        if cover_data:
            try:
                image = Image.open(BytesIO(cover_data)).convert("RGB")
                image = ImageOps.fit(image, (128, 128), method=Image.Resampling.LANCZOS)
                self._cover_photo = ImageTk.PhotoImage(image)
                self.cover.configure(image=self._cover_photo, text="", bg="#171820")
                return
            except Exception:
                pass
        self.cover.configure(image="", text="♫", bg="#1DB954")

    @staticmethod
    def _format_time(seconds):
        total = max(0, int(seconds))
        minutes, seconds = divmod(total, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"

    def _draw_progress(self):
        width = self.progress.winfo_width()
        ratio = min(1.0, self._position / self._duration) if self._duration > 0 else 0.0
        self.progress.coords(self.progress_fill, 0, 0, width * ratio, 4)
        shown_position = self._position
        if self._playing:
            shown_position += time.monotonic() - self._position_time
        shown_position = min(shown_position, self._duration) if self._duration > 0 else shown_position
        self.elapsed.configure(text=self._format_time(shown_position))
        self.duration.configure(text=self._format_time(self._duration))

    def _tick_progress(self):
        self._draw_progress()
        self.root.after(500, self._tick_progress)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    SpotifyOverlay().run()
