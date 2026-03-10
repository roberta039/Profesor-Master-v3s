# ============================================================
# === MODUL DESENARE: Matplotlib / Mermaid / Plotly / ASCII ===
# ============================================================
#
# Înlocuiește complet SVG. AI-ul alege automat librăria
# pe baza tag-ului de deschidere al blocului de desen:
#
#   [[MATPLOTLIB]] ... cod Python matplotlib ... [[/MATPLOTLIB]]
#   [[MERMAID]]    ... sintaxă mermaid ...        [[/MERMAID]]
#   [[PLOTLY]]     ... cod Python plotly ...       [[/PLOTLY]]
#   [[ASCII]]      ... text ASCII art ...          [[/ASCII]]
#
# Detecție automată în render_message():
#   - grafice funcții / date matematice  → MATPLOTLIB
#   - diagrame flux / circuite / arbori  → MERMAID
#   - statistică / date interactive      → PLOTLY
#   - fallback / text simplu             → ASCII
# ============================================================

import re
import io
import base64
import traceback

import streamlit as st


# ── CSS pentru containere ──────────────────────────────────
DRAWING_CSS = """
<style>
.draw-container {
    background: #ffffff;
    border: 1px solid #e0e0e0;
    border-radius: 10px;
    padding: 16px;
    margin: 14px 0;
    box-shadow: 0 2px 8px rgba(0,0,0,0.08);
    overflow: auto;
}
[data-theme="dark"] .draw-container {
    background: #1e1e2e;
    border-color: #444;
    box-shadow: 0 2px 8px rgba(0,0,0,0.4);
}
.ascii-block {
    font-family: 'Courier New', Courier, monospace;
    font-size: 13px;
    line-height: 1.4;
    white-space: pre;
    background: #f8f8f8;
    border: 1px solid #ddd;
    border-radius: 8px;
    padding: 14px 18px;
    margin: 14px 0;
    overflow-x: auto;
    color: #222;
}
[data-theme="dark"] .ascii-block {
    background: #1a1a2e;
    border-color: #444;
    color: #e0e0e0;
}
.draw-label {
    font-size: 11px;
    color: #888;
    text-align: right;
    margin-top: 4px;
    font-style: italic;
}
</style>
"""

# ── Injectăm CSS o singură dată per sesiune ────────────────
def _inject_drawing_css():
    if not st.session_state.get("_drawing_css_injected"):
        st.markdown(DRAWING_CSS, unsafe_allow_html=True)
        st.session_state["_drawing_css_injected"] = True


# ════════════════════════════════════════════════════════════
# 1. MATPLOTLIB
# ════════════════════════════════════════════════════════════
def render_matplotlib(code: str, dark_mode: bool = False) -> bool:
    """Execută cod matplotlib și afișează figura ca imagine PNG inline."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np  # disponibil în codul generat

        # Stil adaptat temei
        if dark_mode:
            plt.style.use("dark_background")
            fig_facecolor = "#1e1e2e"
        else:
            plt.style.use("default")
            fig_facecolor = "white"

        # Namespace curat pentru exec
        ns = {
            "plt": plt,
            "np": np,
            "__builtins__": {
                "range": range, "len": len, "zip": zip, "enumerate": enumerate,
                "list": list, "dict": dict, "tuple": tuple, "set": set,
                "min": min, "max": max, "abs": abs, "round": round,
                "print": print, "str": str, "int": int, "float": float,
                "True": True, "False": False, "None": None,
            }
        }

        # Adaugă automat fig dacă codul nu creează explicit
        preamble = "fig, ax = plt.subplots(figsize=(8, 5))\n" \
                   if "plt.subplots" not in code and "plt.figure" not in code else ""

        exec(preamble + code, ns)  # noqa: S102

        # Setează background după execuție
        fig = plt.gcf()
        fig.patch.set_facecolor(fig_facecolor)

        # Exportă în buffer PNG
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                    facecolor=fig_facecolor)
        plt.close("all")
        buf.seek(0)

        img_b64 = base64.b64encode(buf.read()).decode()
        st.markdown(
            f'<div class="draw-container">'
            f'<img src="data:image/png;base64,{img_b64}" '
            f'style="max-width:100%;height:auto;display:block;margin:auto"/>'
            f'<div class="draw-label">📊 Grafic Matplotlib</div>'
            f'</div>',
            unsafe_allow_html=True
        )
        return True

    except Exception as e:
        st.warning(f"⚠️ Eroare la grafic Matplotlib: {e}")
        return False


# ════════════════════════════════════════════════════════════
# 2. MERMAID
# ════════════════════════════════════════════════════════════

# Template HTML care încarcă Mermaid.js din CDN și randează diagrama
_MERMAID_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<style>
  body {{ margin:0; padding:10px; background:{bg}; font-family: sans-serif; }}
  .mermaid {{ max-width:100%; }}
  svg {{ max-width:100%; height:auto; }}
</style>
</head>
<body>
<div class="mermaid">
{code}
</div>
<script>
  mermaid.initialize({{
    startOnLoad: true,
    theme: '{theme}',
    securityLevel: 'loose',
    flowchart: {{ useMaxWidth: true, htmlLabels: true }},
    er: {{ useMaxWidth: true }},
    sequence: {{ useMaxWidth: true }},
  }});
</script>
</body>
</html>
"""

