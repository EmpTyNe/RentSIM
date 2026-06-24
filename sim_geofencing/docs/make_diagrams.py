"""Генерация диаграмм для пояснительной записки.

Рисуем 4 диаграммы и сохраняем в PNG (300 dpi, удобно вставлять в Word) и в
общий PDF: архитектура, ER-диаграмма БД, диаграмма состояний самоката,
диаграмма последовательности обработки телеметрии.
"""
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch
from matplotlib.backends.backend_pdf import PdfPages

OUT = os.environ.get("OUT", "diagrams")
os.makedirs(OUT, exist_ok=True)

INK = "#1b2430"
MUTED = "#5b6b7d"
GREEN, BLUE, AMBER, RED, VIOLET = "#0bbf8e", "#2e86de", "#e0922f", "#e0496a", "#8c5bd0"
plt.rcParams["font.family"] = "DejaVu Sans"


def box(ax, x, y, w, h, text, fc="#ffffff", ec=INK, fs=11, bold=False, tc=INK):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.08",
                                linewidth=1.6, edgecolor=ec, facecolor=fc, mutation_aspect=1))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=fs,
            color=tc, fontweight="bold" if bold else "normal", wrap=True)


def arrow(ax, p1, p2, text="", color=MUTED, fs=9, rad=0.0, ls="-"):
    ax.add_patch(FancyArrowPatch(p1, p2, arrowstyle="-|>", mutation_scale=14,
                                 linewidth=1.4, color=color, linestyle=ls,
                                 connectionstyle=f"arc3,rad={rad}"))
    if text:
        mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
        ax.text(mx, my + 0.12, text, ha="center", va="bottom", fontsize=fs, color=color)


def new_ax(title, w=12, h=7.2):
    fig, ax = plt.subplots(figsize=(w, h))
    ax.set_xlim(0, 12); ax.set_ylim(0, h); ax.axis("off")
    ax.text(0.1, h - 0.3, title, fontsize=15, fontweight="bold", color=INK)
    return fig, ax


# 1) Архитектура
def diagram_architecture():
    fig, ax = new_ax("Рис. 1 — Архитектура системы")
    box(ax, 0.4, 3.0, 2.4, 1.1, "Эмулятор СИМ\n(emulator)", fc="#eef4fb", ec=BLUE, bold=True)
    box(ax, 4.2, 5.4, 3.6, 1.0, "MQTT-брокер (Mosquitto)", fc="#f3eefb", ec=VIOLET, bold=True)
    box(ax, 3.7, 1.3, 4.6, 3.4, "", fc="#fbfdff", ec=INK)
    ax.text(6.0, 4.4, "Бэкенд (FastAPI)", ha="center", fontsize=12, fontweight="bold", color=INK)
    box(ax, 3.95, 3.35, 1.95, 0.7, "REST API", fc="#eafaf4", ec=GREEN, fs=9)
    box(ax, 6.05, 3.35, 2.1, 0.7, "MQTT-мост", fc="#eafaf4", ec=GREEN, fs=9)
    box(ax, 3.95, 2.5, 1.95, 0.7, "Движок\nсимуляции", fc="#eafaf4", ec=GREEN, fs=9)
    box(ax, 6.05, 2.5, 2.1, 0.7, "Геозоны\n(PostGIS-запросы)", fc="#eafaf4", ec=GREEN, fs=9)
    box(ax, 4.95, 1.55, 2.1, 0.7, "WebSocket", fc="#eafaf4", ec=GREEN, fs=9)
    box(ax, 9.3, 5.2, 2.4, 1.2, "Браузер:\nпанель оператора\n(Leaflet, Chart.js)", fc="#eef4fb", ec=BLUE, bold=True)
    box(ax, 9.3, 1.5, 2.4, 1.2, "PostgreSQL\n+ PostGIS", fc="#fbeef0", ec=RED, bold=True)

    arrow(ax, (2.8, 3.9), (4.2, 5.5), "телеметрия", BLUE, rad=0.15)
    arrow(ax, (4.2, 5.6), (2.8, 3.7), "команды", VIOLET, rad=0.15)
    arrow(ax, (6.0, 5.4), (6.0, 4.05), "")
    arrow(ax, (8.3, 3.0), (9.3, 2.0), "SQL / гео", MUTED, rad=0.1)
    arrow(ax, (9.3, 5.6), (8.3, 3.9), "REST + WebSocket", BLUE, rad=-0.2)
    fig.tight_layout()
    return fig


