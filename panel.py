
import json
import os
import queue
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import traceback
import urllib.parse
import webbrowser
from tkinter import ttk
from types import SimpleNamespace

import customtkinter as ctk
import psycopg2

import auditoria
import db
import scraping

try:
    import mapa                      # pip install tkintermapview
except ImportError:
    mapa = None

AQUI = os.path.dirname(os.path.abspath(__file__))
ARCHIVO_ESTADO = os.path.join(AQUI, ".panel_estado.json")

# ---------------- opciones (cámbialas a tu gusto) ----------------
SPLASH = True            # breve pantalla de arranque (se salta con un clic o Escape)

# ---------------- paleta · modo oscuro ----------------
BG = "#0b0d11"           # fondo de la ventana
PANEL = "#12151b"        # tarjetas y paneles
CAMPO = "#0e1116"        # campos de texto y zonas hundidas
BORDE = "#242a33"        # líneas finas
BORDE2 = "#313947"       # borde de controles
ACENTO = "#6c9bf5"       # azul suave (acción principal)
OK = "#5fbf8f"
AMBAR = "#e2b45a"
TEAL = "#5cc4c8"
VIOLETA = "#a58cf0"
ROJO = "#e5646f"
TEXTO = "#d5dae3"
GRIS = "#7c8594"         # texto secundario
DIM = "#566070"
BLANCO = "#f3f5f9"
HOVER = "#171b23"
SELEC = "#192131"
RADIO = 6                # esquinas redondeadas (0 = rectas)

COLOR_ESTADO = {
    "nuevo": ACENTO,
    "contactado": AMBAR,
    "respondio": TEAL,
    "cotizado": VIOLETA,
    "cerrado": OK,
    "descartado": DIM,
}
BOTONES_ESTADO = [
    ("Contactado", "contactado"),
    ("Respondió", "respondio"),
    ("Cotizado", "cotizado"),
    ("Cerrado", "cerrado"),
    ("Descartar", "descartado"),
]
AUTO = "auto"            # "elegir la sección más reciente"


def wame(tel, mensaje):
    return f"https://wa.me/{tel}?text={urllib.parse.quote(mensaje)}"


def cortar(texto, n):
    texto = texto or ""
    return texto if len(texto) <= n else texto[: n - 1] + "…"


def plural(n, uno, varios):
    return f"{n} {uno if n == 1 else varios}"


def tel_corto(tel):
    return tel[2:] if tel and tel.startswith("57") and len(tel) == 12 else tel



def fecha_corta(dt):
    try:
        return dt.astimezone().strftime("%d %b %H:%M")
    except Exception:
        return ""


def mezcla(a, b, t):
    """Mezcla dos colores #rrggbb: t = parte de `a` (0..1)."""
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x * t + y * (1 - t)):02x}" for x, y in zip(ca, cb))