def render_mermaid(code: str, dark_mode: bool = False) -> bool:
    """Randează o diagramă Mermaid.js într-un iframe HTML."""
    try:
        import streamlit.components.v1 as components

        theme  = "dark"   if dark_mode else "default"
        bg     = "#1e1e2e" if dark_mode else "#ffffff"

        html = _MERMAID_HTML.format(code=code.strip(), theme=theme, bg=bg)

        # Estimăm înălțimea după numărul de linii
        lines     = code.strip().count("\n") + 1
        est_height = max(200, min(lines * 42 + 80, 700))

        st.markdown('<div class="draw-label" style="margin-bottom:4px">📐 Diagramă Mermaid</div>',
                    unsafe_allow_html=True)
        components.html(html, height=est_height, scrolling=True)
        return True

    except Exception as e:
        st.warning(f"⚠️ Eroare la diagrama Mermaid: {e}")
        return False


# ════════════════════════════════════════════════════════════
# 3. PLOTLY
# ════════════════════════════════════════════════════════════
def render_plotly(code: str, dark_mode: bool = False) -> bool:
    """Execută cod Plotly și afișează figura interactivă."""
    try:
        import plotly.express as px
        import plotly.graph_objects as go
        import numpy as np
        import pandas as pd

        template = "plotly_dark" if dark_mode else "plotly_white"

        ns = {
            "px": px, "go": go, "np": np, "pd": pd,
            "template": template,
            "__builtins__": {
                "range": range, "len": len, "zip": zip, "list": list,
                "dict": dict, "print": print, "str": str,
                "int": int, "float": float, "True": True, "False": False, "None": None,
            }
        }

        exec(code, ns)  # noqa: S102

        # Caută variabila `fig` în namespace
        fig = ns.get("fig")
        if fig is None:
            # Încearcă ultimul obiect de tip Figure creat
            for val in reversed(list(ns.values())):
                if hasattr(val, "update_layout"):
                    fig = val
                    break

        if fig is None:
            st.warning("⚠️ Codul Plotly nu a creat o variabilă `fig`.")
            return False

        fig.update_layout(template=template, margin=dict(l=20, r=20, t=40, b=20))
        st.markdown('<div class="draw-label">📈 Grafic Plotly interactiv</div>',
                    unsafe_allow_html=True)
        st.plotly_chart(fig, use_container_width=True)
        return True

    except Exception as e:
        st.warning(f"⚠️ Eroare la grafic Plotly: {e}")
        return False


# ════════════════════════════════════════════════════════════
# 4. ASCII ART (fallback)
# ════════════════════════════════════════════════════════════
def render_ascii(content: str) -> bool:
    """Afișează ASCII art / text preformatat într-un bloc stilizat."""
    if not content.strip():
        return False
    st.markdown(
        f'<div class="ascii-block">{content}</div>'
        f'<div class="draw-label">📝 Diagramă text</div>',
        unsafe_allow_html=True
    )
    return True


# ════════════════════════════════════════════════════════════
# PARSER: extrage toate blocurile de desen din text
# ════════════════════════════════════════════════════════════