# 2) ER-диаграмма
def diagram_er():
    fig, ax = new_ax("Рис. 2 — Схема базы данных (ER)", h=9.2)

    def entity(x, y, title, rows, ec):
        h = 0.5 + 0.32 * len(rows)
        box(ax, x, y - h, 3.0, h, "", fc="#ffffff", ec=ec)
        ax.add_patch(FancyBboxPatch((x, y - 0.5), 3.0, 0.5, boxstyle="round,pad=0.02,rounding_size=0.06",
                                    linewidth=0, facecolor=ec))
        ax.text(x + 1.5, y - 0.25, title, ha="center", va="center", color="white", fontweight="bold", fontsize=11)
        for i, r in enumerate(rows):
            ax.text(x + 0.15, y - 0.78 - i * 0.32, r, ha="left", va="center", fontsize=8.5, color=INK)

    entity(0.5, 8.7, "users", ["PK id", "email", "balance"], VIOLET)
    entity(8.5, 8.7, "devices", ["PK id", "code", "device_type", "status", "battery", "speed_limit", "geom : POINT"], BLUE)
    entity(0.5, 4.6, "zones", ["PK id", "name", "zone_type", "speed_limit", "geom : POLYGON"], AMBER)
    entity(4.5, 5.4, "trips",
           ["PK id", "FK user_id", "FK device_id", "started_at", "ended_at", "outcome", "distance_m", "cost", "path"], GREEN)

    arrow(ax, (2.0, 7.0), (4.6, 5.2), "1 .. *", MUTED, rad=-0.1)
    arrow(ax, (8.6, 6.5), (6.7, 5.2), "1 .. *", MUTED, rad=0.15)
    ax.text(6.0, 0.5, "zones — независимая таблица геозон (используется геозапросами PostGIS)",
            fontsize=9, color=MUTED, ha="center")
    fig.tight_layout()
    return fig


# 3) Диаграмма состояний
def diagram_states():
    fig, ax = new_ax("Рис. 3 — Жизненный цикл самоката (диаграмма состояний)")
    st = {
        "available": (5.0, 5.6, GREEN, "available\n(на парковке)"),
        "in_use": (9.0, 5.6, BLUE, "in_use\n(аренда, едет)"),
        "fault": (9.0, 2.6, RED, "fault\n(поломка)"),
        "maintenance": (5.0, 2.6, VIOLET, "maintenance\n(ремонт)"),
        "charging": (1.2, 4.1, AMBER, "charging\n(зарядка)"),
    }
    for k, (x, y, c, label) in st.items():
        box(ax, x, y, 2.2, 0.95, label, fc="#ffffff", ec=c, bold=True, tc=INK)

    def c(k, side):
        x, y, _, _ = st[k]
        return {"r": (x + 2.2, y + 0.47), "l": (x, y + 0.47),
                "t": (x + 1.1, y + 0.95), "b": (x + 1.1, y)}[side]

    arrow(ax, c("available", "r"), c("in_use", "l"), "через случ. время", GREEN)
    arrow(ax, c("in_use", "b"), c("fault", "t"), "поломка (шанс p)", RED)
    arrow(ax, (9.0, 5.6), (7.2, 5.85), "поездка завершена", BLUE, rad=0.2)
    arrow(ax, c("fault", "l"), c("maintenance", "r"), "оператор: ремонт", VIOLET)
    arrow(ax, c("maintenance", "t"), c("available", "b"), "кулдаун → парковка", VIOLET)
    arrow(ax, c("available", "l"), c("charging", "t"), "оператор: зарядка", AMBER, rad=0.2)
    arrow(ax, c("charging", "r"), c("available", "b"), "кулдаун → парковка, 100%", AMBER, rad=-0.28)
    ax.text(6.0, 0.7, "Въезд в запретную зону во время in_use → поездка прекращается, эвакуация на парковку (→ available)",
            fontsize=9, color=MUTED, ha="center")
    fig.tight_layout()
    return fig


# 4) Диаграмма последовательности
def diagram_sequence():
    fig, ax = new_ax("Рис. 4 — Обработка телеметрии (диаграмма последовательности)")
    actors = [("Эмулятор", 1.5, BLUE), ("MQTT", 4.0, VIOLET),
              ("Бэкенд", 6.5, GREEN), ("БД/PostGIS", 9.0, RED), ("Браузер", 11.0, BLUE)]
    top, bot = 6.0, 1.0
    for name, x, col in actors:
        box(ax, x - 0.9, top, 1.8, 0.6, name, fc="#ffffff", ec=col, bold=True, fs=10)
        ax.plot([x, x], [top, bot], color="#c5cfda", linewidth=1.2, linestyle="--")

    X = {a[0]: a[1] for a in actors}
    msgs = [
        ("Эмулятор", "MQTT", "telemetry(lat,lng,battery)", 5.5, BLUE),
        ("MQTT", "Бэкенд", "публикация телеметрии", 5.0, VIOLET),
        ("Бэкенд", "БД/PostGIS", "геозоны + сохранение позиции", 4.5, GREEN),
        ("Бэкенд", "Браузер", "WebSocket: статус/позиция", 3.9, GREEN),
        ("Бэкенд", "MQTT", "command(lock / set_speed_limit)", 3.3, GREEN),
        ("MQTT", "Эмулятор", "доставка команды", 2.7, VIOLET),
    ]
    for a, b, text, y, col in msgs:
        x1, x2 = X[a], X[b]
        arrow(ax, (x1, y), (x2, y), "", col)
        ax.text((x1 + x2) / 2, y + 0.12, text, ha="center", va="bottom", fontsize=8.5, color=INK)
    fig.tight_layout()
    return fig


builders = [diagram_architecture, diagram_er, diagram_states, diagram_sequence]
names = ["architecture", "er-diagram", "lifecycle-states", "sequence-telemetry"]

with PdfPages(os.path.join(OUT, "diagrams.pdf")) as pdf:
    for build, name in zip(builders, names):
        fig = build()
        fig.savefig(os.path.join(OUT, f"{name}.png"), dpi=300, bbox_inches="tight", facecolor="white")
        pdf.savefig(fig, facecolor="white")
        plt.close(fig)

print("Диаграммы сохранены в", OUT)
