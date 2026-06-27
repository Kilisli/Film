import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from tkinter import (
    BOTH,
    LEFT,
    RIGHT,
    Y,
    NW,
    Button,
    Canvas,
    Checkbutton,
    Entry,
    Frame,
    IntVar,
    Label,
    Scrollbar,
    StringVar,
    OptionMenu,
    Tk,
    Toplevel,
    filedialog,
    messagebox,
)
from enum import Enum

APP_TITLE = "MAC to M3U Converter"
DEFAULT_TIMEOUT = 8

# Canlı ve Modern Renk Paleti (Koyu Tema)
BG_MAIN = "#1E2229"         # Derin antrasit/lacivert ana arka plan
BG_CARD = "#282C34"         # Kartlar ve paneller için koyu gri/mavi
TEXT_MAIN = "#ECEFF1"       # Parlak beyaz ana metin rengi
TEXT_MUTED = "#B0BEC5"      # Yardımcı ve soluk metin rengi
ENTRY_BG = "#353B45"        # Giriş kutuları arka planı

ACCENT_BLUE = "#00E5FF"     # Canlı turkuaz/mavi (Ana butonlar ve vurgular)
ACCENT_GREEN = "#00E676"    # Canlı yeşil (İndirme/Kaydet butonu)
ACCENT_CANCEL = "#FF5252"   # Canlı kırmızı (İptal ve Temizle butonları)
ACCENT_PURPLE = "#E040FB"   # Mor (VOD/Film butonları)
BORDER_COLOR = "#3E4451"    # Kontrast sağlayan ince kenarlık rengi


class ContentType(Enum):
    LIVE = "live"
    VOD = "vod"


class PortalError(Exception):
    pass


@dataclass
class Category:
    id: str
    title: str


@dataclass
class Channel:
    name: str
    category_id: str
    cmd: str
    logo: str = ""
    tvg_id: str = ""


@dataclass
class Movie:
    name: str
    category_id: str
    cmd: str
    movie_id: str = ""
    logo: str = ""
    year: str = ""
    description: str = ""


def normalize_mac(mac: str) -> str:
    value = mac.strip().upper().replace("-", ":")
    value = re.sub(r"[^0-9A-F:]", "", value)
    if ":" not in value and len(value) == 12:
        value = ":".join(value[i : i + 2] for i in range(0, 12, 2))
    if not re.fullmatch(r"[0-9A-F]{2}(:[0-9A-F]{2}){5}", value):
        raise PortalError("MAC adresi geçersiz. Örnek: 00:1A:79:22:02:5E")
    return value


def normalize_portal_url(url: str) -> str:
    value = url.strip()
    if not value:
        raise PortalError("Portal URL boş olamaz.")
    if not re.match(r"^https?://", value, re.I):
        value = "http://" + value

    parsed = urllib.parse.urlsplit(value)
    path = parsed.path.rstrip("/")
    if path.endswith("/portal.php"):
        path = path[: -len("/portal.php")]
    if path.endswith("/server/load.php"):
        path = path[: -len("/server/load.php")]
    normalized = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, path.rstrip("/"), "", "")
    )
    return normalized.rstrip("/")


def clean_stream_cmd(cmd: str) -> str:
    cmd = (cmd or "").strip()
    if not cmd:
        return ""
    cmd = re.sub(r"^(ffmpeg|auto)\s+", "", cmd, flags=re.I).strip()
    if " " in cmd and "http" in cmd:
        cmd = cmd[cmd.find("http") :].strip()
    return cmd


def m3u_escape(value: str) -> str:
    return (value or "").replace('"', "'").strip()