# Mapare tag → (renderer, label_emoji)
_DRAW_TAGS = {
    "MATPLOTLIB": (render_matplotlib, "📊"),
    "MERMAID":    (render_mermaid,    "📐"),
    "PLOTLY":     (render_plotly,     "📈"),
    "ASCII":      (render_ascii,      "📝"),
}

# Pattern care găsește oricare bloc [[TAG]]...[[/TAG]]
_BLOCK_RE = re.compile(
    r'\[\[(MATPLOTLIB|MERMAID|PLOTLY|ASCII)\]\](.*?)\[\[/\1\]\]',
    re.DOTALL | re.IGNORECASE
)

# Curăță blocurile de desen din text pentru TTS
_CLEAN_RE = re.compile(
    r'\[\[(MATPLOTLIB|MERMAID|PLOTLY|ASCII)\]\].*?\[\[/\1\]\]',
    re.DOTALL | re.IGNORECASE
)

def clean_drawing_blocks_for_audio(text: str) -> str:
    """Elimină toate blocurile de desen din text înainte de TTS."""
    return _CLEAN_RE.sub(' Am pregătit un grafic pentru tine. ', text)


def render_message(content: str, dark_mode: bool = False):
    """
    Renderer principal: afișează textul normal și randează
    toate blocurile de desen găsite în ordinea lor.

    Înlocuiește complet render_message_with_svg().
    """
    _inject_drawing_css()

    if not _BLOCK_RE.search(content):
        # Niciun bloc de desen — afișează markdown direct
        st.markdown(content)
        return

    last_end = 0
    for match in _BLOCK_RE.finditer(content):
        tag      = match.group(1).upper()
        code     = match.group(2)
        start    = match.start()
        end      = match.end()

        # Text înainte de bloc
        before = content[last_end:start].strip()
        if before:
            st.markdown(before)

        # Randează blocul
        renderer, _ = _DRAW_TAGS[tag]
        if tag == "ASCII":
            renderer(code)
        else:
            renderer(code, dark_mode=dark_mode)

        last_end = end

    # Text după ultimul bloc
    after = content[last_end:].strip()
    if after:
        st.markdown(after)