# ====================================================================
#  App
# ====================================================================
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title("Scrap-map · Leads")
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(1440, sw - 40), min(960, sh - 90)
        self.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 2 - 20)}")
        self.minsize(min(1180, w), min(700, h))
        self.configure(fg_color=BG)

        self.fam = self._elegir_fuente()
        self.conn = None
        self.filas = []          # todos los leads
        self.scrapes = []        # secciones con contadores
        self.sec = AUTO          # sección elegida: id, None (todas) o AUTO
        self.sel = None          # id del lead seleccionado
        self._mostrado = None    # id del lead que se ve en el detalle
        self._sig_tree = None
        self.cards = {}
        self._orden = None
        self.vista_mapa = None
        self.urls = {}
        self.cola = queue.Queue()
        self.worker = None
        self._job = None
        self._ids_previos = set()
        self._sp = None
        self.auto_marcar = tk.BooleanVar(value=True)
        self.v_audit = tk.BooleanVar(value=True)
        self.v_ps = tk.BooleanVar(value=True)
        self.v_visible = tk.BooleanVar(value=False)

        self._conectar()
        self._estilo_tree()
        self._ui()
        self._cargar_estado()
        self.refrescar()
        self._banner_estado()

        self.after(100, self._drenar_cola)
        self.after(3000, self._poll)
        self._reloj()
        self.protocol("WM_DELETE_WINDOW", self._cerrar)
        self.bind("<Escape>", lambda _e: self._fin_splash())
        self._arranque()

    # ---------------- utilidades ----------------
    def _elegir_fuente(self):
        disp = set(tkfont.families(self))
        self.mono = next((f for f in ("JetBrains Mono", "Cascadia Mono", "Consolas", "SF Mono",
                                      "Menlo", "DejaVu Sans Mono", "Liberation Mono")
                          if f in disp), "Courier")
        for f in ("Inter", "Segoe UI", "SF Pro Text", "Helvetica Neue", "Ubuntu", "Noto Sans",
                  "Cantarell", "DejaVu Sans", "Arial"):
            if f in disp:
                return f
        return "Helvetica"

    def F(self, size=13, bold=False, mono=False):
        return ctk.CTkFont(family=self.mono if mono else self.fam, size=size,
                           weight="bold" if bold else "normal")

    def btn(self, master, texto, cmd, color=ACENTO, relleno=False, ancho=None):
        """Botón fino: contorno sutil (o relleno si es la acción principal)."""
        if relleno:
            fg, hov, txt, borde = color, mezcla(color, BLANCO, 0.82), BG, color
        else:
            fg, hov, txt = "transparent", mezcla(color, PANEL, 0.14), color
            borde = BORDE2 if color in (GRIS, TEXTO, BLANCO) else mezcla(color, PANEL, 0.5)
        return ctk.CTkButton(
            master, text=texto, command=cmd, font=self.F(12, relleno), height=30,
            width=ancho or 110, corner_radius=RADIO, fg_color=fg, hover_color=hov,
            text_color=txt, text_color_disabled=DIM, border_width=1, border_color=borde)

    def _conectar(self):
        try:
            self.conn = db.conectar()
            db.init_db(self.conn)
        except psycopg2.Error as e:
            self.conn = None
            err = str(e).strip()   # (e se borra al salir del except)
            self.after(400, lambda: self.log(
                f"Error: sin conexión a Postgres: {err}\n"
                "Revisa tu .env y que el contenedor esté arriba (docker ps)."))

    def msg(self, texto, color=TEXTO):
        self.status.configure(text=texto[:1].upper() + texto[1:], text_color=color)

    def _ocupado(self):
        return bool(self.worker and self.worker.is_alive())

    def _cargar_estado(self):
        """Recuerda el formulario y la sección que tenías abierta."""
        try:
            with open(ARCHIVO_ESTADO, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        if d.get("tipo"):
            self.e_tipo.insert(0, d["tipo"])
        if d.get("lugar"):
            self.e_lugar.insert(0, d["lugar"])
        self.e_max.delete(0, "end")
        self.e_max.insert(0, str(d.get("max") or 50))
        self.v_audit.set(d.get("audit", True))
        self.v_ps.set(d.get("ps", True))
        if "sec" in d:
            self.sec = d["sec"]

    def _guardar_estado(self):
        try:
            d = {"tipo": self.e_tipo.get(), "lugar": self.e_lugar.get(),
                 "max": self.e_max.get(), "audit": self.v_audit.get(), "ps": self.v_ps.get()}
            if self.sec != AUTO:
                d["sec"] = self.sec
            with open(ARCHIVO_ESTADO, "w", encoding="utf-8") as f:
                json.dump(d, f)
        except OSError:
            pass

    # ---------------- estilo del Treeview ----------------
    def _estilo_tree(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXTO,
                     borderwidth=0, rowheight=30, bordercolor=PANEL, lightcolor=PANEL,
                     darkcolor=PANEL, font=(self.fam, 11))
        st.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        st.configure("Treeview.Heading", background=PANEL, foreground=GRIS, relief="flat",
                     borderwidth=0, bordercolor=PANEL, lightcolor=PANEL, darkcolor=PANEL,
                     font=(self.fam, 9, "bold"), padding=(4, 6))
        st.map("Treeview", background=[("selected", SELEC)], foreground=[("selected", BLANCO)])
        st.map("Treeview.Heading", background=[("active", PANEL)])
        st.layout("Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
            ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        st.configure("Vertical.TScrollbar", background=BORDE, troughcolor=PANEL,
                     bordercolor=PANEL, lightcolor=BORDE, darkcolor=BORDE,
                     arrowcolor=PANEL, relief="flat", gripcount=0, width=8)
        st.map("Vertical.TScrollbar", background=[("active", BORDE2), ("!active", BORDE)])

    # ---------------- interfaz ----------------
    def _ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # --- cabecera ---
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 6))
        head.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(head, text="SCRAPMAP", font=self.F(17, True), text_color=BLANCO).grid(
            row=0, column=0, sticky="w")
        sub = ctk.CTkFrame(head, fg_color="transparent")
        sub.grid(row=0, column=1, sticky="w", padx=(8, 0))
        ctk.CTkLabel(sub, text="Leads", font=self.F(17), text_color=GRIS).pack(side="left")
        self.seg_vista = ctk.CTkSegmentedButton(
            sub, values=["Leads", "Mapa"], command=self.cambiar_vista, font=self.F(11),
            fg_color=CAMPO, selected_color="#1d2638", selected_hover_color="#222c42",
            unselected_color=CAMPO, unselected_hover_color=HOVER, text_color=TEXTO,
            corner_radius=RADIO, width=150)
        self.seg_vista.set("Leads")
        if mapa:
            self.seg_vista.pack(side="left", padx=(20, 0))
        self.lbl_reloj = ctk.CTkLabel(head, text="", font=self.F(11), text_color=GRIS)
        self.lbl_reloj.grid(row=0, column=4, sticky="e", padx=(18, 0))
        self.lbl_ia = ctk.CTkLabel(head, text="", font=self.F(11), text_color=GRIS)
        self.lbl_ia.grid(row=0, column=3, sticky="e", padx=(18, 0))
        self.lbl_db = ctk.CTkLabel(head, text="", font=self.F(11), text_color=GRIS)
        self.lbl_db.grid(row=0, column=2, sticky="e")
        ctk.CTkFrame(head, height=1, fg_color=BORDE, corner_radius=0).grid(
            row=1, column=0, columnspan=5, sticky="ew", pady=(10, 0))

        self._ui_control()

        self.main = main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=2, column=0, sticky="nsew", padx=14, pady=4)
        main.grid_columnconfigure(0, weight=0, minsize=252)
        main.grid_columnconfigure(1, weight=3)
        main.grid_columnconfigure(2, weight=2, minsize=420)
        main.grid_rowconfigure(0, weight=1)
        self._ui_secciones(main)
        self._ui_lista(main)
        self._ui_detalle(main)
        self._ui_mapa()

        cons = ctk.CTkFrame(self, fg_color=PANEL, border_width=1, border_color=BORDE,
                            corner_radius=RADIO)
        cons.grid(row=3, column=0, sticky="ew", padx=14, pady=(4, 2))
        cons.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(cons, text="Registro", font=self.F(11), text_color=GRIS).grid(
            row=0, column=0, sticky="w", padx=14, pady=(6, 0))
        self.consola = ctk.CTkTextbox(
            cons, height=78, fg_color=CAMPO, text_color="#a3acb9", font=self.F(11, mono=True),
            border_width=0, corner_radius=RADIO, state="disabled")
        self.consola.grid(row=1, column=0, sticky="ew", padx=8, pady=(2, 7))
        self.consola.tag_config("err", foreground=ROJO)
        self.consola.tag_config("cmd", foreground=ACENTO)
        self.consola.tag_config("ok", foreground=OK)

        self.status = ctk.CTkLabel(self, text="Listo", font=self.F(11),
                                   text_color=GRIS, anchor="w")
        self.status.grid(row=4, column=0, sticky="ew", padx=18, pady=(0, 6))

    def _ui_control(self):
        """Formulario del scraper: tipo, ciudad, máximo y opciones."""
        c = ctk.CTkFrame(self, fg_color=PANEL, border_width=1, border_color=BORDE,
                         corner_radius=RADIO)
        c.grid(row=1, column=0, sticky="ew", padx=14, pady=4)
        c.grid_columnconfigure(0, weight=3)
        c.grid_columnconfigure(1, weight=2)

        kw = dict(font=self.F(12), fg_color=CAMPO, border_color=BORDE, text_color=TEXTO,
                  placeholder_text_color=GRIS, height=34, corner_radius=RADIO)
        self.e_tipo = ctk.CTkEntry(c, placeholder_text="negocio(s):  restaurantes, odontólogos", **kw)
        self.e_tipo.grid(row=0, column=0, sticky="ew", padx=(10, 4), pady=(8, 4))
        self.e_lugar = ctk.CTkEntry(c, placeholder_text="ciudad(es):  Aguachica, Ocaña", **kw)
        self.e_lugar.grid(row=0, column=1, sticky="ew", padx=4, pady=(8, 4))
        self.e_max = ctk.CTkEntry(c, width=64, justify="center", placeholder_text="máx", **kw)
        self.e_max.insert(0, "50")
        self.e_max.grid(row=0, column=2, padx=4, pady=(8, 4))
        self.e_tipo.bind("<Return>", lambda _e: self.iniciar_scrape())
        self.e_lugar.bind("<Return>", lambda _e: self.iniciar_scrape())

        self.b_start = self.btn(c, "Iniciar", self.iniciar_scrape, ACENTO, True, 100)
        self.b_start.grid(row=0, column=3, padx=4, pady=(8, 4))
        self.b_stop = self.btn(c, "Detener", self.detener, ROJO, False, 100)
        self.b_stop.grid(row=0, column=4, padx=4, pady=(8, 4))
        self.b_follow = self.btn(c, "Seguimientos", self.iniciar_followups, GRIS, False, 120)
        self.b_follow.grid(row=0, column=5, padx=(4, 10), pady=(8, 4))

        fila = ctk.CTkFrame(c, fg_color="transparent")
        fila.grid(row=1, column=0, columnspan=6, sticky="ew", padx=12, pady=(0, 4))
        for txt, var in (("auditar webs", self.v_audit), ("PageSpeed", self.v_ps),
                         ("mostrar navegador", self.v_visible)):
            ctk.CTkCheckBox(fila, text=txt, variable=var, font=self.F(11), text_color=GRIS,
                            fg_color=ACENTO, hover_color=ACENTO, border_color=BORDE2,
                            checkmark_color=BG, corner_radius=4, checkbox_width=16,
                            checkbox_height=16).pack(side="left", padx=(0, 16))
        self.lbl_run = ctk.CTkLabel(fila, text="Inactivo", font=self.F(11), text_color=DIM)
        self.lbl_run.pack(side="right")

        self.prog = ctk.CTkProgressBar(c, mode="determinate", height=2, corner_radius=0,
                                       fg_color=BORDE, progress_color=ACENTO)
        self.prog.grid(row=2, column=0, columnspan=6, sticky="ew", padx=10, pady=(0, 7))
        self.prog.set(0)

    # ---------- vista de mapa ----------
    def _ui_mapa(self):
        if not mapa:
            return
        tema = SimpleNamespace(
            BG=BG, PANEL=PANEL, CAMPO=CAMPO, BORDE=BORDE, BORDE2=BORDE2, ACENTO=ACENTO,
            TEXTO=TEXTO, GRIS=GRIS, DIM=DIM, BLANCO=BLANCO, HOVER=HOVER, SELEC=SELEC,
            RADIO=RADIO, COLOR_ESTADO=COLOR_ESTADO, mezcla=mezcla)
        self.vista_mapa = mapa.VistaMapa(self, self, tema)
        self.vista_mapa.grid(row=2, column=0, sticky="nsew", padx=14, pady=4)
        self.vista_mapa.grid_remove()

    def cambiar_vista(self, vista):
        if not self.vista_mapa:
            return
        self.seg_vista.set(vista)
        if vista == "Mapa":
            self.main.grid_remove()
            self.vista_mapa.grid()
            self.vista_mapa.mostrar(True)
        else:
            self.vista_mapa.mostrar(False)
            self.vista_mapa.grid_remove()
            self.main.grid()

    def ir_a_lead(self, pid):
        """Salta a la vista Leads con ese lead seleccionado (desde un marcador del mapa)."""
        r = self._fila(pid)
        if not r:
            return
        self.cambiar_vista("Leads")
        self.sec = r["scrape_id"]
        self.seg.set("todos")
        self.busca.delete(0, "end")
        self._guardar_estado()
        self._pintar_secciones()
        self._cabecera_seccion()
        self.sel = None
        self._mostrado = None
        self.aplicar_filtro()
        self._stats()
        iid = str(pid)
        if self.tree.exists(iid):
            self.tree.selection_set(iid)
            self.tree.see(iid)

    # ---------- columna 0: secciones ----------
    def _ui_secciones(self, main):
        lado = ctk.CTkFrame(main, fg_color=PANEL, border_width=1, border_color=BORDE,
                            corner_radius=RADIO)
        lado.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        lado.grid_columnconfigure(0, weight=1)
        lado.grid_rowconfigure(1, weight=1)
        self.lbl_secs = ctk.CTkLabel(lado, text="Secciones", font=self.F(13, True),
                                     text_color=TEXTO, anchor="w")
        self.lbl_secs.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        self.lista_sec = ctk.CTkScrollableFrame(
            lado, fg_color="transparent", width=214, scrollbar_button_color=BORDE,
            scrollbar_button_hover_color=BORDE2)
        self.lista_sec.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 6))
        self.lista_sec.grid_columnconfigure(0, weight=1)

    def _crear_card(self, fila, sid):
        f = ctk.CTkFrame(self.lista_sec, fg_color=PANEL, border_width=1, border_color=BORDE,
                         corner_radius=RADIO)
        f.grid(row=fila, column=0, sticky="ew", padx=2, pady=2)
        f.grid_columnconfigure(0, weight=1)
        l1 = ctk.CTkLabel(f, text="", font=self.F(12, True), anchor="w")
        l1.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 0))
        l2 = ctk.CTkLabel(f, text="", font=self.F(10), anchor="w", text_color=GRIS)
        l2.grid(row=1, column=0, sticky="ew", padx=8)
        l3 = ctk.CTkLabel(f, text="", font=self.F(10), anchor="w", text_color=TEXTO)
        l3.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 6))
        card = {"f": f, "l1": l1, "l2": l2, "l3": l3, "sel": False}
        destino = sid or None            # 0 = "todos" -> None
        for w in (f, l1, l2, l3):
            w.bind("<Button-1>", lambda _e, d=destino: self.seleccionar_seccion(d))
            w.bind("<Enter>", lambda _e, c=card: self._hover(c, True))
            w.bind("<Leave>", lambda _e, c=card: self._hover(c, False))
            try:
                w.configure(cursor="hand2")
            except Exception:
                pass
        return card

    def _hover(self, card, on):
        if not card["sel"]:
            card["f"].configure(fg_color=HOVER if on else PANEL)

    def _pintar_secciones(self):
        items = [(0, None)] + [(s["id"], s) for s in self.scrapes]
        orden = [i for i, _ in items]
        if orden != self._orden:       # cambió el conjunto de secciones: reconstruir
            for c in self.cards.values():
                c["f"].destroy()
            self.cards = {sid: self._crear_card(i, sid) for i, (sid, _s) in enumerate(items)}
            self._orden = orden
        for sid, s in items:
            card = self.cards[sid]
            sel = self.sec == (sid or None)
            card["sel"] = sel
            if sid == 0:
                tot = sum(x["total"] for x in self.scrapes)
                nue = sum(x["nuevos"] for x in self.scrapes)
                l1, l2 = "Todos los leads", plural(len(self.scrapes), "sección", "secciones")
                l3 = f"{plural(tot, 'lead', 'leads')} · {plural(nue, 'nuevo', 'nuevos')}"
            else:
                marca = {"cancelado": " · cancelado", "error": " · error",
                         "corriendo": " · en curso"}.get(s["estado"], "")
                l1 = f"#{s['id']}  {cortar((s['tipo'] or '?').capitalize(), 20)}"
                l2 = f"{cortar(s['lugar'] or '?', 14)} · {fecha_corta(s['creado_en'])}"
                l3 = (f"{plural(s['total'], 'lead', 'leads')} · "
                      f"{plural(s['nuevos'], 'nuevo', 'nuevos')}{marca}")
            card["l1"].configure(text=l1, text_color=BLANCO if sel else TEXTO)
            card["l2"].configure(text=l2)
            card["l3"].configure(text=l3)
            card["f"].configure(fg_color=SELEC if sel else PANEL,
                                border_color=ACENTO if sel else BORDE)
        self.lbl_secs.configure(text=f"Secciones · {len(self.scrapes)}")

    def seleccionar_seccion(self, sid):
        self.sec = sid
        self._guardar_estado()
        self._pintar_secciones()
        self._cabecera_seccion()
        self.aplicar_filtro()
        self._stats()

    # ---------- columna 1: leads de la sección ----------
    def _ui_lista(self, main):
        izq = ctk.CTkFrame(main, fg_color=PANEL, border_width=1, border_color=BORDE,
                           corner_radius=RADIO)
        izq.grid(row=0, column=1, sticky="nsew", padx=6)
        izq.grid_columnconfigure(0, weight=1)
        izq.grid_rowconfigure(5, weight=1)

        cab = ctk.CTkFrame(izq, fg_color="transparent")
        cab.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 0))
        cab.grid_columnconfigure(0, weight=1)
        self.lbl_sec = ctk.CTkLabel(cab, text="", font=self.F(15, True), text_color=BLANCO,
                                    anchor="w")
        self.lbl_sec.grid(row=0, column=0, sticky="ew")
        self.b_del_sec = self.btn(cab, "Eliminar sección", self.eliminar_seccion,
                                  ROJO, False, 130)
        self.b_del_sec.grid(row=0, column=1, padx=(6, 0))
        self.btn(cab, "Actualizar", self.refrescar, GRIS, False, 84).grid(row=0, column=2, padx=(6, 0))
        self.lbl_sec_sub = ctk.CTkLabel(izq, text="", font=self.F(10), text_color=GRIS,
                                        anchor="w")
        self.lbl_sec_sub.grid(row=1, column=0, sticky="ew", padx=12)
        self.lbl_stats = ctk.CTkLabel(izq, text="", font=self.F(11), text_color=TEXTO,
                                      anchor="w")
        self.lbl_stats.grid(row=2, column=0, sticky="ew", padx=12, pady=(2, 4))

        self.seg = ctk.CTkSegmentedButton(
            izq, values=["todos", *COLOR_ESTADO], command=lambda _: self.aplicar_filtro(),
            font=self.F(11), fg_color=CAMPO, selected_color="#1d2638",
            selected_hover_color="#222c42", unselected_color=CAMPO,
            unselected_hover_color=HOVER, text_color=TEXTO, corner_radius=RADIO)
        self.seg.set("todos")
        self.seg.grid(row=3, column=0, sticky="ew", padx=10, pady=4)

        self.busca = ctk.CTkEntry(
            izq, placeholder_text="Buscar por nombre o teléfono", font=self.F(12),
            fg_color=CAMPO, border_color=BORDE, text_color=TEXTO, placeholder_text_color=GRIS,
            height=30, corner_radius=RADIO)
        self.busca.grid(row=4, column=0, sticky="ew", padx=10, pady=4)
        self.busca.bind("<KeyRelease>", lambda e: self.aplicar_filtro())

        marco = tk.Frame(izq, bg=PANEL)
        marco.grid(row=5, column=0, sticky="nsew", padx=10, pady=(4, 10))
        marco.grid_columnconfigure(0, weight=1)
        marco.grid_rowconfigure(0, weight=1)
        cols = ("id", "estado", "nombre", "tel", "web", "v")
        self.tree = ttk.Treeview(marco, columns=cols, show="headings", selectmode="browse")
        for c, t, w, anc in [("id", "ID", 46, "center"), ("estado", "ESTADO", 100, "w"),
                             ("nombre", "NOMBRE", 200, "w"), ("tel", "TELÉFONO", 112, "w"),
                             ("web", "WEB", 42, "center"), ("v", "A/B", 38, "center")]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=anc, stretch=(c == "nombre"),
                             minwidth=(150 if c == "nombre" else w))
        for e in COLOR_ESTADO:
            self.tree.tag_configure(e, foreground=DIM if e == "descartado" else
                                    (BLANCO if e == "nuevo" else TEXTO))
        sb = ttk.Scrollbar(marco, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self._al_seleccionar)
        self.tree.bind("<Delete>", lambda _e: self.eliminar_lead())

    # ---------- columna 2: detalle del lead ----------
    def _ui_detalle(self, main):
        der = ctk.CTkScrollableFrame(
            main, fg_color=PANEL, border_width=1, border_color=BORDE, corner_radius=RADIO,
            scrollbar_button_color=BORDE, scrollbar_button_hover_color=BORDE2)
        der.grid(row=0, column=2, sticky="nsew", padx=(6, 0))
        der.grid_columnconfigure(0, weight=1)

        self.lbl_nombre = ctk.CTkLabel(der, text="Selecciona un lead",
                                       font=self.F(17, True), text_color=BLANCO,
                                       wraplength=370, justify="left", anchor="w")
        self.lbl_nombre.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 0))
        linea = ctk.CTkFrame(der, fg_color="transparent")
        linea.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 4))
        linea.grid_columnconfigure(0, weight=1)
        self.lbl_sub = ctk.CTkLabel(linea, text="", font=self.F(10), text_color=GRIS, anchor="w")
        self.lbl_sub.grid(row=0, column=0, sticky="ew")
        self.lbl_estado = ctk.CTkLabel(linea, text="", font=self.F(12, True), anchor="e")
        self.lbl_estado.grid(row=0, column=1, sticky="e", padx=(6, 0))

        info = ctk.CTkFrame(der, fg_color=CAMPO, corner_radius=RADIO, border_width=1, border_color=BORDE)
        info.grid(row=2, column=0, sticky="ew", padx=10, pady=4)
        info.grid_columnconfigure(1, weight=1)
        self.val = {}
        for i, (clave, etiqueta) in enumerate([("tel", "Teléfono"), ("web", "Web"), ("red", "Red social"),
                                               ("email", "Correo"), ("maps", "Maps")]):
            ctk.CTkLabel(info, text=etiqueta, font=self.F(11), text_color=GRIS,
                         width=78, anchor="w").grid(row=i, column=0, padx=(12, 4), pady=3)
            v = ctk.CTkLabel(info, text="—", font=self.F(11), text_color=TEXTO,
                             anchor="w", wraplength=310, justify="left")
            v.grid(row=i, column=1, sticky="ew", pady=3)
            if clave in ("web", "red", "maps"):
                try:
                    v.configure(cursor="hand2")
                except Exception:
                    pass
                v.bind("<Button-1>", lambda e, k=clave: self._abrir_url(k))
            self.val[clave] = v

        self.lbl_hall = ctk.CTkLabel(der, text="", font=self.F(11), text_color=TEXTO,
                                     wraplength=370, justify="left", anchor="w")
        self.lbl_hall.grid(row=3, column=0, sticky="ew", padx=12, pady=4)

        acc = ctk.CTkFrame(der, fg_color="transparent")
        acc.grid(row=4, column=0, sticky="ew", padx=10, pady=4)
        self.btn(acc, "WhatsApp", self.abrir_whatsapp, ACENTO, True, 96).pack(side="left")
        self.btn(acc, "Copiar", self.copiar, GRIS, False, 72).pack(side="left", padx=4)
        self.btn(acc, "Guardar", self.guardar_edicion, GRIS, False, 80).pack(side="left")
        self.b_regen = self.btn(acc, "Regenerar", self.regenerar, GRIS, False, 92)
        self.b_regen.pack(side="left", padx=4)
        ctk.CTkCheckBox(der, text="marcar contactado al abrir WhatsApp",
                        variable=self.auto_marcar, font=self.F(11), text_color=GRIS,
                        fg_color=ACENTO, hover_color=ACENTO, border_color=BORDE2, corner_radius=4,
                        checkmark_color=BG, checkbox_width=16, checkbox_height=16
                        ).grid(row=5, column=0, sticky="w", padx=14, pady=2)

        est = ctk.CTkFrame(der, fg_color="transparent")
        est.grid(row=6, column=0, sticky="ew", padx=10, pady=(4, 2))
        for i, (txt, e) in enumerate(BOTONES_ESTADO):
            est.grid_columnconfigure(i % 3, weight=1)
            b = self.btn(est, txt, lambda e=e: self.poner_estado(e), COLOR_ESTADO[e], False, 100)
            b.grid(row=i // 3, column=i % 3, padx=3, pady=3, sticky="ew")

        self.tabs = ctk.CTkTabview(
            der, height=160, fg_color=CAMPO, border_width=1, border_color=BORDE, corner_radius=RADIO,
            segmented_button_fg_color=CAMPO, segmented_button_selected_color="#1d2638",
            segmented_button_selected_hover_color="#222c42",
            segmented_button_unselected_color=CAMPO,
            segmented_button_unselected_hover_color=HOVER, text_color=TEXTO)
        self.tabs.grid(row=7, column=0, sticky="ew", padx=10, pady=4)
        self.tb = {}
        for nombre in ("Mensaje", "Seguimiento"):
            t = self.tabs.add(nombre)
            t.grid_columnconfigure(0, weight=1)
            t.grid_rowconfigure(0, weight=1)
            box = ctk.CTkTextbox(t, height=100, fg_color=CAMPO, text_color=TEXTO,
                                 font=self.F(12), wrap="word", border_width=0)
            box.grid(row=0, column=0, sticky="nsew")
            self.tb[nombre] = box

        self.nota = ctk.CTkEntry(der, placeholder_text="Nota (se guarda con Guardar)",
                                 font=self.F(12), fg_color=CAMPO, border_color=BORDE,
                                 text_color=TEXTO, placeholder_text_color=GRIS, height=32,
                                 corner_radius=RADIO)
        self.nota.grid(row=8, column=0, sticky="ew", padx=12, pady=(6, 6))
        self.btn(der, "Eliminar lead", self.eliminar_lead, ROJO, False).grid(
            row=9, column=0, sticky="ew", padx=12, pady=(0, 12))

    # ---------------- datos ----------------
    def refrescar(self, silencioso=False):
        if not self.conn:
            self.msg("sin conexión a la base", ROJO)
            return
        try:
            self.filas = db.listar(self.conn, None, 5000)
            self.scrapes = db.listar_scrapes(self.conn)
        except psycopg2.Error as e:
            self.conn.rollback()
            self.msg(f"error de base: {e}", ROJO)
            return
        ids = {s["id"] for s in self.scrapes}
        if self.sec == AUTO or (self.sec is not None and self.sec not in ids):
            self.sec = self.scrapes[0]["id"] if self.scrapes else None
        self._pintar_secciones()
        self._cabecera_seccion()
        self.aplicar_filtro()
        self._stats()
        if self.vista_mapa:
            self.vista_mapa.actualizar()
        if not silencioso:
            self.msg(f"{len(self.filas)} leads en {len(self.scrapes)} secciones")

    def _filas_sec(self):
        if self.sec is None:
            return self.filas
        return [r for r in self.filas if r["scrape_id"] == self.sec]

    def _stats(self):
        fs = self._filas_sec()
        total = len(fs)
        nuevos = sum(1 for r in fs if r["estado"] == "nuevo")
        contact = sum(1 for r in fs if r["contactado_en"]
                      or r["estado"] in ("contactado", "respondio", "cotizado", "cerrado"))
        resp = sum(1 for r in fs if r["estado"] in ("respondio", "cotizado", "cerrado"))
        pct = f"{min(100, 100 * resp / contact):.0f}%" if contact else "—"
        self.lbl_stats.configure(
            text=f"{plural(total, 'lead', 'leads')}  ·  {plural(nuevos, 'nuevo', 'nuevos')}  ·  "
                 f"{plural(contact, 'contactado', 'contactados')}  ·  respuesta {pct}")

    def _cabecera_seccion(self):
        if self.sec is None:
            self.lbl_sec.configure(text="Todas las secciones")
            self.lbl_sec_sub.configure(
                text=f"{len(self.scrapes)} secciones · {len(self.filas)} leads")
            self.b_del_sec.configure(state="disabled")
            return
        s = next((x for x in self.scrapes if x["id"] == self.sec), None)
        if not s:
            return
        self.lbl_sec.configure(
            text=cortar(f"#{s['id']}  {(s['tipo'] or '?').capitalize()} · {s['lugar'] or '?'}", 44))
        self.lbl_sec_sub.configure(
            text=f"Maps devolvió {s['encontrados']} · guardados {s['total']} · "
                 f"{fecha_corta(s['creado_en'])} · {s['estado']}")
        self.b_del_sec.configure(state="normal")

    def aplicar_filtro(self):
        estado = self.seg.get()
        q = self.busca.get().strip().lower()
        visibles = []
        for r in self._filas_sec():
            if estado != "todos" and r["estado"] != estado:
                continue
            if q and q not in f"{r['nombre']} {r['telefono']}".lower():
                continue
            visibles.append(r)
        sig = tuple((r["id"], r["estado"], r["variante"], bool(r["web"])) for r in visibles)
        if sig != self._sig_tree:
            self._sig_tree = sig
            y0 = self.tree.yview()[0]
            self.tree.delete(*self.tree.get_children())
            for r in visibles:
                self.tree.insert(
                    "", "end", iid=str(r["id"]), tags=(r["estado"],),
                    values=(r["id"], r["estado"].capitalize(), r["nombre"], tel_corto(r["telefono"]),
                            "✓" if r["web"] else ("RS" if r["red_social"] else "—"),
                            r["variante"] or "-"))
            self.tree.yview_moveto(y0)
        if self.sel is not None:
            if self.tree.exists(str(self.sel)):
                if self.tree.selection() != (str(self.sel),):
                    self.tree.selection_set(str(self.sel))
            else:
                self._limpiar_detalle()

    def _fila(self, pid):
        return next((r for r in self.filas if r["id"] == pid), None)

    # ---------------- detalle ----------------
    def _al_seleccionar(self, _evt=None):
        s = self.tree.selection()
        if not s:
            return
        pid = int(s[0])
        if pid == self._mostrado and pid == self.sel:
            return                      # ya se está mostrando: no pisar lo que estés editando
        self.sel = pid
        r = self._fila(pid)
        if r:
            self.mostrar(r)

    def mostrar(self, r):
        self._mostrado = r["id"]
        self.lbl_nombre.configure(text=r["nombre"])
        self.lbl_sub.configure(text=f"{cortar(r['tipo'] or '?', 16)} · "
                                    f"{cortar(r['lugar'] or '?', 14)} · id {r['id']}")
        self.lbl_estado.configure(text=f"● {r['estado'].capitalize()}",
                                  text_color=COLOR_ESTADO[r["estado"]])
        self.urls = {"web": r["web"], "red": r["red_social"], "maps": r["maps_url"]}
        self.val["tel"].configure(text=f"+{r['telefono']}")
        self.val["web"].configure(text=r["web"] or "— sin página web —",
                                  text_color=TEAL if r["web"] else GRIS)
        self.val["red"].configure(text=r["red_social"] or "—",
                                  text_color=TEAL if r["red_social"] else GRIS)
        self.val["email"].configure(text=r["email"] or "—")
        self.val["maps"].configure(text="abrir en Google Maps" if r["maps_url"] else "—",
                                   text_color=TEAL if r["maps_url"] else GRIS)

        h = r["hallazgos"] or {}
        pts = auditoria.resumir(h)
        if pts:
            txt = "•  " + "\n•  ".join(pts)
        elif r["web"]:
            txt = "•  Sin hallazgos relevantes en su página"
        else:
            txt = "•  Sin página web: lead ideal para ofrecerle una"
        if h.get("stack"):
            txt += f"\n•  Stack: {h['stack']}"
        self.lbl_hall.configure(text=txt)

        for nombre, valor in (("Mensaje", r["mensaje"]), ("Seguimiento", r["mensaje_followup"])):
            self.tb[nombre].delete("1.0", "end")
            self.tb[nombre].insert("1.0", valor or "")
        self.tabs.set("Seguimiento" if r["mensaje_followup"] and r["estado"] == "contactado"
                      else "Mensaje")
        self.nota.delete(0, "end")
        self.nota.insert(0, r["notas"] or "")

    def _limpiar_detalle(self):
        self.sel = None
        self._mostrado = None
        self.lbl_nombre.configure(text="Selecciona un lead")
        self.lbl_sub.configure(text="")
        self.lbl_estado.configure(text="")
        for v in self.val.values():
            v.configure(text="—", text_color=GRIS)
        self.lbl_hall.configure(text="")
        for box in self.tb.values():
            box.delete("1.0", "end")
        self.nota.delete(0, "end")
        self.urls = {}

    def _forzar_detalle(self):
        r = self._fila(self.sel) if self.sel is not None else None
        if r:
            self.mostrar(r)

    def _abrir_url(self, clave):
        url = self.urls.get(clave)
        if url:
            webbrowser.open(url if url.startswith("http") else "https://" + url)

    # ---------------- acciones sobre un lead ----------------
    def _texto_activo(self):
        return self.tb[self.tabs.get()].get("1.0", "end").strip()

    def _requiere_sel(self):
        if self.sel is None or not self._fila(self.sel):
            self.msg("primero selecciona un lead", AMBAR)
            return None
        return self._fila(self.sel)

    def guardar_edicion(self):
        r = self._requiere_sel()
        if not r or not self.conn:
            return
        try:
            db.guardar_mensaje(self.conn, r["id"], r["variante"],
                               self.tb["Mensaje"].get("1.0", "end").strip())
            if self.tb["Seguimiento"].get("1.0", "end").strip() or r["mensaje_followup"]:
                db.guardar_followup(self.conn, r["id"],
                                    self.tb["Seguimiento"].get("1.0", "end").strip() or None)
            db.guardar_nota(self.conn, r["id"], self.nota.get().strip() or None)
        except psycopg2.Error as e:
            self.conn.rollback()
            self.msg(f"error al guardar: {e}", ROJO)
            return
        self.refrescar(silencioso=True)
        self.msg("cambios guardados")

    def abrir_whatsapp(self):
        r = self._requiere_sel()
        if not r:
            return
        texto = self._texto_activo()
        if not texto:
            self.msg("ese mensaje está vacío (usa Regenerar para generarlo)", AMBAR)
            return
        self.guardar_edicion()
        webbrowser.open(wame(r["telefono"], texto))
        if self.auto_marcar.get() and r["estado"] == "nuevo":
            self.poner_estado("contactado")

    def copiar(self):
        texto = self._texto_activo()
        if texto:
            self.clipboard_clear()
            self.clipboard_append(texto)
            self.msg("mensaje copiado al portapapeles")

    def poner_estado(self, estado):
        r = self._requiere_sel()
        if not r or not self.conn:
            return
        try:
            db.cambiar_estado(self.conn, r["id"], estado, self.nota.get().strip() or None)
        except psycopg2.Error as e:
            self.conn.rollback()
            self.msg(f"error: {e}", ROJO)
            return
        self.refrescar(silencioso=True)
        self._forzar_detalle()
        self.msg(f"{r['nombre']} → {estado}", COLOR_ESTADO[estado])

    def regenerar(self):
        r = self._requiere_sel()
        if r:
            fila = dict(r)
            self._lanzar(lambda conn, cl: scraping.regenerar_mensaje(conn, cl, fila),
                         "regenerando mensaje", "regen")

    # ---------------- eliminar ----------------
    def confirmar(self, titulo, texto, si="Eliminar"):
        """Diálogo modal. Devuelve True si el usuario confirma."""
        d = ctk.CTkToplevel(self)
        d.title(titulo)
        d.configure(fg_color=BG)
        d.transient(self)
        d.resizable(False, False)
        res = {"ok": False}

        def cerrar(v):
            res["ok"] = v
            d.destroy()

        marco = ctk.CTkFrame(d, fg_color=PANEL, border_width=1, border_color=BORDE2,
                             corner_radius=RADIO)
        marco.pack(padx=12, pady=12)
        ctk.CTkLabel(marco, text=titulo, font=self.F(15, True),
                     text_color=BLANCO).pack(anchor="w", padx=18, pady=(16, 6))
        ctk.CTkLabel(marco, text=texto, font=self.F(12), text_color=TEXTO, wraplength=430,
                     justify="left").pack(anchor="w", padx=16, pady=(2, 10))
        fila = ctk.CTkFrame(marco, fg_color="transparent")
        fila.pack(anchor="e", padx=16, pady=(0, 14))
        b_no = self.btn(fila, "Cancelar", lambda: cerrar(False), GRIS, False, 110)
        b_no.pack(side="left", padx=(0, 8))
        self.btn(fila, si, lambda: cerrar(True), ROJO, True, 120).pack(side="left")
        d.protocol("WM_DELETE_WINDOW", lambda: cerrar(False))
        d.bind("<Escape>", lambda _e: cerrar(False))
        d.bind("<Return>", lambda _e: cerrar(False))      # Enter = cancelar (lo seguro)
        d.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - d.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - d.winfo_reqheight()) // 3
        d.geometry(f"+{max(0, x)}+{max(0, y)}")
        try:
            d.wait_visibility()
            d.grab_set()
        except tk.TclError:
            pass
        d.focus_force()
        self.wait_window(d)
        return res["ok"]

    def eliminar_lead(self):
        r = self._requiere_sel()
        if not r or not self.conn:
            return
        txt = f"Se eliminará el lead «{r['nombre']}» (+{r['telefono']})."
        if r["estado"] != "nuevo":
            txt += f"\n\nEstá en estado «{r['estado']}»: se perderá su historial."
        txt += ("\n\nSi solo quieres no volver a contactarlo, mejor márcalo como descartado: "
                "así no reaparece cuando repitas esta búsqueda.")
        if not self.confirmar("Eliminar lead", txt):
            return
        try:
            db.borrar_prospecto(self.conn, r["id"])
        except psycopg2.Error as e:
            self.conn.rollback()
            self.msg(f"error al eliminar: {e}", ROJO)
            return
        self._limpiar_detalle()
        self.refrescar(silencioso=True)
        self.log(f"$ lead eliminado: {r['nombre']}", "cmd")
        self.msg(f"lead eliminado: {r['nombre']}", AMBAR)

    def eliminar_seccion(self):
        if self.sec is None:
            self.msg("elige una sección para eliminarla", AMBAR)
            return
        if self._ocupado():
            self.msg("hay un proceso corriendo: espera o usa Detener", AMBAR)
            return
        s = next((x for x in self.scrapes if x["id"] == self.sec), None)
        if not s or not self.conn:
            return
        fs = self._filas_sec()
        trabajados = sum(1 for r in fs if r["estado"] != "nuevo")
        txt = (f"Se eliminará la sección #{s['id']} «{s['tipo']} @ {s['lugar']}» "
               f"y sus {len(fs)} leads.")
        if trabajados:
            txt += (f"\n\nAtención: {trabajados} de ellos ya están contactados o con respuesta: "
                    "se perderá ese historial.")
        txt += "\n\nNo se puede deshacer."
        if not self.confirmar("Eliminar sección", txt):
            return
        restantes = [x["id"] for x in self.scrapes if x["id"] != s["id"]]
        try:
            n = db.borrar_scrape(self.conn, s["id"])
        except psycopg2.Error as e:
            self.conn.rollback()
            self.msg(f"error al eliminar: {e}", ROJO)
            return
        self._limpiar_detalle()
        # pasar a la sección vecina (la anterior en el tiempo, o la más reciente)
        self.sec = next((i for i in restantes if i < s["id"]), restantes[0] if restantes else None)
        self._guardar_estado()
        self.refrescar(silencioso=True)
        self.log(f"$ sección #{s['id']} eliminada ({n} leads)", "cmd")
        self.msg(f"sección #{s['id']} eliminada ({n} leads)", AMBAR)

    # ---------------- scraper (corre en un hilo) ----------------
    @staticmethod
    def _partir(texto):
        return [x.strip() for x in texto.split(",") if x.strip()]

    def iniciar_scrape(self):
        negocios = self._partir(self.e_tipo.get())
        lugares = self._partir(self.e_lugar.get())
        if not negocios or not lugares:
            self.msg("escribe al menos un tipo de negocio y una ciudad", AMBAR)
            return
        try:
            mx = max(1, min(int(self.e_max.get()), 300))
        except ValueError:
            self.msg("el máximo debe ser un número", AMBAR)
            return
        busquedas = [(n, l) for n in negocios for l in lugares]
        args = SimpleNamespace(max=mx, sin_auditoria=not self.v_audit.get(),
                               sin_pagespeed=not self.v_ps.get(),
                               debug=self.v_visible.get())
        self._guardar_estado()
        self.log(f"$ scrape {len(busquedas)} búsqueda(s), máx {mx} c/u", "cmd")
        self._lanzar(lambda conn, cl: scraping.run_busquedas(conn, cl, busquedas, args),
                     f"scrape ({len(busquedas)})", "scrape")

    def iniciar_followups(self):
        self.log("$ follow-ups", "cmd")
        self._lanzar(lambda conn, cl: scraping.seguimientos(conn, cl), "follow-ups", "followup")

    def _lanzar(self, fn, titulo, tipo="otro"):
        if self._ocupado():
            self.log("Aviso: ya hay un proceso corriendo (usa Detener)", "err")
            self.msg("ya hay un proceso corriendo", AMBAR)
            return
        scraping.CANCELAR.clear()
        scraping.log = lambda *a: self.cola.put(("log", " ".join(str(x) for x in a)))
        self._job = tipo
        self._ids_previos = {s["id"] for s in self.scrapes}
        self._set_corriendo(True, titulo)
        self.worker = threading.Thread(target=self._trabajo, args=(fn,), daemon=True)
        self.worker.start()

    def _trabajo(self, fn):
        """Corre en el hilo: usa su propia conexión a la base."""
        codigo, conn = 0, None
        try:
            conn = db.conectar()
            db.init_db(conn)
            fn(conn, scraping.crear_client())
        except scraping.Cancelado:
            self.cola.put(("log", "Aviso: proceso cancelado"))
            codigo = 1
        except Exception:
            self.cola.put(("log", "Error: " + traceback.format_exc()))
            codigo = 1
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            self.cola.put(("fin", codigo))

    def detener(self):
        if self._ocupado():
            scraping.CANCELAR.set()
            self.log("Aviso: cancelando… (termina el paso actual)", "err")
        else:
            self.msg("no hay nada corriendo")

    def _set_corriendo(self, on, titulo=""):
        estado = "disabled" if on else "normal"
        for b in (self.b_start, self.b_follow, self.b_regen):
            b.configure(state=estado)
        if on:
            self.prog.configure(mode="indeterminate")
            self.prog.start()
            self.lbl_run.configure(text=f"En curso · {titulo}", text_color=AMBAR)
            self.msg("proceso corriendo…", AMBAR)
        else:
            self.prog.stop()
            self.prog.configure(mode="determinate")
            self.prog.set(0)
            self.lbl_run.configure(text="Inactivo", text_color=DIM)

    def _poll(self):
        """Mientras corre un scrape, refresca para ver aparecer las secciones y leads."""
        if self._ocupado():
            self.refrescar(silencioso=True)
        self.after(3000, self._poll)

    # ---------------- consola ----------------
    def log(self, texto, tag=None):
        if tag is None:
            low = texto.lower()
            if "error" in low or "falló" in low or "aviso" in low or "traceback" in low:
                tag = "err"
            elif texto.startswith("$"):
                tag = "cmd"
            elif "terminado" in low or "listo" in low:
                tag = "ok"
        self.consola.configure(state="normal")
        self.consola.insert("end", texto + "\n", tag or ())
        if int(self.consola.index("end-1c").split(".")[0]) > 600:
            self.consola.delete("1.0", "100.0")
        self.consola.see("end")
        self.consola.configure(state="disabled")

    def _drenar_cola(self):
        try:
            while True:
                tipo, dato = self.cola.get_nowait()
                if tipo == "log":
                    self.log(dato)
                else:
                    self._set_corriendo(False)
                    self.log("[proceso terminado]" if dato == 0 else "[proceso terminó con errores]",
                             "ok" if dato == 0 else "err")
                    self.refrescar()
                    if self.vista_mapa:
                        self.vista_mapa.actualizar(force=True)
                    if self._job == "scrape":
                        nuevas = [s["id"] for s in self.scrapes if s["id"] not in self._ids_previos]
                        if nuevas:                       # saltar a la sección recién creada
                            self.seleccionar_seccion(max(nuevas))
                    elif self._job in ("regen", "followup"):
                        self._forzar_detalle()
        except queue.Empty:
            pass
        self.after(100, self._drenar_cola)

    # ---------------- detalles visuales ----------------
    def _banner_estado(self):
        db_ok = bool(self.conn)
        ia = bool(os.getenv("GROQ_API_KEY"))
        self.lbl_db.configure(text="● Base de datos" if db_ok else "● Sin base de datos",
                              text_color=OK if db_ok else ROJO)
        self.lbl_ia.configure(text="● IA · Groq" if ia else "● IA desactivada",
                              text_color=OK if ia else DIM)

    def _reloj(self):
        self.lbl_reloj.configure(text=time.strftime("%d %b %Y  ·  %H:%M"))
        self.after(20000, self._reloj)

    # ---------------- pantalla de arranque ----------------
    def _arranque(self):
        if SPLASH:
            self._splash()

    def _splash(self):
        sp = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        sp.place(x=0, y=0, relwidth=1, relheight=1)
        tk.Misc.lift(sp)
        self._sp = sp
        centro = ctk.CTkFrame(sp, fg_color="transparent")
        centro.place(relx=0.5, rely=0.44, anchor="center")
        ctk.CTkLabel(centro, text="SCRAPMAP", font=self.F(32, True), text_color=BLANCO).pack()
        ctk.CTkLabel(centro, text="Leads", font=self.F(13), text_color=GRIS).pack(pady=(0, 20))
        self._lbl_carga = ctk.CTkLabel(centro, text="Conectando con la base de datos…",
                                       font=self.F(11), text_color=GRIS)
        self._lbl_carga.pack()
        self._barra = ctk.CTkProgressBar(centro, width=280, height=2, corner_radius=0,
                                         fg_color=BORDE, progress_color=ACENTO)
        self._barra.pack(pady=(10, 0))
        self._barra.set(0)
        for w in (sp, centro):
            w.bind("<Button-1>", lambda _e: self._fin_splash())
        self._splash_paso(0)

    def _splash_paso(self, n):
        if self._sp is None:
            return
        try:
            if n >= 20:
                self._fin_splash()
                return
            self._barra.set(n / 20)
            self.after(45, lambda: self._splash_paso(n + 1))
        except tk.TclError:
            self._sp = None

    def _fin_splash(self):
        sp, self._sp = self._sp, None
        if sp is not None:
            sp.destroy()

    def _cerrar(self):
        scraping.CANCELAR.set()
        self._guardar_estado()
        if self.conn:
            self.conn.close()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