class StalkerPortalClient:
    def __init__(self, portal_url: str, mac: str, timeout: int = DEFAULT_TIMEOUT):
        self.portal_url = normalize_portal_url(portal_url)
        self.mac = normalize_mac(mac)
        self.timeout = timeout
        self.token = ""
        self.profile = {}

    @property
    def api_url(self) -> str:
        return self.portal_url + "/portal.php"

    def _headers(self) -> dict:
        headers = {
            "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C) MAG200 stbapp",
            "Accept": "*/*",
            "X-User-Agent": "Model: MAG254; Link: Ethernet",
            "Referer": self.portal_url + "/c/",
            "Cookie": (
                "mac={mac}; stb_lang=en; timezone=Europe%2FIstanbul"
            ).format(mac=urllib.parse.quote(self.mac)),
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        return headers

    def _request_json(self, params: dict) -> dict:
        query = dict(params)
        query.setdefault("JsHttpRequest", "1-xml")
        url = self.api_url + "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise PortalError(f"Portal HTTP hata verdi: {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise PortalError(f"Portala bağlanılamadı: {exc.reason}") from exc
        except OSError as exc:
            raise PortalError(f"Portal bağlantıyı kapattı: {exc}") from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PortalError("Portal beklenen JSON cevabını vermedi.") from exc

        if isinstance(payload, dict) and "js" in payload:
            return payload["js"] or {}
        return payload

    def handshake(self) -> None:
        data = self._request_json({"type": "stb", "action": "handshake"})
        token = data.get("token")
        if not token:
            raise PortalError("Hesap doğrulanamadı veya token alınamadı.")
        self.token = token

    def get_profile(self) -> dict:
        data = self._request_json(
            {
                "type": "stb",
                "action": "get_profile",
                "hd": "1",
                "ver": "ImageDescription: 0.2.18-r23-254",
                "num_banks": "2",
            }
        )
        self.profile = data if isinstance(data, dict) else {}
        return self.profile

    # ========== LIVE TV METODLARI ==========
    def get_live_categories(self) -> list[Category]:
        data = self._request_json({"type": "itv", "action": "get_genres"})
        if isinstance(data, dict):
            rows = data.get("data") or data.get("genres") or []
        else:
            rows = data or []
        categories = []
        for row in rows:
            cat_id = str(row.get("id") or row.get("genre_id") or "").strip()
            title = str(row.get("title") or row.get("name") or "").strip()
            if cat_id and title:
                categories.append(Category(cat_id, title))
        return categories

    def _rows_to_channels(self, rows: list[dict], fallback_category_id: str = "") -> list[Channel]:
        channels = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            category_id = str(
                row.get("tv_genre_id")
                or row.get("category_id")
                or row.get("genre_id")
                or fallback_category_id
            ).strip()
            channels.append(
                Channel(
                    name=str(row.get("name") or row.get("title") or "").strip(),
                    category_id=category_id,
                    cmd=str(row.get("cmd") or row.get("stream_url") or row.get("url") or "").strip(),
                    logo=str(row.get("logo") or row.get("screenshot_uri") or "").strip(),
                    tvg_id=str(row.get("xmltv_id") or row.get("id") or "").strip(),
                )
            )
        return [channel for channel in channels if channel.name and channel.cmd]

    def get_all_channels(self) -> list[Channel]:
        data = self._request_json(
            {"type": "itv", "action": "get_all_channels", "force_ch_link_check": "0"}
        )
        rows = data.get("data") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            rows = []
        return self._rows_to_channels(rows)

    def get_channels_for_category(
        self, category_id: str, page_callback=None
    ) -> list[Channel]:
        channels = []
        seen = set()
        page = 1
        while page <= 200:
            if page_callback:
                page_callback(page)
            data = self._request_json(
                {
                    "type": "itv",
                    "action": "get_ordered_list",
                    "genre": category_id,
                    "p": str(page),
                    "fav": "0",
                    "sortby": "number",
                    "hd": "0",
                    "force_ch_link_check": "0",
                }
            )
            rows = data.get("data") if isinstance(data, dict) else data
            if not isinstance(rows, list) or not rows:
                break

            page_channels = self._rows_to_channels(rows, category_id)
            new_count = 0
            for channel in page_channels:
                key = (channel.name, channel.cmd)
                if key in seen:
                    continue
                seen.add(key)
                channels.append(channel)
                new_count += 1
            if new_count == 0:
                break

            max_page = 0
            if isinstance(data, dict):
                try:
                    max_page = int(data.get("max_page") or data.get("total_pages") or 0)
                except (TypeError, ValueError):
                    max_page = 0
            if max_page and page >= max_page:
                break
            if len(rows) < 14 and not max_page:
                break
            page += 1
        return channels

    def get_channels_for_categories(
        self,
        categories: list[Category],
        selected_ids: set[str],
        progress_callback=None,
    ) -> list[Channel]:
        selected_categories = [
            category for category in categories if category.id in selected_ids
        ]
        channels = []
        seen = set()
        errors = []
        for cat_index, category in enumerate(selected_categories, start=1):
            def page_callback(page, category=category, cat_index=cat_index):
                if progress_callback:
                    progress_callback(
                        0,
                        0,
                        f"Kategori okunuyor: {cat_index}/{len(selected_categories)} "
                        f"- {category.title} (sayfa {page})",
                    )

            try:
                category_channels = self.get_channels_for_category(
                    category.id, page_callback
                )
            except PortalError as exc:
                errors.append(f"{category.title}: {exc}")
                continue

            for channel in category_channels:
                key = (channel.name, channel.cmd)
                if key in seen:
                    continue
                seen.add(key)
                channels.append(channel)
        if not channels and errors:
            raise PortalError(
                "Kanal listesi alınamadı. Seçilen kategoriler için "
                "bağlantı kapatıldı. Daha az kategori seçip tekrar deneyin.\n\n"
                + "\n".join(errors[:5])
            )
        return channels

    # ========== VOD / FİLM METODLARI ==========
    def get_vod_categories(self) -> list[Category]:
        """Film kategorilerini alır (type=vod, action=get_categories)"""
        data = self._request_json({"type": "vod", "action": "get_categories"})
        if isinstance(data, dict):
            rows = data.get("data") or data.get("categories") or []
        else:
            rows = data or []
        categories = []
        for row in rows:
            cat_id = str(row.get("id") or row.get("category_id") or "").strip()
            title = str(row.get("title") or row.get("name") or row.get("category_name") or "").strip()
            if cat_id and title:
                categories.append(Category(cat_id, title))
        return categories

    def _rows_to_movies(self, rows: list[dict], fallback_category_id: str = "") -> list[Movie]:
        movies = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            category_id = str(
                row.get("category_id")
                or row.get("cat_id")
                or row.get("genre_id")
                or fallback_category_id
            ).strip()

            cmd = str(row.get("cmd") or row.get("stream_url") or row.get("url") or "").strip()
            # Eğer cmd boşsa, movie_id ile create_link yapılacak
            movie_id = str(row.get("id") or row.get("movie_id") or row.get("video_id") or "").strip()

            movies.append(
                Movie(
                    name=str(row.get("name") or row.get("title") or "").strip(),
                    category_id=category_id,
                    cmd=cmd,
                    movie_id=movie_id,
                    logo=str(row.get("screenshot_uri") or row.get("logo") or row.get("poster") or "").strip(),
                    year=str(row.get("year") or row.get("release_year") or "").strip(),
                    description=str(row.get("description") or row.get("plot") or "").strip(),
                )
            )
        return [movie for movie in movies if movie.name and (movie.cmd or movie.movie_id)]

    def get_movies_for_category(
        self, category_id: str, page_callback=None
    ) -> list[Movie]:
        movies = []
        seen = set()
        page = 1
        while page <= 200:
            if page_callback:
                page_callback(page)
            data = self._request_json(
                {
                    "type": "vod",
                    "action": "get_ordered_list",
                    "category": category_id,
                    "p": str(page),
                    "sortby": "added",
                }
            )
            rows = data.get("data") if isinstance(data, dict) else data
            if not isinstance(rows, list) or not rows:
                break

            page_movies = self._rows_to_movies(rows, category_id)
            new_count = 0
            for movie in page_movies:
                key = (movie.name, movie.movie_id or movie.cmd)
                if key in seen:
                    continue
                seen.add(key)
                movies.append(movie)
                new_count += 1
            if new_count == 0:
                break

            max_page = 0
            if isinstance(data, dict):
                try:
                    max_page = int(data.get("max_page") or data.get("total_pages") or 0)
                except (TypeError, ValueError):
                    max_page = 0
            if max_page and page >= max_page:
                break
            if len(rows) < 14 and not max_page:
                break
            page += 1
        return movies

    def get_movies_for_categories(
        self,
        categories: list[Category],
        selected_ids: set[str],
        progress_callback=None,
    ) -> list[Movie]:
        selected_categories = [
            category for category in categories if category.id in selected_ids
        ]
        movies = []
        seen = set()
        errors = []
        for cat_index, category in enumerate(selected_categories, start=1):
            def page_callback(page, category=category, cat_index=cat_index):
                if progress_callback:
                    progress_callback(
                        0,
                        0,
                        f"Film kategorisi okunuyor: {cat_index}/{len(selected_categories)} "
                        f"- {category.title} (sayfa {page})",
                    )

            try:
                category_movies = self.get_movies_for_category(
                    category.id, page_callback
                )
            except PortalError as exc:
                errors.append(f"{category.title}: {exc}")
                continue

            for movie in category_movies:
                key = (movie.name, movie.movie_id or movie.cmd)
                if key in seen:
                    continue
                seen.add(key)
                movies.append(movie)
        if not movies and errors:
            raise PortalError(
                "Film listesi alınamadı. Seçilen kategoriler için "
                "bağlantı kapatıldı. Daha az kategori seçip tekrar deneyin.\n\n"
                + "\n".join(errors[:5])
            )
        return movies

    def create_vod_link(self, cmd: str) -> str:
        """VOD stream linki oluşturur. Eğer cmd direkt URL ise onu döndürür,
        değilse create_link API'sini kullanır."""
        if cmd and (cmd.startswith("http://") or cmd.startswith("https://") or cmd.startswith("rtmp://")):
            return cmd

        # create_link ile stream URL al
        data = self._request_json({
            "type": "vod",
            "action": "create_link",
            "cmd": cmd,
        })

        if isinstance(data, dict):
            link = data.get("link") or data.get("cmd") or data.get("url") or ""
            if link:
                return link

        # Direkt cmd döndür (bazı portallar cmd'yi direkt kullanır)
        return cmd


def build_live_m3u(
    client: StalkerPortalClient,
    categories: list[Category],
    selected_ids: set[str],
    progress_callback=None,
) -> str:
    category_names = {category.id: category.title for category in categories}
    if progress_callback:
        progress_callback(0, 0, "Kanal listesi alınıyor...")
    channels = client.get_channels_for_categories(
        categories, selected_ids, progress_callback=progress_callback
    )
    if not channels:
        raise PortalError("Seçilen kategorilerde kanal bulunamadı.")

    lines = ["#EXTM3U"]
    total = len(channels)
    for index, channel in enumerate(channels, start=1):
        if progress_callback:
            progress_callback(index, total, channel.name)
        stream_url = clean_stream_cmd(channel.cmd)
        if not stream_url:
            continue
        group = category_names.get(channel.category_id, "")
        lines.append(
            '#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{name}" tvg-logo="{logo}" '
            'group-title="{group}",{name}'.format(
                tvg_id=m3u_escape(channel.tvg_id),
                name=m3u_escape(channel.name),
                logo=m3u_escape(channel.logo),
                group=m3u_escape(group),
            )
        )
        lines.append(stream_url)
        time.sleep(0.005)

    if len(lines) == 1:
        raise PortalError("Stream linkleri oluşturulamadı.")
    return "\n".join(lines) + "\n"


def build_vod_m3u(
    client: StalkerPortalClient,
    categories: list[Category],
    selected_ids: set[str],
    progress_callback=None,
) -> str:
    category_names = {category.id: category.title for category in categories}
    if progress_callback:
        progress_callback(0, 0, "Film listesi alınıyor...")
    movies = client.get_movies_for_categories(
        categories, selected_ids, progress_callback=progress_callback
    )
    if not movies:
        raise PortalError("Seçilen kategorilerde film bulunamadı.")

    lines = ["#EXTM3U"]
    total = len(movies)
    for index, movie in enumerate(movies, start=1):
        if progress_callback:
            progress_callback(index, total, movie.name)

        # Stream linkini al
        stream_url = clean_stream_cmd(movie.cmd)
        if not stream_url:
            # create_link ile dene
            try:
                stream_url = client.create_vod_link(movie.cmd)
            except Exception:
                continue
        if not stream_url:
            continue

        group = category_names.get(movie.category_id, "")

        # Film için tvg-name ve group-title kullan
        year_info = f" ({movie.year})" if movie.year else ""
        display_name = movie.name + year_info

        lines.append(
            '#EXTINF:-1 tvg-name="{name}" tvg-logo="{logo}" '
            'group-title="{group}",{display_name}'.format(
                name=m3u_escape(movie.name),
                logo=m3u_escape(movie.logo),
                group=m3u_escape(group),
                display_name=m3u_escape(display_name),
            )
        )
        lines.append(stream_url)
        time.sleep(0.005)

    if len(lines) == 1:
        raise PortalError("Film stream linkleri oluşturulamadı.")
    return "\n".join(lines) + "\n"


class CategoryDialog(Toplevel):
    def __init__(self, parent, categories: list[Category], content_type: ContentType):
        super().__init__(parent)
        self.content_type = content_type
        title_prefix = "Film Kategorileri" if content_type == ContentType.VOD else "Kategorileri"
        self.title(f"{title_prefix} Seç")
        self.geometry("540x600")
        self.configure(bg=BG_MAIN)
        self.categories = categories
        self.result = None

        # Üst Bilgi ve Renkli Hızlı Seçim Paneli
        header = Frame(self, bg=BG_MAIN)
        header.pack(fill="x", padx=16, pady=14)

        type_label = "Film" if content_type == ContentType.VOD else "Kategori"
        Label(
            header, 
            text=f"{len(categories)} {type_label.lower()} bulundu", 
            font=("Segoe UI", 11, "bold"), 
            bg=BG_MAIN, 
            fg=ACCENT_PURPLE if content_type == ContentType.VOD else ACCENT_BLUE
        ).pack(side=LEFT)

        Button(
            header, text="Tümünü Seç", command=self.select_all,
            font=("Segoe UI", 9, "bold"), bg=ACCENT_BLUE, fg="#121212", 
            activebackground="#00B8D4", activeforeground="#121212", relief="flat", padx=10, pady=4
        ).pack(side=RIGHT, padx=(6, 0))

        Button(
            header, text="Temizle", command=self.clear_all,
            font=("Segoe UI", 9, "bold"), bg=ACCENT_CANCEL, fg=TEXT_MAIN, 
            activebackground="#D32F2F", activeforeground=TEXT_MAIN, relief="flat", padx=10, pady=4
        ).pack(side=RIGHT)

        # Renkli Liste Taşıyıcı Alanı
        container_frame = Frame(self, bg=BORDER_COLOR, bd=1)
        container_frame.pack(fill=BOTH, expand=True, padx=16, pady=(0, 14))

        canvas = Canvas(container_frame, bg=BG_CARD, highlightthickness=0)
        scrollbar = Scrollbar(container_frame, orient="vertical", command=canvas.yview)

        self.scrollable_frame = Frame(canvas, bg=BG_CARD)
        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        canvas.create_window((0, 0), window=self.scrollable_frame, anchor=NW)
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar.pack(side=RIGHT, fill=Y)

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        self.checkbox_vars = {}
        for number, category in enumerate(categories, start=1):
            var = IntVar(value=1)
            self.checkbox_vars[category.id] = var

            row_frame = Frame(self.scrollable_frame, bg=BG_CARD, pady=5)
            row_frame.pack(fill="x", anchor="w", padx=12)

            cb = Checkbutton(
                row_frame,
                text=f"{number:03d}  •  {category.title}",
                variable=var,
                font=("Segoe UI", 10),
                bg=BG_CARD,
                fg=TEXT_MAIN,
                selectcolor=ENTRY_BG,
                activebackground=BG_CARD,
                activeforeground=ACCENT_PURPLE if content_type == ContentType.VOD else ACCENT_BLUE
            )
            cb.pack(side=LEFT, anchor="w")

        # Alt Butonlar Alanı
        footer = Frame(self, bg=BG_MAIN)
        footer.pack(fill="x", padx=16, pady=(0, 18))

        Button(
            footer, text="İptal", command=self.cancel,
            font=("Segoe UI", 10, "bold"), bg=BORDER_COLOR, fg=TEXT_MAIN, 
            activebackground=ENTRY_BG, activeforeground=TEXT_MAIN, relief="flat", padx=16, pady=6
        ).pack(side=RIGHT)

        btn_text = "Film M3U Oluştur" if content_type == ContentType.VOD else "M3U Oluştur"
        btn_color = ACCENT_PURPLE if content_type == ContentType.VOD else ACCENT_GREEN
        btn_active = "#D500F9" if content_type == ContentType.VOD else "#00C853"

        Button(
            footer, text=btn_text, command=self.ok,
            font=("Segoe UI", 10, "bold"), bg=btn_color, fg="#121212", 
            activebackground=btn_active, activeforeground="#121212", relief="flat", padx=16, pady=6
        ).pack(side=RIGHT, padx=12)

        self.transient(parent)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.cancel)

    def select_all(self):
        for var in self.checkbox_vars.values():
            var.set(1)

    def clear_all(self):
        for var in self.checkbox_vars.values():
            var.set(0)

    def ok(self):
        selected_ids = {cat_id for cat_id, var in self.checkbox_vars.items() if var.get() == 1}
        if not selected_ids:
            messagebox.showwarning(APP_TITLE, "En az bir kategori seçin.")
            return
        self.result = selected_ids
        self.destroy()

    def cancel(self):
        self.result = None
        self.destroy()


class App:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("640x480")
        self.root.configure(bg=BG_MAIN)
        self.client = None
        self.categories = []
        self.content_type = ContentType.LIVE

        container = Frame(root, padx=24, pady=20, bg=BG_MAIN)
        container.pack(fill=BOTH, expand=True)

        # Başlık Bölümü
        Label(
            container, text=APP_TITLE, font=("Segoe UI", 20, "bold"), 
            bg=BG_MAIN, fg=ACCENT_BLUE
        ).pack(anchor="w", pady=(0, 16))

        # Portal Giriş Kartı (Renkli Koyu Panel)
        form_frame = Frame(container, bg=BG_CARD, bd=1, relief="solid", highlightthickness=0)
        form_frame.config(highlightbackground=BORDER_COLOR, highlightcolor=BORDER_COLOR)
        form_frame.pack(fill="x", pady=(0, 16), padx=2)

        inner_form = Frame(form_frame, bg=BG_CARD, padx=16, pady=16)
        inner_form.pack(fill="x")

        Label(inner_form, text="Portal URL", font=("Segoe UI", 10, "bold"), bg=BG_CARD, fg=TEXT_MUTED).pack(anchor="w")
        self.portal_entry = Entry(
            inner_form, font=("Consolas", 11), bg=ENTRY_BG, fg=TEXT_MAIN, 
            insertbackground=TEXT_MAIN, bd=0, highlightthickness=1, highlightbackground=BORDER_COLOR, highlightcolor=ACCENT_BLUE
        )
        self.portal_entry.pack(fill="x", pady=(6, 14), ipady=3)
        self.portal_entry.insert(0, "http://example.com")

        Label(inner_form, text="MAC Adresi", font=("Segoe UI", 10, "bold"), bg=BG_CARD, fg=TEXT_MUTED).pack(anchor="w")
        self.mac_entry = Entry(
            inner_form, font=("Consolas", 11), bg=ENTRY_BG, fg=TEXT_MAIN, 
            insertbackground=TEXT_MAIN, bd=0, highlightthickness=1, highlightbackground=BORDER_COLOR, highlightcolor=ACCENT_BLUE
        )
        self.mac_entry.pack(fill="x", pady=(6, 4), ipady=3)
        self.mac_entry.insert(0, "00:1A:79:00:00:00")

        # İçerik Türü Seçimi
        content_frame = Frame(container, bg=BG_MAIN)
        content_frame.pack(fill="x", pady=(0, 14))

        Label(content_frame, text="İçerik Türü:", font=("Segoe UI", 10, "bold"), 
              bg=BG_MAIN, fg=TEXT_MUTED).pack(side=LEFT, padx=(0, 10))

        self.content_var = StringVar(value="Canlı TV")
        content_options = ["Canlı TV", "Filmler (VOD)"]
        self.content_menu = OptionMenu(
            content_frame, self.content_var, *content_options,
            command=self.on_content_type_change
        )
        self.content_menu.config(
            font=("Segoe UI", 10),
            bg=ENTRY_BG, fg=TEXT_MAIN,
            activebackground=ENTRY_BG, activeforeground=TEXT_MAIN,
            highlightthickness=0, bd=0, relief="flat",
            width=15
        )
        self.content_menu["menu"].config(
            bg=ENTRY_BG, fg=TEXT_MAIN,
            activebackground=ACCENT_BLUE, activeforeground="#121212"
        )
        self.content_menu.pack(side=LEFT)

        # Seçenekler Paneli
        options_frame = Frame(container, bg=BG_MAIN)
        options_frame.pack(fill="x", pady=(0, 14))

        self.only_check = IntVar(value=0)
        Checkbutton(
            options_frame,
            text="Sadece hesabı kontrol et, dosya oluşturma",
            variable=self.only_check,
            font=("Segoe UI", 10),
            bg=BG_MAIN,
            fg=TEXT_MAIN,
            selectcolor=ENTRY_BG,
            activebackground=BG_MAIN,
            activeforeground=ACCENT_BLUE
        ).pack(anchor="w")

        # İşlem Butonları Paneli
        buttons = Frame(container, bg=BG_MAIN)
        buttons.pack(fill="x", pady=(6, 12))

        self.fetch_button = Button(
            buttons, text="Kategorileri Getir", command=self.start_fetch,
            font=("Segoe UI", 10, "bold"), bg=ACCENT_BLUE, fg="#121212",
            activebackground="#00B8D4", activeforeground="#121212", relief="flat", padx=18, pady=7
        )
        self.fetch_button.pack(side=LEFT)

        self.save_button = Button(
            buttons, text="M3U İndir / Kaydet", command=self.choose_categories, state="disabled",
            font=("Segoe UI", 10, "bold"), bg=ACCENT_GREEN, fg="#121212",
            activebackground="#00C853", activeforeground="#121212", disabledforeground="#555555", relief="flat", padx=18, pady=7
        )
        self.save_button.pack(side=LEFT, padx=12)

        # VOD butonu
        self.vod_fetch_button = Button(
            buttons, text="Film Kategorileri Getir", command=self.start_vod_fetch,
            font=("Segoe UI", 10, "bold"), bg=ACCENT_PURPLE, fg="#121212",
            activebackground="#D500F9", activeforeground="#121212", relief="flat", padx=18, pady=7
        )
        self.vod_fetch_button.pack(side=LEFT)

        # Durum Çubuğu
        self.status = Label(
            container, text="Hazır.", anchor="w", justify=LEFT,
            font=("Segoe UI", 10, "normal"), bg=BG_CARD, fg=ACCENT_BLUE, padx=10, pady=6,
            bd=1, relief="solid", highlightthickness=0
        )
        self.status.config(highlightbackground=BORDER_COLOR)
        self.status.pack(fill="x", pady=(10, 0))

        note = (
            "Not: Bu araç sadece kendi yetkili hesabınızın normal portal API "
            "erişimiyle çalışır; erişim engeli atlatmaz."
        )
        Label(
            container, text=note, wraplength=580, justify=LEFT, 
            font=("Segoe UI", 9), bg=BG_MAIN, fg=TEXT_MUTED
        ).pack(fill="x", side="bottom", pady=(10, 0))

    def on_content_type_change(self, value):
        if value == "Filmler (VOD)":
            self.content_type = ContentType.VOD
            self.fetch_button.config(text="Film Kategorileri Getir", bg=ACCENT_PURPLE,
                                    activebackground="#D500F9")
            self.save_button.config(text="Film M3U İndir / Kaydet")
        else:
            self.content_type = ContentType.LIVE
            self.fetch_button.config(text="Kategorileri Getir", bg=ACCENT_BLUE,
                                    activebackground="#00B8D4")
            self.save_button.config(text="M3U İndir / Kaydet")

    def set_busy(self, busy: bool):
        if busy:
            self.fetch_button.config(state="disabled", bg=BORDER_COLOR)
            self.save_button.config(state="disabled", bg=BORDER_COLOR)
            self.vod_fetch_button.config(state="disabled", bg=BORDER_COLOR)
        else:
            if self.content_type == ContentType.VOD:
                self.fetch_button.config(state="normal", bg=ACCENT_PURPLE)
            else:
                self.fetch_button.config(state="normal", bg=ACCENT_BLUE)
            self.save_button.config(state=("normal" if self.categories else "disabled"), 
                                    bg=(ACCENT_GREEN if self.categories else BORDER_COLOR))
            self.vod_fetch_button.config(state="normal", bg=ACCENT_PURPLE)

    def set_status(self, text: str):
        self.status.config(text=text)

    def make_client(self) -> StalkerPortalClient:
        return StalkerPortalClient(self.portal_entry.get(), self.mac_entry.get())

    def start_fetch(self):
        if self.content_type == ContentType.VOD:
            self.start_vod_fetch()
        else:
            self.start_live_fetch()

    def start_live_fetch(self):
        self.set_busy(True)
        self.set_status("Portal kontrol ediliyor...")
        self.content_type = ContentType.LIVE
        threading.Thread(target=self.fetch_live_categories, daemon=True).start()

    def start_vod_fetch(self):
        self.set_busy(True)
        self.set_status("Portal kontrol ediliyor (VOD)...")
        self.content_type = ContentType.VOD
        threading.Thread(target=self.fetch_vod_categories, daemon=True).start()

    def fetch_live_categories(self):
        try:
            client = self.make_client()
            client.handshake()
            client.get_profile()
            categories = client.get_live_categories()
            if not categories:
                raise PortalError("Kategori bulunamadı.")
            self.client = client
            self.categories = categories
            message = f"Hesap doğrulandı. {len(categories)} canlı TV kategorisi bulundu."
            if self.only_check.get():
                message = "Hesap doğrulandı."
            self.root.after(0, lambda: self.set_status(message))
        except Exception as exc:
            self.root.after(0, lambda exc=exc: self.show_error(exc))
        finally:
            self.root.after(0, lambda: self.set_busy(False))

    def fetch_vod_categories(self):
        try:
            client = self.make_client()
            client.handshake()
            client.get_profile()
            categories = client.get_vod_categories()
            if not categories:
                raise PortalError("Film kategorisi bulunamadı.")
            self.client = client
            self.categories = categories
            message = f"Hesap doğrulandı. {len(categories)} film kategorisi bulundu."
            if self.only_check.get():
                message = "Hesap doğrulandı."
            self.root.after(0, lambda: self.set_status(message))
        except Exception as exc:
            self.root.after(0, lambda exc=exc: self.show_error(exc))
        finally:
            self.root.after(0, lambda: self.set_busy(False))

    def show_error(self, exc: Exception):
        self.categories = []
        self.client = None
        self.set_status("Hata: " + str(exc))
        messagebox.showerror(APP_TITLE, str(exc))

    def choose_categories(self):
        if not self.client or not self.categories:
            messagebox.showwarning(APP_TITLE, "Önce kategorileri getirin.")
            return
        dialog = CategoryDialog(self.root, self.categories, self.content_type)
        self.root.wait_window(dialog)
        if not dialog.result:
            return

        default_name = "movies.m3u" if self.content_type == ContentType.VOD else "playlist.m3u"
        target = filedialog.asksaveasfilename(
            title="M3U dosyasını kaydet",
            defaultextension=".m3u",
            filetypes=[("M3U playlist", "*.m3u"), ("Tüm dosyalar", "*.*")],
            initialfile=default_name,
        )
        if not target:
            return
        self.set_busy(True)
        self.set_status("M3U oluşturuluyor...")
        threading.Thread(
            target=self.save_m3u, args=(set(dialog.result), Path(target)), daemon=True
        ).start()

    def save_m3u(self, selected_ids: set[str], target: Path):
        try:
            def progress(index, total, name):
                if total:
                    item_type = "Filmler" if self.content_type == ContentType.VOD else "Kanallar"
                    text = f"{item_type} yazılıyor: {index}/{total} - {name}"
                else:
                    text = name
                self.root.after(
                    0,
                    lambda text=text: self.set_status(text),
                )

            if self.content_type == ContentType.VOD:
                playlist = build_vod_m3u(
                    self.client,
                    self.categories,
                    selected_ids,
                    progress_callback=progress,
                )
            else:
                playlist = build_live_m3u(
                    self.client,
                    self.categories,
                    selected_ids,
                    progress_callback=progress,
                )
            target.write_text(playlist, encoding="utf-8")
            self.root.after(
                0,
                lambda: (
                    self.set_status(f"Kaydedildi: {target}"),
                    messagebox.showinfo(APP_TITLE, f"M3U dosyası kaydedildi:\n{target}"),
                ),
            )
        except Exception as exc:
            self.root.after(0, lambda exc=exc: self.show_error(exc))
        finally:
            self.root.after(0, lambda: self.set_busy(False))


def main():
    root = Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