# ════════════════════════════════════════════════════════════
# SYSTEM PROMPT — instrucțiuni pentru AI
# ════════════════════════════════════════════════════════════
DRAWING_SYSTEM_PROMPT = """
═══════════════════════════════════════════════════════
SISTEM DE DESENARE — REGULI OBLIGATORII
═══════════════════════════════════════════════════════
Când elevul cere un grafic, diagramă, schemă sau vizualizare,
alege AUTOMAT librăria potrivită și folosește tag-urile corecte:

━━━ 1. MATPLOTLIB — grafice matematice ━━━
Folosit pentru: grafice de funcții, reprezentări geometrice,
histograme, grafice fizică (v-t, s-t, F-x), chimie (concentrație),
orice grafic cu axe numerice.

[[MATPLOTLIB]]
import numpy as np
# fig, ax deja create automat — folosește direct ax
x = np.linspace(-5, 5, 400)
y = x**2 - 3*x + 2
ax.plot(x, y, 'b-', linewidth=2, label='f(x) = x² - 3x + 2')
ax.axhline(0, color='k', linewidth=0.8)
ax.axvline(0, color='k', linewidth=0.8)
ax.set_xlabel('x')
ax.set_ylabel('f(x)')
ax.set_title('Graficul funcției f(x) = x² - 3x + 2')
ax.legend()
ax.grid(True, alpha=0.3)
[[/MATPLOTLIB]]

REGULI Matplotlib:
- ÎNTOTDEAUNA folosește `ax.` (nu `plt.` direct) pentru plot, set_xlabel etc.
- `fig` și `ax` sunt create automat — NU apela plt.subplots() sau plt.figure()
- Excepție: plt.subplots(1,2) pentru subplots multiple — atunci creează tu fig,ax
- Adaugă ÎNTOTDEAUNA: titlu (set_title), etichete axe (set_xlabel, set_ylabel), grid
- Marchează puncte speciale: zerouri, extreme, intersecții cu ax.scatter() sau ax.annotate()
- Folosește culori clare: 'b' albastru, 'r' roșu, 'g' verde, 'orange', 'purple'
- Pentru mai multe funcții: plot separat cu label diferit, adaugă ax.legend()
- Trigonometrie: etichetează valorile speciale (π/2, π, etc.) pe axa x
- Derivate: trasează și funcția și derivata sa cu culori diferite
- Geometrie analitică: setează ax.set_aspect('equal') pentru proporții corecte

━━━ 2. MERMAID — diagrame și scheme ━━━
Folosit pentru: scheme logice, diagrame flux, circuite electrice (simple),
arbori genealogici, relații biologice, cronologii, organigrame, ERD.

[[MERMAID]]
flowchart TD
    A[Start] --> B{Δ = b²-4ac}
    B -->|Δ > 0| C[Două soluții reale]
    B -->|Δ = 0| D[O soluție dublă]
    B -->|Δ < 0| E[Fără soluții reale]
    C --> F[x₁,₂ = -b ± √Δ / 2a]
[[/MERMAID]]

Tipuri Mermaid disponibile:
- `flowchart TD` / `LR` — diagrame flux (top-down / left-right)
- `sequenceDiagram` — secvențe (biologie: sinteză proteică, reacții)
- `classDiagram` — clase (informatică OOP)
- `erDiagram` — entitate-relație (baze de date)
- `timeline` — cronologii (istorie)
- `mindmap` — hărți mentale (recapitulări)
- `graph` — grafuri generale

REGULI Mermaid:
- Nodurile cu text lung: folosește ghilimele "text lung"
- Săgeți: --> (normal), ==> (gros), -.-> (punctat), -->|text| (cu etichetă)
- Forme: [dreptunghi], (rotunjit), {romb/decizie}, ((cerc)), >paralelogram]
- Nu folosi caractere speciale în noduri fără ghilimele (ă,â,î,ș,ț => scrie fara diacritice sau pune in ghilimele)
- Testează că sintaxa e validă — evită noduri nedefinite

━━━ 3. PLOTLY — date interactive ━━━
Folosit pentru: statistică descriptivă, comparații de date,
distribuții, date reale din probleme, grafice cu mai multe serii.

[[PLOTLY]]
import plotly.express as px
import pandas as pd
df = pd.DataFrame({
    'Materie': ['Mate', 'Fizică', 'Chimie', 'Bio', 'Info'],
    'Note': [8.5, 7.2, 9.1, 6.8, 9.5]
})
fig = px.bar(df, x='Materie', y='Note', color='Note',
             color_continuous_scale='Blues',
             title='Note pe materii',
             template=template)
fig.update_layout(showlegend=False)
[[/PLOTLY]]

REGULI Plotly:
- Variabila `template` e disponibilă automat (light/dark adaptat)
- ÎNTOTDEAUNA salvezi figura în variabila `fig`
- Folosește plotly.express (px) pentru simplicitate, go pentru control fin
- Adaugă titlu și etichete axe

━━━ 4. ASCII — fallback text ━━━
Folosit DOAR când celelalte nu se potrivesc: scheme foarte simple,
tabele de valori, structuri de date (stivă, coadă), arbori simpli.

[[ASCII]]
    Stivă (Stack) — LIFO
    ┌─────────┐
    │  "top"  │  ← push / pop
    ├─────────┤
    │  "B"    │
    ├─────────┤
    │  "A"    │  ← bottom
    └─────────┘
[[/ASCII]]

━━━ CÂND SĂ FOLOSEȘTI FIECARE ━━━
| Cerere elevului                    | Librărie    |
|------------------------------------|-------------|
| "Desenează graficul lui f(x)"      | MATPLOTLIB  |
| "Arată-mi cum variază v în timp"   | MATPLOTLIB  |
| "Fă o schemă a algoritmului"       | MERMAID     |
| "Diagrama circuitului electric"    | MERMAID     |
| "Arborele genealogic al familiei"  | MERMAID     |
| "Grafic cu notele clasei"          | PLOTLY      |
| "Compară datele din tabel"         | PLOTLY      |
| "Schema structurii de date"        | ASCII       |
| "Tabel de valori simplu"           | ASCII       |

IMPORTANT: Generează cod CORECT și complet. Nu lăsa variabile nedefinite.
Testează mental codul înainte să îl scrii.
═══════════════════════════════════════════════════════
"""
