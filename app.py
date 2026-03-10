import streamlit as st
from google import genai
from google.genai import types as genai_types
import edge_tts
import asyncio
from io import BytesIO
from supabase import create_client, Client
import uuid
import time
import tempfile
import ast
import re


# === APP INSTANCE ID ===
def get_app_id() -> str:
    try:
        return str(st.secrets.get("APP_INSTANCE_ID", "default")).strip() or "default"
    except Exception:
        return "default"


# === CONSTANTE PENTRU LIMITE ===
MAX_MESSAGES_IN_MEMORY = 100
MAX_MESSAGES_TO_SEND_TO_AI = 20
MAX_MESSAGES_IN_DB_PER_SESSION = 500
CLEANUP_DAYS_OLD = 7


# === ISTORIC CONVERSAȚII ===
def get_session_list(limit: int = 20) -> list[dict]:
    cache_ts  = st.session_state.get("_sess_list_ts", 0)
    cache_val = st.session_state.get("_sess_list_cache", None)
    force_refresh = st.session_state.pop("_sess_cache_dirty", False)

    if not force_refresh and cache_val is not None and (time.time() - cache_ts) < 30:
        return cache_val

    try:
        supabase = get_supabase_client()
        resp = (
            supabase.table("sessions")
            .select("session_id, last_active")
            .eq("app_id", get_app_id())
            .order("last_active", desc=True)
            .limit(limit)
            .execute()
        )
        sessions = resp.data or []
        if not sessions:
            return []

        session_ids = [s["session_id"] for s in sessions]
        hist_resp = (
            supabase.table("history")
            .select("session_id, role, content, timestamp")
            .in_("session_id", session_ids)
            .eq("role", "user")
            .order("timestamp", desc=False)
            .execute()
        )
        hist_rows = hist_resp.data or []

        first_msg: dict[str, str] = {}
        msg_count: dict[str, int] = {}
        for row in hist_rows:
            sid = row["session_id"]
            msg_count[sid] = msg_count.get(sid, 0) + 1
            if sid not in first_msg:
                txt = row["content"][:60]
                first_msg[sid] = txt + ("..." if len(row["content"]) > 60 else "")

        result = []
        for s in sessions:
            sid = s["session_id"]
            cnt = msg_count.get(sid, 0)
            if cnt > 0:
                result.append({
                    "session_id": sid,
                    "last_active": s["last_active"],
                    "preview": first_msg.get(sid, "Conversație nouă"),
                    "msg_count": cnt,
                })

        st.session_state["_sess_list_cache"] = result
        st.session_state["_sess_list_ts"]    = time.time()
        return result

    except Exception as e:
        _log("Eroare la încărcarea sesiunilor", "silent", e)
        return cache_val or []


def switch_session(new_session_id: str):
    st.session_state.session_id = new_session_id
    st.session_state.messages = []
    st.query_params["sid"] = new_session_id
    invalidate_session_cache()
    inject_session_js()


def invalidate_session_cache():
    st.session_state["_sess_cache_dirty"] = True


def format_time_ago(timestamp) -> str:
    if isinstance(timestamp, str):
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            timestamp = dt.timestamp()
        except Exception:
            return "necunoscut"
    try:
        diff = time.time() - float(timestamp)
    except (TypeError, ValueError):
        return "necunoscut"
    if diff < 60:
        return "acum"
    elif diff < 3600:
        mins = int(diff / 60)
        return f"{mins} min în urmă"
    elif diff < 86400:
        hours = int(diff / 3600)
        return f"{hours}h în urmă"
    else:
        days = int(diff / 86400)
        return f"{days} zile în urmă"


# === SUPABASE CLIENT ===
@st.cache_resource
def get_supabase_client() -> Client | None:
    try:
        url = st.secrets.get("SUPABASE_URL", "")
        key = st.secrets.get("SUPABASE_KEY", "")
        if not url or not key:
            return None
        return create_client(url, key)
    except Exception:
        return None


def is_supabase_available() -> bool:
    return st.session_state.get("_sb_online", True)


def _mark_supabase_offline():
    was_online = st.session_state.get("_sb_online", True)
    st.session_state["_sb_online"] = False
    if was_online:
        st.toast("⚠️ Baza de date offline — modul local activat.", icon="📴")


def _mark_supabase_online():
    was_offline = not st.session_state.get("_sb_online", True)
    st.session_state["_sb_online"] = True
    if was_offline:
        st.toast("✅ Conexiunea restabilită!", icon="🟢")
        _flush_offline_queue()


def _get_offline_queue() -> list:
    return st.session_state.setdefault("_offline_queue", [])


def _flush_offline_queue():
    queue = _get_offline_queue()
    if not queue:
        return
    client = get_supabase_client()
    if not client:
        return
    failed = []
    for item in queue:
        try:
            client.table("history").insert(item).execute()
        except Exception:
            failed.append(item)
    st.session_state["_offline_queue"] = failed
    if not failed:
        st.toast(f"✅ {len(queue)} mesaje sincronizate cu baza de date.", icon="☁️")


# === VOCI EDGE TTS ===
VOICE_MALE_RO = "ro-RO-EmilNeural"
VOICE_FEMALE_RO = "ro-RO-AlinaNeural"


st.set_page_config(
    page_title="Profesor Liceu",
    page_icon="🎓",
    layout="wide",
    initial_sidebar_state="expanded"
)

if st.session_state.get("dark_mode", False):
    st.markdown("""
    <style>
        :root { color-scheme: dark; }
        .stApp, [data-testid="stAppViewContainer"] {
            background-color: #0e1117 !important;
            color: #fafafa !important;
        }
        [data-testid="stSidebar"] { background-color: #161b22 !important; }
        .stChatMessage { background-color: #1a1f2e !important; }
        .stTextArea textarea, .stTextInput input {
            background-color: #1a1f2e !important;
            color: #fafafa !important;
            border-color: #444 !important;
        }
        .stSelectbox > div, .stRadio > div {
            background-color: #1a1f2e !important;
            color: #fafafa !important;
        }
        p, h1, h2, h3, h4, h5, h6, li, label, span { color: #fafafa !important; }
        .stButton > button { border-color: #555 !important; }
        hr { border-color: #333 !important; }
        .stExpander { border-color: #333 !important; }
        [data-testid="stChatInput"] { background-color: #1a1f2e !important; }
    </style>
    """, unsafe_allow_html=True)

st.markdown("""
<style>
    .stChatMessage { font-size: 16px; }
    footer { visibility: hidden; }
    /* Drawing containers handled by drawing module CSS */
    .typing-indicator {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 10px 4px;
        font-size: 14px;
        color: #888;
    }
    .typing-dots { display: flex; gap: 4px; }
    .typing-dots span {
        width: 7px;
        height: 7px;
        border-radius: 50%;
        background: #888;
        animation: typing-bounce 1.2s infinite ease-in-out;
    }
    .typing-dots span:nth-child(1) { animation-delay: 0s; }
    .typing-dots span:nth-child(2) { animation-delay: 0.2s; }
    .typing-dots span:nth-child(3) { animation-delay: 0.4s; }
    @keyframes typing-bounce {
        0%, 80%, 100% { transform: scale(0.7); opacity: 0.4; }
        40%            { transform: scale(1.0); opacity: 1.0; }
    }
</style>
""", unsafe_allow_html=True)


# === DATABASE FUNCTIONS ===
def _log(msg: str, level: str = "silent", exc: Exception = None):
    full_msg = f"{msg}: {exc}" if exc else msg
    print(full_msg)
    icon_map = {"info": "ℹ️", "warning": "⚠️", "error": "❌"}
    if level in icon_map:
        try:
            st.toast(msg, icon=icon_map[level])
        except Exception:
            pass


def init_db():
    online = is_supabase_available()
    if not online:
        st.warning("📴 **Modul offline activ** — conversația se păstrează în memorie.", icon="⚠️")


def cleanup_old_sessions(days_old: int = CLEANUP_DAYS_OLD):
    if time.time() - st.session_state.get("_last_cleanup", 0) < 86400:
        return
    st.session_state["_last_cleanup"] = time.time()
    try:
        supabase = get_supabase_client()
        cutoff_time = time.time() - (days_old * 24 * 60 * 60)
        supabase.table("history").delete().lt("timestamp", cutoff_time).eq("app_id", get_app_id()).execute()
        supabase.table("sessions").delete().lt("last_active", cutoff_time).eq("app_id", get_app_id()).execute()
    except Exception as e:
        _log("Eroare la curățarea sesiunilor vechi", "silent", e)


def save_message_to_db(session_id, role, content):
    record = {
        "session_id": session_id,
        "role": role,
        "content": content,
        "timestamp": time.time(),
        "app_id": get_app_id()
    }
    if not is_supabase_available():
        _get_offline_queue().append(record)
        return
    try:
        client = get_supabase_client()
        client.table("history").insert(record).execute()
        _mark_supabase_online()
    except Exception as e:
        _log("Mesajul nu a putut fi salvat", "warning", e)
        _mark_supabase_offline()
        _get_offline_queue().append(record)


def load_history_from_db(session_id, limit: int = MAX_MESSAGES_IN_MEMORY):
    if not is_supabase_available():
        return st.session_state.get("messages", [])[-limit:]
    try:
        client = get_supabase_client()
        response = (
            client.table("history")
            .select("role, content, timestamp")
            .eq("session_id", session_id)
            .eq("app_id", get_app_id())
            .order("timestamp", desc=False)
            .limit(limit)
            .execute()
        )
        return [{"role": row["role"], "content": row["content"]} for row in response.data]
    except Exception as e:
        _log("Eroare la încărcarea istoricului", "silent", e)
        return st.session_state.get("messages", [])[-limit:]


def clear_history_db(session_id):
    try:
        supabase = get_supabase_client()
        supabase.table("history").delete().eq("session_id", session_id).eq("app_id", get_app_id()).execute()
        invalidate_session_cache()
    except Exception as e:
        _log("Istoricul nu a putut fi șters", "warning", e)


def trim_db_messages(session_id: str):
    try:
        supabase = get_supabase_client()
        count_resp = (
            supabase.table("history")
            .select("id", count="exact")
            .eq("session_id", session_id)
            .eq("app_id", get_app_id())
            .execute()
        )
        count = count_resp.count or 0
        if count > MAX_MESSAGES_IN_DB_PER_SESSION:
            to_delete = count - MAX_MESSAGES_IN_DB_PER_SESSION
            old_resp = (
                supabase.table("history")
                .select("id")
                .eq("session_id", session_id)
                .eq("app_id", get_app_id())
                .order("timestamp", desc=False)
                .limit(to_delete)
                .execute()
            )
            ids_to_delete = [row["id"] for row in old_resp.data]
            if ids_to_delete:
                supabase.table("history").delete().in_("id", ids_to_delete).execute()
    except Exception as e:
        _log("Eroare la curățarea DB", "silent", e)


# === SESSION MANAGEMENT ===
def generate_unique_session_id() -> str:
    uuid_part = uuid.uuid4().hex[:16]
    time_part = hex(int(time.time() * 1000000))[2:][-8:]
    random_part = uuid.uuid4().hex[:8]
    return f"{uuid_part}{time_part}{random_part}"


_SESSION_ID_RE = re.compile(r'^[a-f0-9]{16,64}$')


def is_valid_session_id(sid: str) -> bool:
    if not sid or not isinstance(sid, str):
        return False
    return bool(_SESSION_ID_RE.match(sid))


def session_exists_in_db(session_id: str) -> bool:
    try:
        supabase = get_supabase_client()
        response = (
            supabase.table("sessions")
            .select("session_id")
            .eq("session_id", session_id)
            .eq("app_id", get_app_id())
            .limit(1)
            .execute()
        )
        return len(response.data) > 0
    except Exception:
        return False


def register_session(session_id: str):
    if not is_supabase_available():
        return
    try:
        client = get_supabase_client()
        now = time.time()
        client.table("sessions").upsert({
            "session_id": session_id,
            "created_at": now,
            "last_active": now,
            "app_id": get_app_id()
        }).execute()
    except Exception as e:
        _log("Eroare la înregistrarea sesiunii", "silent", e)


def update_session_activity(session_id: str):
    last = st.session_state.get("_last_activity_update", 0)
    if time.time() - last < 300:
        return
    st.session_state["_last_activity_update"] = time.time()
    if not is_supabase_available():
        return
    try:
        client = get_supabase_client()
        client.table("sessions").update({
            "last_active": time.time()
        }).eq("session_id", session_id).execute()
    except Exception as e:
        _log("Eroare la actualizarea sesiunii", "silent", e)


def inject_session_js():
    import streamlit.components.v1 as components
    components.html("""
    <script>
    (function() {
        const SID_KEY    = 'profesor_session_id';
        const APIKEY_KEY = 'profesor_api_key';
        const params     = new URLSearchParams(window.parent.location.search);

        const sidFromUrl = params.get('sid');
        const storedSid  = localStorage.getItem(SID_KEY);

        if (sidFromUrl && sidFromUrl.length >= 16) {
            localStorage.setItem(SID_KEY, sidFromUrl);
            params.delete('sid');
        }

        const keyFromUrl = params.get('apikey');
        if (keyFromUrl && keyFromUrl.startsWith('AIza')) {
            localStorage.setItem(APIKEY_KEY, keyFromUrl);
            params.delete('apikey');
        } else {
            const storedKey = localStorage.getItem(APIKEY_KEY);
            if (storedKey && storedKey.startsWith('AIza') && !params.get('apikey')) {
                params.set('apikey', storedKey);
            }
        }

        const newSearch = params.toString();
        const newUrl = window.parent.location.pathname +
            (newSearch ? '?' + newSearch : '');
        if (window.parent.location.href !== window.parent.location.origin + newUrl) {
            window.parent.history.replaceState(null, '', newUrl);
        }
    })();
    </script>
    <script>
    window._clearStoredApiKey = function() {
        localStorage.removeItem('profesor_api_key');
    };
    </script>
    """, height=0)


def get_or_create_session_id() -> str:
    if "session_id" in st.session_state:
        existing_id = st.session_state.session_id
        if is_valid_session_id(existing_id):
            return existing_id

    if "sid" in st.query_params:
        sid_from_storage = st.query_params["sid"]
        if is_valid_session_id(sid_from_storage):
            if session_exists_in_db(sid_from_storage):
                try:
                    st.query_params.pop("sid", None)
                except Exception:
                    pass
                return sid_from_storage

    for _ in range(10):
        new_id = generate_unique_session_id()
        if not session_exists_in_db(new_id):
            register_session(new_id)
            try:
                st.query_params["sid"] = new_id
            except Exception:
                pass
            return new_id

    fallback_id = uuid.uuid4().hex + uuid.uuid4().hex[:8]
    register_session(fallback_id)
    return fallback_id


# === MEMORY MANAGEMENT ===
def trim_session_messages():
    if "messages" in st.session_state:
        current_count = len(st.session_state.messages)
        if current_count > MAX_MESSAGES_IN_MEMORY:
            excess = current_count - MAX_MESSAGES_IN_MEMORY
            st.session_state.messages = st.session_state.messages[excess:]
            st.toast(f"📝 Am arhivat {excess} mesaje vechi pentru performanță.", icon="📦")


def get_context_for_ai(messages: list) -> list:
    if len(messages) <= MAX_MESSAGES_TO_SEND_TO_AI:
        return messages
    first_message = messages[0] if messages else None
    recent_messages = messages[-MAX_MESSAGES_TO_SEND_TO_AI:]
    if first_message and first_message not in recent_messages:
        return [first_message] + recent_messages[1:]
    return recent_messages


def save_message_with_limits(session_id: str, role: str, content: str):
    save_message_to_db(session_id, role, content)
    invalidate_session_cache()
    if len(st.session_state.get("messages", [])) % 10 == 0:
        trim_db_messages(session_id)
    trim_session_messages()


# === HELPER: get current mod_avansat value ===
def _get_mod_avansat() -> bool:
    """Helper to safely get mod_avansat from session state."""
    return st.session_state.get("mod_avansat", False)


# === AUDIO / TTS FUNCTIONS ===
_UNITS: list[tuple[str, str]] = [
    ("GΩ", "gigaohmi"), ("MΩ", "megaohmi"), ("kΩ", "kiloohmi"),
    ("mΩ", "miliohmi"), ("μΩ", "microohmi"), ("nΩ", "nanoohmi"), ("Ω", "ohmi"),
    ("°C", "grade Celsius"), ("°F", "grade Fahrenheit"), ("°K", "Kelvin"), ("K", "Kelvin"), ("°", "grade"),
    ("MV", "megavolți"), ("kV", "kilovolți"), ("mV", "milivolți"), ("μV", "microvolți"), ("V", "volți"),
    ("kA", "kiloamperi"), ("mA", "miliamperi"), ("μA", "microamperi"), ("nA", "nanoamperi"), ("A", "amperi"),
    ("GW", "gigawați"), ("MW", "megawați"), ("kW", "kilowați"), ("mW", "miliwați"), ("μW", "microwați"), ("W", "wați"),
    ("THz", "terahertzi"), ("GHz", "gigahertzi"), ("MHz", "megahertzi"), ("kHz", "kilohertzi"), ("mHz", "milihertzi"), ("Hz", "hertzi"),
    ("mF", "milifarazi"), ("μF", "microfarazi"), ("nF", "nanofarazi"), ("pF", "picofarazi"), ("F", "farazi"),
    ("mH", "milihenry"), ("μH", "microhenry"), ("nH", "nanohenry"), ("H", "henry"),
    ("mC", "milicoulombi"), ("μC", "microcoulombi"), ("nC", "nanocoulombi"), ("C", "coulombi"),
    ("Wb", "weberi"), ("mT", "militesla"), ("μT", "microtesla"), ("T", "tesla"),
    ("MN", "meganewtoni"), ("kN", "kilonewtoni"), ("mN", "milinewtoni"), ("N", "newtoni"),
    ("kWh", "kilowatt oră"), ("Wh", "watt oră"),
    ("GeV", "gigaelectronvolți"), ("MeV", "megaelectronvolți"), ("keV", "kiloelectronvolți"), ("eV", "electronvolți"),
    ("kcal", "kilocalorii"), ("cal", "calorii"),
    ("GJ", "gigajouli"), ("MJ", "megajouli"), ("kJ", "kilojouli"), ("mJ", "milijouli"), ("J", "jouli"),
    ("GPa", "gigapascali"), ("MPa", "megapascali"), ("kPa", "kilopascali"), ("hPa", "hectopascali"), ("Pa", "pascali"),
    ("mmHg", "milimetri coloană de mercur"), ("atm", "atmosfere"), ("bar", "bari"),
    ("km", "kilometri"), ("dm", "decimetri"), ("cm", "centimetri"), ("mm", "milimetri"),
    ("μm", "micrometri"), ("nm", "nanometri"), ("pm", "picometri"), ("Å", "angstromi"), ("m", "metri"),
    ("kg", "kilograme"), ("mg", "miligrame"), ("μg", "micrograme"), ("ng", "nanograme"), ("g", "grame"), ("t", "tone"),
    ("mL", "mililitri"), ("ml", "mililitri"), ("μL", "microlitri"), ("L", "litri"), ("l", "litri"),
    ("dm³", "decimetri cubi"), ("cm³", "centimetri cubi"), ("mm³", "milimetri cubi"), ("m³", "metri cubi"),
    ("ms", "milisecunde"), ("μs", "microsecunde"), ("ns", "nanosecunde"), ("ps", "picosecunde"),
    ("min", "minute"), ("s", "secunde"), ("h", "ore"),
    ("km²", "kilometri pătrați"), ("m²", "metri pătrați"), ("dm²", "decimetri pătrați"),
    ("cm²", "centimetri pătrați"), ("mm²", "milimetri pătrați"), ("ha", "hectare"),
    ("m/s²", "metri pe secundă la pătrat"), ("m/s", "metri pe secundă"), ("km/h", "kilometri pe oră"),
    ("km/s", "kilometri pe secundă"), ("cm/s", "centimetri pe secundă"),
    ("rad/s", "radiani pe secundă"), ("rpm", "rotații pe minut"),
    ("kg/m³", "kilograme pe metru cub"), ("g/cm³", "grame pe centimetru cub"), ("g/mL", "grame pe mililitru"),
    ("N/m²", "newtoni pe metru pătrat"), ("N/m", "newtoni pe metru"),
    ("J/kg", "jouli pe kilogram"), ("J/mol", "jouli pe mol"),
    ("W/m²", "wați pe metru pătrat"), ("V/m", "volți pe metru"), ("A/m", "amperi pe metru"),
    ("mol/L", "moli pe litru"), ("mol/l", "moli pe litru"),
    ("g/mol", "grame pe mol"), ("kg/mol", "kilograme pe mol"),
    ("mol", "moli"), ("M", "molar"),
    ("Bq", "becquereli"), ("Gy", "gray"), ("Sv", "sievert"),
    ("cd", "candele"), ("lm", "lumeni"), ("lx", "lucși"),
    ("rad", "radiani"), ("sr", "steradiani"),
]

_SYMBOLS: dict[str, str] = {
    ">=": " mai mare sau egal cu ", "<=": " mai mic sau egal cu ",
    "!=": " diferit de ", "==": " egal cu ", "<>": " diferit de ",
    ">>": " mult mai mare decât ", "<<": " mult mai mic decât ",
    "->": " implică ", "<-": " provine din ", "<->": " echivalent cu ", "=>": " rezultă că ",
    "...": " ", "…": " ", "N·m": " newton metri ", "N*m": " newton metri ", "kW·h": " kilowatt oră ",
    "α": " alfa ", "β": " beta ", "γ": " gama ", "δ": " delta ", "ε": " epsilon ",
    "ζ": " zeta ", "η": " eta ", "θ": " teta ", "ι": " iota ", "κ": " kapa ",
    "λ": " lambda ", "μ": " miu ", "ν": " niu ", "ξ": " csi ", "ο": " omicron ",
    "π": " pi ", "ρ": " ro ", "σ": " sigma ", "ς": " sigma ", "τ": " tau ",
    "υ": " ipsilon ", "φ": " fi ", "χ": " hi ", "ψ": " psi ", "ω": " omega ",
    "Α": " alfa ", "Β": " beta ", "Γ": " gama ", "Δ": " delta ", "Ε": " epsilon ",
    "Ζ": " zeta ", "Η": " eta ", "Θ": " teta ", "Ι": " iota ", "Κ": " kapa ",
    "Λ": " lambda ", "Μ": " miu ", "Ν": " niu ", "Ξ": " csi ", "Ο": " omicron ",
    "Π": " pi ", "Ρ": " ro ", "Σ": " sigma ", "Τ": " tau ", "Υ": " ipsilon ",
    "Φ": " fi ", "Χ": " hi ", "Ψ": " psi ", "Ω": " omega ",
    "∞": " infinit ", "∑": " suma ", "∏": " produsul ", "∫": " integrala ",
    "∂": " derivata parțială ", "√": " radical din ", "∛": " radical de ordin 3 din ",
    "∜": " radical de ordin 4 din ", "±": " plus minus ", "∓": " minus plus ",
    "×": " ori ", "÷": " împărțit la ", "≠": " diferit de ", "≈": " aproximativ egal cu ",
    "≡": " identic cu ", "≤": " mai mic sau egal cu ", "≥": " mai mare sau egal cu ",
    "≪": " mult mai mic decât ", "≫": " mult mai mare decât ", "∝": " proporțional cu ",
    "∈": " aparține lui ", "∉": " nu aparține lui ", "⊂": " inclus în ", "⊃": " include ",
    "⊆": " inclus sau egal cu ", "⊇": " include sau egal cu ",
    "∪": " reunit cu ", "∩": " intersectat cu ", "∅": " mulțimea vidă ",
    "∀": " pentru orice ", "∃": " există ", "∄": " nu există ",
    "∴": " deci ", "∵": " deoarece ",
    "→": " implică ", "←": " rezultă din ", "↔": " echivalent cu ",
    "⇒": " rezultă că ", "⇐": " provine din ", "⇔": " dacă și numai dacă ",
    "↑": " crește ", "↓": " scade ", "°": " grade ", "′": " ", "″": " ",
    "‰": " la mie ", "∠": " unghiul ", "⊥": " perpendicular pe ", "∥": " paralel cu ",
    "△": " triunghiul ", "□": " ", "○": " ", "★": " ", "☆": " ",
    "✓": " corect ", "✗": " greșit ", "✘": " greșit ",
    ">": " mai mare decât ", "<": " mai mic decât ", "=": " egal ",
    "+": " plus ", "−": " minus ", "—": " ", "–": " ",
    "·": " ori ", "•": " ", "∙": " ori ", "⋅": " ori ",
    "⁰": " la puterea 0 ", "¹": " la puterea 1 ", "²": " la pătrat ", "³": " la cub ",
    "⁴": " la puterea 4 ", "⁵": " la puterea 5 ", "⁶": " la puterea 6 ",
    "⁷": " la puterea 7 ", "⁸": " la puterea 8 ", "⁹": " la puterea 9 ",
    "⁺": " plus ", "⁻": " minus ", "⁼": " egal ",
    "₀": " indice 0 ", "₁": " indice 1 ", "₂": " indice 2 ", "₃": " indice 3 ",
    "₄": " indice 4 ", "₅": " indice 5 ", "₆": " indice 6 ", "₇": " indice 7 ",
    "₈": " indice 8 ", "₉": " indice 9 ",
    "%": " procent ", "&": " și ", "#": " numărul ", "~": " aproximativ ",
    "≅": " congruent cu ", "≃": " aproximativ egal cu ", "|": " ", "‖": " ", "⋯": " ",
    "∧": " și ", "∨": " sau ", "¬": " negația lui ",
    "ℕ": " mulțimea numerelor naturale ", "ℤ": " mulțimea numerelor întregi ",
    "ℚ": " mulțimea numerelor raționale ", "ℝ": " mulțimea numerelor reale ",
    "ℂ": " mulțimea numerelor complexe ", "℃": " grade Celsius ", "℉": " grade Fahrenheit ",
    "Å": " angstrom ", "№": " numărul ",
}

_LATEX_PATTERNS: list[tuple[str, str]] = [
    (r'\\sqrt\[(\d+)\]\{([^}]+)\}', r' radical de ordin \1 din \2 '),
    (r'\\sqrt\{([^}]+)\}', r' radical din \1 '),
    (r'\\d?frac\{([^}]+)\}\{([^}]+)\}', r' \1 supra \2 '),
    (r'\^\{([^}]+)\}', r' la puterea \1 '), (r'\^(\d+)', r' la puterea \1 '),
    (r'_\{([^}]+)\}', r' indice \1 '),     (r'_(\d+)', r' indice \1 '),
    (r'\\alpha', ' alfa '), (r'\\beta', ' beta '), (r'\\gamma', ' gama '),
    (r'\\delta', ' delta '), (r'\\(?:var)?epsilon', ' epsilon '),
    (r'\\zeta', ' zeta '), (r'\\eta', ' eta '), (r'\\(?:var)?theta', ' teta '),
    (r'\\iota', ' iota '), (r'\\kappa', ' kapa '), (r'\\lambda', ' lambda '),
    (r'\\mu', ' miu '), (r'\\nu', ' niu '), (r'\\xi', ' csi '),
    (r'\\(?:var)?pi', ' pi '), (r'\\(?:var)?rho', ' ro '),
    (r'\\(?:var)?sigma', ' sigma '), (r'\\tau', ' tau '), (r'\\upsilon', ' ipsilon '),
    (r'\\(?:var)?phi', ' fi '), (r'\\chi', ' hi '), (r'\\psi', ' psi '),
    (r'\\(?:var)?omega', ' omega '),
    (r'\\Gamma', ' gama '), (r'\\Delta', ' delta '), (r'\\Theta', ' teta '),
    (r'\\Lambda', ' lambda '), (r'\\Xi', ' csi '), (r'\\Pi', ' pi '),
    (r'\\Sigma', ' sigma '), (r'\\Upsilon', ' ipsilon '), (r'\\Phi', ' fi '),
    (r'\\Psi', ' psi '), (r'\\Omega', ' omega '),
    (r'\\times', ' ori '), (r'\\cdot', ' ori '), (r'\\div', ' împărțit la '),
    (r'\\pm', ' plus minus '), (r'\\mp', ' minus plus '),
    (r'\\(?:leq?)', ' mai mic sau egal cu '), (r'\\(?:geq?)', ' mai mare sau egal cu '),
    (r'\\(?:neq?)', ' diferit de '), (r'\\approx', ' aproximativ egal cu '),
    (r'\\equiv', ' echivalent cu '), (r'\\sim', ' similar cu '),
    (r'\\propto', ' proporțional cu '), (r'\\infty', ' infinit '),
    (r'\\sum', ' suma '), (r'\\prod', ' produsul '),
    (r'\\iiint', ' integrala triplă '), (r'\\iint', ' integrala dublă '),
    (r'\\oint', ' integrala pe contur '), (r'\\int', ' integrala '),
    (r'\\lim', ' limita '), (r'\\log', ' logaritm de '), (r'\\ln', ' logaritm natural de '),
    (r'\\lg', ' logaritm zecimal de '), (r'\\exp', ' exponențiala de '),
    (r'\\sin', ' sinus de '), (r'\\cos', ' cosinus de '),
    (r'\\(?:tg|tan)', ' tangentă de '), (r'\\(?:ctg|cot)', ' cotangentă de '),
    (r'\\sec', ' secantă de '), (r'\\csc', ' cosecantă de '),
    (r'\\arcsin', ' arc sinus de '), (r'\\arccos', ' arc cosinus de '),
    (r'\\(?:arctg|arctan)', ' arc tangentă de '),
    (r'\\sinh', ' sinus hiperbolic de '), (r'\\cosh', ' cosinus hiperbolic de '),
    (r'\\tanh', ' tangentă hiperbolică de '),
    (r'\\(?:right|left)?arrow', ' implică '), (r'\\to\b', ' tinde la '),
    (r'\\Rightarrow', ' rezultă că '), (r'\\Leftarrow', ' este implicat de '),
    (r'\\[Ll]eftrightarrow', ' echivalent cu '), (r'\\Leftrightarrow', ' dacă și numai dacă '),
    (r'\\forall', ' pentru orice '), (r'\\exists', ' există '), (r'\\nexists', ' nu există '),
    (r'\\in\b', ' aparține lui '), (r'\\notin', ' nu aparține lui '),
    (r'\\subseteq', ' inclus sau egal cu '), (r'\\supseteq', ' include sau egal cu '),
    (r'\\subset', ' inclus în '), (r'\\supset', ' include '),
    (r'\\cup', ' reunit cu '), (r'\\cap', ' intersectat cu '),
    (r'\\(?:empty[Ss]et|varnothing)', ' mulțimea vidă '),
    (r'\\mathbb\{R\}', ' mulțimea numerelor reale '),
    (r'\\mathbb\{N\}', ' mulțimea numerelor naturale '),
    (r'\\mathbb\{Z\}', ' mulțimea numerelor întregi '),
    (r'\\mathbb\{Q\}', ' mulțimea numerelor raționale '),
    (r'\\mathbb\{C\}', ' mulțimea numerelor complexe '),
    (r'\\partial', ' derivata parțială '), (r'\\nabla', ' nabla '),
    (r'\\(?:degree|circ)\b', ' grad '), (r'\\(?:angle|measuredangle)', ' unghiul '),
    (r'\\perp', ' perpendicular pe '), (r'\\parallel', ' paralel cu '),
    (r'\\triangle', ' triunghiul '), (r'\\square', ' pătratul '),
    (r'\\therefore', ' deci '), (r'\\because', ' deoarece '),
    (r'\\lt\b', ' mai mic decât '), (r'\\gt\b', ' mai mare decât '),
]

_NUM = r'(\d+[.,]?\d*)'
_UNIT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(
            r'(?<![A-Za-z])' + _NUM + r'\s*' + re.escape(unit) + r'(?![A-Za-z/²³])'
        ),
        r'\1 ' + pron
    )
    for unit, pron in _UNITS
]


def clean_text_for_audio(text: str) -> str:
    if not text:
        return ""

    text = re.sub(
        r'[\U0001F300-\U0001F9FF\U00002600-\U000027BF\U0001F000-\U0001F02F'
        r'\U0001F0A0-\U0001F0FF\U0001F100-\U0001F1FF\U0001F200-\U0001F2FF'
        r'\U00002702-\U000027B0\U000024C2-\U0001F251]',
        '', text, flags=re.UNICODE
    )

    text = re.sub(r'\*\*Pasul\s+(\d+)\s*[—–-]+\s*([^*]+)\*\*\s*:', r'Pasul \1. \2.', text)
    text = re.sub(r'\*\*(Ce avem|Ce căutăm|Rezolvare|Răspuns final|Reține)[:\s*]*\*\*', r'\1.', text)
    text = re.sub(r'[═=\-─]{3,}', ' ', text)
    # Clean all drawing blocks (Matplotlib/Mermaid/Plotly/ASCII) for TTS
    text = clean_drawing_blocks_for_audio(text)

    for pattern, replacement in _UNIT_PATTERNS:
        text = pattern.sub(replacement, text)

    text = re.sub(r'([A-Za-zα-ωΑ-Ω])\s*_\s*\{([^}]+)\}', r'\1 indice \2', text)
    text = re.sub(r'([A-Za-zα-ωΑ-Ω])\s*_\s*([A-Za-z0-9α-ωΑ-Ω]+)', r'\1 indice \2', text)

    for symbol, replacement in _SYMBOLS.items():
        text = text.replace(symbol, replacement)

    text = re.sub(r'(\d)\s*:\s*(\d)', r'\1 este la \2', text)
    text = re.sub(r'(\d+)\s*/\s*(\d+)', r'\1 supra \2', text)
    text = re.sub(r':\s*$', '.', text)
    text = re.sub(r':\s*\n', '.\n', text)
    text = re.sub(r'(\w):\s+', r'\1. ', text)

    for pattern, replacement in _LATEX_PATTERNS:
        text = re.sub(pattern, replacement, text)

    text = re.sub(r'\$\$([^$]+)\$\$', r' \1 ', text)
    text = re.sub(r'\$([^$]+)\$', r' \1 ', text)
    text = re.sub(r'\\\[(.+?)\\\]', r' \1 ', text, flags=re.DOTALL)
    text = re.sub(r'\\\((.+?)\\\)', r' \1 ', text)
    text = re.sub(r'\\[a-zA-Z]+\{[^}]*\}', '', text)
    text = re.sub(r'\\[a-zA-Z]+', '', text)
    text = re.sub(r'[{}\\]', '', text)
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'^#{1,6}\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'[│▌►◄■▪▫\[\](){}]', ' ', text)
    text = re.sub(r'\s*:\s*', '. ', text)
    text = re.sub(r'\s+', ' ', text)

    text = text.strip()
    if len(text) > 3000:
        text = text[:3000]
        last_period = max(text.rfind('.'), text.rfind('!'), text.rfind('?'))
        if last_period > 2500:
            text = text[:last_period + 1]

    return text


async def _generate_audio_edge_tts(text: str, voice: str = VOICE_MALE_RO) -> bytes:
    try:
        clean_text = clean_text_for_audio(text)
        if not clean_text or len(clean_text.strip()) < 10:
            return None
        communicate = edge_tts.Communicate(clean_text, voice)
        audio_data = BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_data.write(chunk["data"])
        audio_data.seek(0)
        return audio_data.getvalue()
    except Exception as e:
        _log("Eroare Edge TTS", "silent", e)
        return None


def generate_professor_voice(text: str, voice: str = VOICE_MALE_RO) -> BytesIO:
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            audio_bytes = loop.run_until_complete(_generate_audio_edge_tts(text, voice))
        finally:
            loop.close()
        if audio_bytes:
            audio_file = BytesIO(audio_bytes)
            audio_file.seek(0)
            return audio_file
        return None
    except Exception as e:
        _log("Eroare la generarea vocii", "silent", e)
        return None


# === DRAWING MODULE (Matplotlib / Mermaid / Plotly / ASCII) ===
# Replaces SVG completely. Auto-selects library based on content type.
from drawing_module import (
    render_message,
    clean_drawing_blocks_for_audio,
    DRAWING_SYSTEM_PROMPT,
)


# === INIȚIALIZARE ===
init_db()
cleanup_old_sessions(CLEANUP_DAYS_OLD)

session_id = get_or_create_session_id()
st.session_state.session_id = session_id
update_session_activity(session_id)

if not st.session_state.get("_js_injected"):
    inject_session_js()
    st.session_state["_js_injected"] = True


# === API KEYS ===
if not st.session_state.get("_manual_api_key"):
    key_from_url = st.query_params.get("apikey", "")
    if key_from_url and key_from_url.startswith("AIza") and len(key_from_url) > 20:
        st.session_state["_manual_api_key"] = key_from_url.strip()
        st.query_params.pop("apikey", None)

saved_manual_key = st.session_state.get("_manual_api_key", "")

raw_keys_secrets = None
if "GOOGLE_API_KEYS" in st.secrets:
    raw_keys_secrets = st.secrets["GOOGLE_API_KEYS"]
elif "GOOGLE_API_KEY" in st.secrets:
    raw_keys_secrets = [st.secrets["GOOGLE_API_KEY"]]

keys = []
if raw_keys_secrets:
    if isinstance(raw_keys_secrets, str):
        try:
            raw_keys_secrets = ast.literal_eval(raw_keys_secrets)
        except Exception:
            raw_keys_secrets = [raw_keys_secrets]
    if isinstance(raw_keys_secrets, list):
        for k in raw_keys_secrets:
            if k and isinstance(k, str):
                clean_k = k.strip().strip('"').strip("'")
                if clean_k:
                    keys.append(clean_k)

if saved_manual_key and saved_manual_key not in keys:
    keys.append(saved_manual_key)

_are_secrets_keys = len([k for k in keys if k != saved_manual_key]) > 0

with st.sidebar:
    if not _are_secrets_keys:
        st.divider()
        st.subheader("🔑 Cheie API Google AI")
        if not saved_manual_key:
            with st.expander("❓ Cum obțin o cheie? (gratuit)", expanded=False):
                st.markdown("**Ai nevoie de un cont Google** (Gmail). Este complet gratuit.")
                st.markdown("**Pasul 1** — Deschide Google AI Studio:")
                st.link_button("🌐 Mergi la aistudio.google.com", "https://aistudio.google.com/apikey", use_container_width=True)
                st.markdown("""
**Pasul 2** — Autentifică-te cu contul Google.
**Pasul 3** — Apasă **"Create API key"** (buton albastru).
**Pasul 4** — Copiază cheia afișată (forma: `AIzaSy...`, 39 caractere).
**Pasul 5** — Lipește cheia mai jos și apasă **Salvează**.

💡 **Limită gratuită:** 15 cereri/minut, 1 milion tokeni/zi.
                """)
            st.caption("Cheia se salvează în browserul tău și rămâne activă după refresh.")
            new_key = st.text_input("Cheie API Google AI:", type="password", placeholder="AIzaSy...", label_visibility="collapsed")
            if st.button("✅ Salvează cheia", use_container_width=True, type="primary", key="save_api_key"):
                clean = new_key.strip().strip('"').strip("'")
                if clean and clean.startswith("AIza") and len(clean) > 20:
                    st.session_state["_manual_api_key"] = clean
                    keys.append(clean)
                    st.query_params["apikey"] = clean
                    st.toast("✅ Cheie salvată în browser!", icon="🔑")
                    st.rerun()
                else:
                    st.error("❌ Cheie invalidă. Trebuie să înceapă cu 'AIza' și să aibă minim 20 caractere.")
        else:
            st.success("🔑 Cheie personală activă.")
            st.caption("Salvată în browserul tău — rămâne după refresh.")
            if st.button("🗑️ Șterge cheia", use_container_width=True, key="del_api_key"):
                st.session_state.pop("_manual_api_key", None)
                st.query_params.pop("apikey", None)
                import streamlit.components.v1 as _comp
                _comp.html("<script>localStorage.removeItem('profesor_api_key');</script>", height=0)
                st.rerun()

if not keys:
    st.error("❌ Nicio cheie API validă. Introdu cheia ta Google AI în bara laterală.")
    st.stop()

if "key_index" not in st.session_state:
    import random
    st.session_state.key_index = random.randint(0, max(len(keys) - 1, 0))


# === MATERII ===
MATERII = {
    "🎓 Toate materiile": None,
    "📐 Matematică":      "matematică",
    "⚡ Fizică":          "fizică",
    "🧪 Chimie":          "chimie",
    "📖 Română":          "limba și literatura română",
    "🇫🇷 Franceză":       "limba franceză",
    "🇬🇧 Engleză":        "limba engleză",
    "🌍 Geografie":       "geografie",
    "🏛️ Istorie":         "istorie",
    "💻 Informatică":     "informatică",
    "🧬 Biologie":        "biologie",
}


def get_system_prompt(materie: str | None = None, pas_cu_pas: bool = False,
                      mod_strategie: bool = False,
                      mod_bac_intensiv: bool = False, mod_avansat: bool = False) -> str:

    if materie:
        rol_line = (
            f"ROL: Ești un profesor de liceu din România specializat în {materie.upper()}, "
            f"bărbat, cu experiență în pregătirea pentru BAC. "
            f"Răspunde EXCLUSIV la întrebări legate de {materie}. "
            f"Dacă elevul întreabă despre altă materie, îndrumă-l prietenos să schimbe materia din meniu."
        )
    else:
        rol_line = (
            "ROL: Ești un profesor de liceu din România, universal "
            "(Mate, Fizică, Chimie, Literatură și Gramatică Română, Franceză, Engleză, "
            "Geografie, Istorie, Informatică, Biologie), bărbat, cu experiență în pregătirea pentru BAC."
        )

    pas_cu_pas_bloc = r"""

    ═══════════════════════════════════════════════════
    MOD ACTIV: EXPLICAȚIE PAS CU PAS (PRIORITATE MAXIMĂ)
    ═══════════════════════════════════════════════════
    FORMAT OBLIGATORIU pentru orice problemă sau explicație:
    **📋 Ce avem:** — Listează datele cunoscute
    **🎯 Ce căutăm:** — Spune clar ce trebuie aflat
    **🔢 Rezolvare pas cu pas:**
    **Pasul 1 — [nume pas]:** [acțiune + de ce o facem]
    **Pasul 2 — [nume pas]:** [acțiune + de ce o facem]
    **✅ Răspuns final:** [rezultatul clar, cu unități]
    **💡 Reține:** — 1-2 idei cheie

    REGULI: niciodată nu sări un pas, explică DE CE la fiecare pas, verifică răspunsul la final.
    ═══════════════════════════════════════════════════
""" if pas_cu_pas else ""

    mod_strategie_bloc = r"""

    ═══════════════════════════════════════════════════
    MOD ACTIV: EXPLICĂ-MI STRATEGIA (PRIORITATE MAXIMĂ)
    ═══════════════════════════════════════════════════
    **🧠 Cum recunoști tipul de problemă:**
    **🗺️ Strategia de rezolvare (fără calcule):**
    **⚠️ Capcane frecvente:**
    **✏️ Acum încearcă tu:**

    REGULI: NU calcula nimic — explică doar logica și gândirea.
    ═══════════════════════════════════════════════════
""" if mod_strategie else ""

    mod_bac_intensiv_bloc = r"""

    ═══════════════════════════════════════════════════
    MOD ACTIV: PREGĂTIRE BAC INTENSIVĂ (PRIORITATE MAXIMĂ)
    ═══════════════════════════════════════════════════
    - Focusează-te EXCLUSIV pe ce apare la BAC
    - Menționează frecvența la BAC: "Apare frecvent" sau "Rar, dar posibil"
    - Structurează ca la subiectele BAC (Subiectul I / II / III)
    - Punctaj estimativ și timp estimativ per problemă
    - TEORIA LIPSĂ: dacă observi că elevul nu are baza teoretică, OPREȘTE-TE și explică
    ═══════════════════════════════════════════════════
""" if mod_bac_intensiv else r"""

    TEORIA LIPSĂ — DETECTARE AUTOMATĂ:
    Dacă observi că elevul nu are baza teoretică: explică teoria ÎNTÂI, apoi continuă.
"""

    mod_avansat_bloc = r"""

    ═══════════════════════════════════════════════════
    MOD ACTIV: AVANSAT (PRIORITATE MAXIMĂ)
    ═══════════════════════════════════════════════════
    NU explica concepte de bază. Mergi DIRECT la ideea cheie.
    Format: 💡 **Ideea:** → ⚡ **Calcul rapid:** → ✅ **Rezultat:**
    Răspuns scurt și dens: maxim 3-5 rânduri pentru o problemă tipică.
    ═══════════════════════════════════════════════════
""" if mod_avansat else ""

    return f"""
ROL: {rol_line}
{pas_cu_pas_bloc}{mod_strategie_bloc}{mod_bac_intensiv_bloc}{mod_avansat_bloc}

    REGULI DE IDENTITATE: Folosește EXCLUSIV genul masculin. Te prezinți ca "Domnul Profesor".
    TON: Vorbește DIRECT, la persoana I. Fii cald, natural. NU saluta în fiecare mesaj.
    PREDĂ exact ca la școală (nivel Gimnaziu/Liceu). Lucrează cu valori exacte (√2, π).
    Folosește LaTeX ($...$) pentru toate formulele matematice.
    Notații: f'(x) pentru derivată, ln(x) pentru log natural, lg(x) pentru log zecimal,
             tg(x) pentru tangentă, ctg(x) pentru cotangentă.

{DRAWING_SYSTEM_PROMPT}"""


# === DETECȚIE AUTOMATĂ MATERIE ===
SUBJECT_KEYWORDS = {
    "matematică": [
        "ecuație", "funcție", "derivată", "integrală", "limită", "matrice", "determinant",
        "trigonometrie", "geometrie", "algebră", "logaritm", "radical", "inecuație",
        "probabilitate", "combinatorică", "vector", "parabola", "matematica", "mate",
    ],
    "fizică": [
        "forță", "viteză", "accelerație", "masă", "energie", "putere", "curent", "tensiune",
        "rezistență", "circuit", "câmp", "undă", "optică", "lentilă", "termodinamică",
        "gaz", "presiune", "temperatură", "fizica", "mecanică", "electricitate", "baterie",
        "gravitație", "frecare", "pendul",
    ],
    "chimie": [
        "atom", "moleculă", "element", "compus", "reacție", "acid", "baza", "sare",
        "oxidare", "reducere", "electroliză", "moli", "mol", "masă molară",
        "stoechiometrie", "organic", "alcan", "alchenă", "alcool", "ester", "chimica",
        "ph", "soluție", "concentratie",
    ],
    "biologie": [
        "celulă", "adn", "arn", "proteină", "enzimă", "mitoză", "meioză", "genetică",
        "cromozom", "fotosinteză", "respiratie", "metabolism", "ecosistem", "specie",
        "organ", "tesut", "sistem nervos", "biologie",
    ],
    "informatică": [
        "algoritm", "cod", "c++", "program", "functie", "vector", "array",
        "recursivitate", "sortare", "căutare", "stivă", "coada", "backtracking",
        "greedy", "sql", "informatica", "pseudocod", "variabila", "ciclu",
    ],
    "geografie": [
        "relief", "munte", "câmpie", "râu", "dunărea", "climă", "vegetatie",
        "populație", "romania", "europa", "continent", "ocean", "geografie",
        "carpati", "câmpia", "delta", "lac",
    ],
    "istorie": [
        "război", "revoluție", "unire", "independenta", "cuza", "mihai viteazul",
        "stefan cel mare", "comunism", "ceausescu", "bac 1918", "marea unire",
        "medieval", "evul mediu", "modern", "contemporan", "istorie", "domnie",
    ],
    "limba și literatura română": [
        "poezie", "poem", "eminescu", "rebreanu", "sadoveanu", "preda", "arghezi",
        "blaga", "bacovia", "caragiale", "creanga", "eseu", "comentariu",
        "caracterizare", "narator", "personaj", "figuri de stil", "metafora",
        "epitet", "comparatie", "roman", "proza", "dramaturgie", "gramatica", "romana",
    ],
    "limba engleză": [
        "english", "engleză", "engleza", "tense", "grammar", "essay", "present",
        "past", "future", "conditional", "passive",
    ],
    "limba franceză": [
        "français", "franceză", "franceza", "passé", "imparfait", "subjonctif",
        "verbe", "grammaire", "être", "avoir",
    ],
}


def detect_subject_from_text(text: str) -> str | None:
    text_lower = text.lower()
    scores = {}
    for subject, keywords in SUBJECT_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text_lower)
        if score > 0:
            scores[subject] = score
    if not scores:
        return None
    return max(scores, key=scores.get)


def update_system_prompt_for_subject(materie: str | None):
    """
    FIX: Separated get_system_prompt kwargs properly.
    Previously had mod_avansat= nested inside st.session_state.get() call.
    """
    st.session_state["_detected_subject"] = materie
    st.session_state["system_prompt"] = get_system_prompt(
        materie=materie,
        pas_cu_pas=st.session_state.get("pas_cu_pas", False),
        mod_avansat=st.session_state.get("mod_avansat", False),
        mod_strategie=st.session_state.get("mod_strategie", False),
        mod_bac_intensiv=st.session_state.get("mod_bac_intensiv", False),
    )


safety_settings = [
    {"category": "HARM_CATEGORY_HARASSMENT",        "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH",        "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",  "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT",  "threshold": "BLOCK_NONE"},
]

# System prompt initial
SYSTEM_PROMPT = get_system_prompt(
    pas_cu_pas=st.session_state.get("pas_cu_pas", False),
    mod_avansat=st.session_state.get("mod_avansat", False),
)


# ============================================================
# === SIMULARE BAC ===
# ============================================================
MATERII_BAC = {
    "📐 Matematică": {
        "cod": "matematica",
        "profile": ["M1 - Mate-Info", "M2 - Științe ale naturii"],
        "subiecte": ["Algebră", "Analiză matematică", "Geometrie"],
        "timp_minute": 180,
        "punctaj_total": 100,
    },
    "📖 Română": {
        "cod": "romana",
        "profile": ["Toate profilurile"],
        "subiecte": ["Text literar", "Text nonliterar", "Redactare eseu"],
        "timp_minute": 180,
        "punctaj_total": 100,
    },
    "⚡ Fizică": {
        "cod": "fizica",
        "profile": ["Mate-Info", "Științe ale naturii"],
        "subiecte": ["Mecanică", "Termodinamică", "Electricitate", "Optică"],
        "timp_minute": 180,
        "punctaj_total": 100,
    },
    "🧪 Chimie": {
        "cod": "chimie",
        "profile": ["Chimie anorganică", "Chimie organică"],
        "subiecte": ["Chimie anorganică", "Chimie organică"],
        "timp_minute": 180,
        "punctaj_total": 100,
    },
    "🧬 Biologie": {
        "cod": "biologie",
        "profile": ["Biologie vegetală și animală", "Anatomie și fiziologie umană"],
        "subiecte": ["Anatomie", "Genetică", "Ecologie"],
        "timp_minute": 180,
        "punctaj_total": 100,
    },
    "🏛️ Istorie": {
        "cod": "istorie",
        "profile": ["Umanist", "Pedagogic", "Teologic"],
        "subiecte": ["Istorie românească", "Istorie universală"],
        "timp_minute": 180,
        "punctaj_total": 100,
    },
    "🌍 Geografie": {
        "cod": "geografie",
        "profile": ["Profiluri umaniste"],
        "subiecte": ["Geografia României", "Geografia Europei", "Geografia lumii"],
        "timp_minute": 180,
        "punctaj_total": 100,
    },
    "💻 Informatică": {
        "cod": "informatica",
        "profile": ["Mate-Info intensiv C++", "Mate-Info intensiv Pascal"],
        "subiecte": ["Algoritmi", "Structuri de date", "Programare"],
        "timp_minute": 180,
        "punctaj_total": 100,
    },
}


def extract_text_from_photo(image_bytes: bytes, materie_label: str) -> str:
    import os
    tmp_path = None
    try:
        key = keys[st.session_state.get("key_index", 0)]
        gemini_client = genai.Client(api_key=key)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
            tmp.write(image_bytes)
            tmp_path = tmp.name
        gfile = gemini_client.files.upload(file=tmp_path, config=genai_types.UploadFileConfig(mime_type="image/jpeg"))
        poll = 0
        while str(gfile.state) in ("FileState.PROCESSING", "PROCESSING") and poll < 30:
            time.sleep(1)
            gfile = gemini_client.files.get(gfile.name)
            poll += 1
        if str(gfile.state) not in ("FileState.ACTIVE", "ACTIVE"):
            return "[Eroare: imaginea nu a putut fi procesată de Google]"
        prompt = (
            f"Ești un asistent care transcrie text scris de mână din lucrări de elevi la {materie_label}. "
            f"Transcrie EXACT tot ce este scris în imagine, inclusiv formule, simboluri matematice și calcule. "
            f"Păstrează structura (Subiectul I, II, III dacă există). "
            f"Dacă un cuvânt e greu de citit, transcrie-l cu [?]. "
            f"Nu adăuga nimic, nu corecta nimic — transcrie fidel."
        )
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[gfile, prompt]
        )
        try:
            gemini_client.files.delete(gfile.name)
        except Exception:
            pass
        return response.text.strip()
    except Exception as e:
        return f"[Eroare la citirea pozei: {e}]"
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def get_bac_prompt_ai(materie_label, materie_info, profil):
    subiecte_str = ", ".join(materie_info["subiecte"])
    return (
        f"Generează un subiect complet de BAC la {materie_label} ({profil}), "
        f"identic ca structură și dificultate cu subiectele oficiale din România.\n\n"
        f"STRUCTURĂ OBLIGATORIE:\n"
        f"- SUBIECTUL I (30 puncte): 5 itemi tip grilă/răspuns scurt\n"
        f"- SUBIECTUL II (30 puncte): 3-4 probleme de dificultate medie\n"
        f"- SUBIECTUL III (30 puncte): 1-2 probleme complexe / eseu structurat\n"
        f"- 10 puncte din oficiu\n\n"
        f"TEME: {subiecte_str}\n"
        f"TIMP: {materie_info['timp_minute']} minute\n\n"
        f"La final adaugă baremul:\n"
        f"[[BAREM_BAC]]\nSUBIECTUL I: ...\nSUBIECTUL II: ...\nSUBIECTUL III: ...\n[[/BAREM_BAC]]"
    )


def get_bac_correction_prompt(materie_label, subiect, raspuns_elev, from_photo=False):
    source_note = (
        "NOTĂ: Răspunsul a fost extras automat dintr-o fotografie a lucrării. "
        "Judecă după intenția elevului, nu după eventuale erori de OCR.\n\n"
        if from_photo else ""
    )
    if "Română" in materie_label:
        lang_rules = (
            "CORECTARE LIMBĂ ROMÂNĂ: ortografie, punctuație, acord gramatical, "
            "registru stilistic. Acordă până la 10 puncte bonus/penalizare.\n\n"
        )
    else:
        lang_rules = (
            f"LIMBAJ ȘTIINȚIFIC ({materie_label}): terminologie, notații, unități. "
            "Acordă până la 5 puncte bonus/penalizare.\n\n"
        )
    return (
        f"Ești examinator BAC România pentru {materie_label}.\n\n"
        f"{source_note}"
        f"SUBIECTUL:\n{subiect}\n\n"
        f"RĂSPUNSUL ELEVULUI:\n{raspuns_elev}\n\n"
        f"## 📊 Punctaj per subiect\n- Subiectul I: X/30\n- Subiectul II: X/30\n"
        f"- Subiectul III: X/30\n- Din oficiu: 10\n\n"
        f"## ✅ Ce a făcut bine\n[aspecte corecte]\n\n"
        f"## ❌ Greșeli și explicații\n[fiecare greșeală explicată]\n\n"
        f"## 🖊️ Calitatea limbii\n{lang_rules}"
        f"## 🎓 Nota finală\n**Nota: X/10**\n\n"
        f"## 💡 Recomandări pentru BAC\n[2-3 sfaturi concrete]\n\n"
        f"Fii constructiv, cald, dar riguros."
    )


def parse_bac_subject(response):
    barem = ""
    subject_text = response
    match = re.search(r"\[\[BAREM_BAC\]\](.*?)\[\[/BAREM_BAC\]\]", response, re.DOTALL)
    if match:
        barem = match.group(1).strip()
        subject_text = response[:match.start()].strip()
    return subject_text, barem


def format_timer(seconds_remaining):
    h = seconds_remaining // 3600
    m = (seconds_remaining % 3600) // 60
    s = seconds_remaining % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _get_bac_system_prompt(materie_label: str) -> str:
    """
    FIX: Helper to correctly build system prompt for BAC/homework/quiz functions.
    Previously: get_system_prompt(MATERII.get(bac_materie, mod_avansat=...))
    which passed mod_avansat as kwarg to dict.get() — a runtime TypeError.
    Now: correctly separates dict lookup from get_system_prompt kwargs.
    """
    materie_val = MATERII.get(materie_label)
    return get_system_prompt(
        materie_val,
        mod_avansat=st.session_state.get("mod_avansat", False),
    )


def run_bac_sim_ui():
    st.subheader("🎓 Simulare BAC")

    if not st.session_state.get("bac_active"):
        st.markdown(
            "<div style='background:linear-gradient(135deg,#667eea,#764ba2);"
            "color:white;padding:20px 24px;border-radius:12px;margin-bottom:20px'>"
            "<h4 style='margin:0 0 8px 0'>📋 Cum funcționează?</h4>"
            "<ul style='margin:0;padding-left:18px;line-height:1.8'>"
            "<li>Alegi materia, profilul și tipul de subiect</li>"
            "<li>Rezolvi în timp real cu cronometru opțional</li>"
            "<li>Primești corectare AI detaliată + barem</li>"
            "</ul></div>",
            unsafe_allow_html=True
        )

        col1, col2 = st.columns(2)
        with col1:
            bac_materie = st.selectbox("📚 Materia:", options=list(MATERII_BAC.keys()), key="bac_mat_sel")
            info = MATERII_BAC[bac_materie]
            bac_profil = st.selectbox("🎯 Profil:", options=info["profile"], key="bac_prof_sel")
        with col2:
            use_timer = st.checkbox(f"⏱️ Cronometru ({info['timp_minute']} min)", value=True, key="bac_timer")

        st.divider()
        col_s, col_b = st.columns(2)
        with col_s:
            if st.button("🚀 Generează subiect AI", type="primary", use_container_width=True):
                with st.spinner("📝 Se generează subiectul BAC..."):
                    prompt = get_bac_prompt_ai(bac_materie, info, bac_profil)
                    # FIX: _get_bac_system_prompt() separates MATERII.get() from get_system_prompt kwargs
                    full = "".join(run_chat_with_rotation(
                        [], [prompt],
                        system_prompt=_get_bac_system_prompt(bac_materie)
                    ))
                subject_text, barem = parse_bac_subject(full)

                st.session_state.update({
                    "bac_active":     True,
                    "bac_materie":    bac_materie,
                    "bac_profil":     bac_profil,
                    "bac_tip":        "🤖 Generat de AI",
                    "bac_subject":    subject_text,
                    "bac_barem":      barem,
                    "bac_raspuns":    "",
                    "bac_corectat":   False,
                    "bac_corectare":  "",
                    "bac_start_time": time.time() if use_timer else None,
                    "bac_timp_min":   info["timp_minute"],
                    "bac_use_timer":  use_timer,
                })
                st.rerun()
        with col_b:
            if st.button("↩️ Înapoi la chat", use_container_width=True):
                st.session_state.pop("bac_mode", None)
                st.rerun()
        return

    col_title, col_timer = st.columns([3, 1])
    with col_title:
        st.markdown(f"### {st.session_state.bac_materie} · {st.session_state.bac_profil}")
    with col_timer:
        if st.session_state.get("bac_use_timer") and st.session_state.get("bac_start_time"):
            elapsed = int(time.time() - st.session_state.bac_start_time)
            total   = st.session_state.bac_timp_min * 60
            left    = max(0, total - elapsed)
            pct     = left / total
            color   = "#2ecc71" if pct > 0.5 else ("#e67e22" if pct > 0.2 else "#e74c3c")
            st.markdown(
                f'<div style="background:{color};color:white;padding:8px 12px;'
                f'border-radius:8px;text-align:center;font-size:20px;font-weight:700">'
                f'⏱️ {format_timer(left)}</div>',
                unsafe_allow_html=True
            )
            if left == 0:
                st.warning("⏰ Timpul a expirat!")

    st.divider()

    with st.expander("📋 Subiectul", expanded=not st.session_state.bac_corectat):
        st.markdown(st.session_state.bac_subject)

    if not st.session_state.bac_corectat:
        st.markdown("### ✏️ Răspunsurile tale")
        tab_foto, tab_text = st.tabs(["📷 Fotografiază lucrarea", "⌨️ Scrie manual"])
        raspuns = st.session_state.get("bac_raspuns", "")

        with tab_foto:
            st.info(
                "📱 **Pe telefon:** fotografiază lucrarea.\n\n"
                "💻 **Pe calculator:** încarcă o poză din galerie.\n\n"
                "AI-ul va citi textul și va porni corectarea automat."
            )
            uploaded_photo = st.file_uploader(
                "Încarcă fotografia lucrării:",
                type=["jpg", "jpeg", "png", "webp", "heic"],
                key="bac_photo_upload",
            )

            if uploaded_photo:
                st.image(uploaded_photo, caption="Fotografia încărcată", use_container_width=True)
                if not st.session_state.get("bac_ocr_done"):
                    with st.spinner("🔍 Profesorul citește lucrarea..."):
                        img_bytes = uploaded_photo.read()
                        text_extras = extract_text_from_photo(img_bytes, st.session_state.bac_materie)
                    st.session_state.bac_raspuns   = text_extras
                    st.session_state.bac_ocr_done  = True
                    st.session_state.bac_from_photo = True
                    with st.spinner("📊 Se corectează lucrarea..."):
                        prompt = get_bac_correction_prompt(
                            st.session_state.bac_materie, st.session_state.bac_subject,
                            text_extras, from_photo=True
                        )
                        # FIX: _get_bac_system_prompt() used here too
                        corectare = "".join(run_chat_with_rotation(
                            [], [prompt],
                            system_prompt=_get_bac_system_prompt(st.session_state.bac_materie)
                        ))
                    st.session_state.bac_corectare = corectare
                    st.session_state.bac_corectat  = True
                    st.rerun()
                if st.session_state.get("bac_ocr_done"):
                    with st.expander("📄 Text extras din poză", expanded=False):
                        st.text(st.session_state.get("bac_raspuns", ""))

        with tab_text:
            raspuns = st.text_area(
                "Scrie rezolvarea completă:",
                value=st.session_state.get("bac_raspuns", ""),
                height=350,
                placeholder="Subiectul I:\n1. ...\n\nSubiectul II:\n...\n\nSubiectul III:\n...",
                key="bac_ans_input"
            )
            st.session_state.bac_raspuns    = raspuns
            st.session_state.bac_from_photo = False

            if st.button("🤖 Corectare AI", type="primary", use_container_width=True,
                         disabled=not raspuns.strip()):
                with st.spinner("📊 Se corectează lucrarea..."):
                    prompt = get_bac_correction_prompt(
                        st.session_state.bac_materie, st.session_state.bac_subject,
                        raspuns, from_photo=False
                    )
                    corectare = "".join(run_chat_with_rotation(
                        [], [prompt],
                        system_prompt=_get_bac_system_prompt(st.session_state.bac_materie)
                    ))
                st.session_state.bac_corectare = corectare
                st.session_state.bac_corectat  = True
                st.rerun()

        st.divider()
        col_barem, col_nou = st.columns(2)
        with col_barem:
            if st.session_state.get("bac_barem"):
                if st.button("📋 Arată Baremul", use_container_width=True):
                    st.session_state.bac_show_barem = not st.session_state.get("bac_show_barem", False)
                    st.rerun()
        with col_nou:
            if st.button("🔄 Subiect nou", use_container_width=True):
                for k in [k for k in list(st.session_state.keys()) if k.startswith("bac_")]:
                    st.session_state.pop(k, None)
                st.rerun()

        if st.session_state.get("bac_show_barem") and st.session_state.get("bac_barem"):
            with st.expander("📋 Barem de corectare", expanded=True):
                st.markdown(st.session_state.bac_barem)

    else:
        st.markdown("### 📊 Corectare AI")
        st.markdown(st.session_state.bac_corectare)
        if st.session_state.get("bac_barem"):
            with st.expander("📋 Barem"):
                st.markdown(st.session_state.bac_barem)
        st.divider()
        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button("🔄 Subiect nou", type="primary", use_container_width=True):
                for k in [k for k in list(st.session_state.keys()) if k.startswith("bac_")]:
                    st.session_state.pop(k, None)
                st.rerun()
        with col2:
            if st.button("✏️ Reîncerc același subiect", use_container_width=True):
                st.session_state.bac_corectat  = False
                st.session_state.bac_corectare = ""
                st.session_state.bac_raspuns   = ""
                if st.session_state.get("bac_use_timer"):
                    st.session_state.bac_start_time = time.time()
                st.rerun()
        with col3:
            if st.button("💬 Înapoi la chat", use_container_width=True):
                for k in [k for k in list(st.session_state.keys()) if k.startswith("bac_")]:
                    st.session_state.pop(k, None)
                st.session_state.pop("bac_mode", None)
                st.rerun()


# ============================================================
# === CORECTARE TEME ===
# ============================================================
def get_homework_correction_prompt(materie_label: str, text_tema: str, from_photo: bool = False) -> str:
    source_note = (
        "NOTĂ: Tema a fost extrasă dintr-o fotografie. Judecă după intenția elevului.\n\n"
        if from_photo else ""
    )
    if "Română" in materie_label:
        corectare_limba = (
            "## 🖊️ Corectare limbă și stil\n"
            "Ortografie (diacritice, cratimă), punctuație, acord gramatical, "
            "exprimare, coerență. Subliniază greșelile și explică regula corectă.\n\n"
        )
    else:
        corectare_limba = (
            f"## 🖊️ Limbaj și exprimare ({materie_label})\n"
            "Terminologie, notații, unități de măsură, raționament clar.\n\n"
        )
    return (
        f"Ești profesor de {materie_label} și corectezi tema unui elev de liceu.\n\n"
        f"{source_note}"
        f"TEMA ELEVULUI:\n{text_tema}\n\n"
        f"## ✅ Ce a făcut bine\n[aspecte corecte — fii specific]\n\n"
        f"## ❌ Greșeli de conținut\n[fiecare greșeală explicată cu varianta corectă]\n\n"
        f"{corectare_limba}"
        f"## 📊 Notă orientativă\n**Nota: X/10** — [justificare scurtă]\n\n"
        f"## 💡 Sfaturi pentru data viitoare\n[2-3 recomandări concrete]\n\n"
        f"Ton: cald, constructiv."
    )


def run_homework_ui():
    st.subheader("📚 Corectare Temă")

    if not st.session_state.get("hw_done"):
        col1, col2 = st.columns([2, 1])
        with col1:
            hw_materie = st.selectbox(
                "📚 Materia temei:",
                options=[m for m in MATERII.keys() if m != "🎓 Toate materiile"],
                key="hw_materie_sel"
            )
        with col2:
            st.markdown("<br>", unsafe_allow_html=True)
            st.caption("Profesorul se adaptează materiei.")

        st.divider()
        tab_foto, tab_text = st.tabs(["📷 Fotografiază tema", "⌨️ Scrie / lipește textul"])

        with tab_foto:
            st.info(
                "📱 **Pe telefon:** fotografiază caietul sau foaia de temă.\n\n"
                "💻 **Pe calculator:** încarcă o poză din galerie."
            )
            hw_photo = st.file_uploader(
                "Încarcă fotografia temei:",
                type=["jpg", "jpeg", "png", "webp", "heic"],
                key="hw_photo_upload",
            )
            if hw_photo and not st.session_state.get("hw_ocr_done"):
                st.image(hw_photo, caption="Fotografia încărcată", use_container_width=True)
                with st.spinner("🔍 Profesorul citește tema..."):
                    text_extras = extract_text_from_photo(hw_photo.read(), hw_materie)
                st.session_state.hw_text       = text_extras
                st.session_state.hw_ocr_done   = True
                st.session_state.hw_from_photo = True
                st.session_state.hw_materie    = hw_materie
                with st.spinner("📝 Se corectează tema..."):
                    prompt = get_homework_correction_prompt(hw_materie, text_extras, from_photo=True)
                    # FIX: _get_bac_system_prompt reused here for consistency
                    corectare = "".join(run_chat_with_rotation(
                        [], [prompt],
                        system_prompt=_get_bac_system_prompt(hw_materie)
                    ))
                st.session_state.hw_corectare = corectare
                st.session_state.hw_done      = True
                st.rerun()
            elif hw_photo and st.session_state.get("hw_ocr_done"):
                with st.expander("📄 Text extras din poză", expanded=False):
                    st.text(st.session_state.get("hw_text", ""))

        with tab_text:
            hw_text = st.text_area(
                "Lipește sau scrie textul temei:",
                value=st.session_state.get("hw_text", ""),
                height=300,
                placeholder="Scrie sau lipește tema aici...",
                key="hw_text_input"
            )
            st.session_state.hw_text = hw_text
            if st.button("📝 Corectează tema", type="primary",
                         use_container_width=True, disabled=not hw_text.strip()):
                st.session_state.hw_materie    = hw_materie
                st.session_state.hw_from_photo = False
                with st.spinner("📝 Se corectează tema..."):
                    prompt = get_homework_correction_prompt(hw_materie, hw_text, from_photo=False)
                    corectare = "".join(run_chat_with_rotation(
                        [], [prompt],
                        system_prompt=_get_bac_system_prompt(hw_materie)
                    ))
                st.session_state.hw_corectare = corectare
                st.session_state.hw_done      = True
                st.rerun()
    else:
        mat = st.session_state.get("hw_materie", "")
        src = "📷 din fotografie" if st.session_state.get("hw_from_photo") else "✏️ scrisă manual"
        st.caption(f"{mat} · temă {src}")
        if st.session_state.get("hw_from_photo") and st.session_state.get("hw_text"):
            with st.expander("📄 Text extras din poză", expanded=False):
                st.text(st.session_state.hw_text)
        st.markdown(st.session_state.hw_corectare)
        st.divider()
        col1, col2 = st.columns(2)
        with col1:
            if st.button("📚 Corectează altă temă", type="primary", use_container_width=True):
                for k in [k for k in list(st.session_state.keys()) if k.startswith("hw_")]:
                    st.session_state.pop(k, None)
                st.rerun()
        with col2:
            if st.button("💬 Înapoi la chat", use_container_width=True):
                for k in [k for k in list(st.session_state.keys()) if k.startswith("hw_")]:
                    st.session_state.pop(k, None)
                st.session_state.pop("homework_mode", None)
                st.rerun()


# === MOD QUIZ ===
NIVELE_QUIZ   = ["🟢 Ușor (gimnaziu)", "🟡 Mediu (liceu)", "🔴 Greu (BAC)"]
MATERII_QUIZ  = [m for m in list(MATERII.keys()) if m != "🎓 Toate materiile"]


def get_quiz_prompt(materie_label: str, nivel: str, materie_val: str) -> str:
    nivel_text = nivel.split(" ", 1)[1].strip("()")
    return f"""Generează un quiz de 5 întrebări la {materie_label} pentru nivel {nivel_text}.

REGULI STRICTE:
1. Generează EXACT 5 întrebări numerotate (1. 2. 3. 4. 5.)
2. Fiecare întrebare are 4 variante: A) B) C) D)
3. La finalul TUTUROR întrebărilor adaugă:

[[RASPUNSURI_CORECTE]]
1: X
2: X
3: X
4: X
5: X
[[/RASPUNSURI_CORECTE]]

4. Întrebările trebuie să fie clare și potrivite pentru nivel {nivel_text}.
5. Folosește LaTeX ($...$) pentru formule matematice.
6. NU da explicații acum — doar întrebările și răspunsurile corecte la final."""


def parse_quiz_response(response: str) -> tuple[str, dict]:
    correct = {}
    clean_response = response
    match = re.search(r'\[\[RASPUNSURI_CORECTE\]\](.*?)\[\[/RASPUNSURI_CORECTE\]\]', response, re.DOTALL)
    if not match:
        match = re.search(
            r'(?:raspunsuri\s*corecte)[:\s]*\n((?:\s*\d+\s*[:.)-]\s*[A-D].*\n?){3,})',
            response, re.IGNORECASE | re.DOTALL
        )
    if match:
        block_start = match.start()
        clean_response = response[:block_start].strip()
        raw_block = match.group(1) if match.lastindex and match.lastindex >= 1 else match.group(0)
        for line in raw_block.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(r'\*{0,2}(\d+)\*{0,2}\s*[:.)-]\s*\*{0,2}([A-D])\*{0,2}', line, re.IGNORECASE)
            if m:
                try:
                    q_num = int(m.group(1))
                    ans = m.group(2).upper()
                    if 1 <= q_num <= 10:
                        correct[q_num] = ans
                except ValueError:
                    pass
    if not correct:
        for m in re.finditer(
            r'(?:intrebarea|question)?\s*(\d+).*?r[a]spuns(?:ul)?\s*(?:corect)?\s*[:\s]+([A-D])\b',
            response, re.IGNORECASE
        ):
            try:
                q_num = int(m.group(1))
                ans = m.group(2).upper()
                if 1 <= q_num <= 10:
                    correct[q_num] = ans
            except ValueError:
                pass
    return clean_response, correct


def evaluate_quiz(user_answers: dict, correct_answers: dict) -> tuple[int, str]:
    score = sum(1 for q, a in user_answers.items() if correct_answers.get(q) == a)
    total = len(correct_answers)
    lines = []
    for q in sorted(correct_answers.keys()):
        user_ans    = user_answers.get(q, "—")
        correct_ans = correct_answers[q]
        if user_ans == correct_ans:
            lines.append(f"✅ **Întrebarea {q}**: {user_ans} — Corect!")
        else:
            lines.append(f"❌ **Întrebarea {q}**: ai răspuns **{user_ans}**, corect era **{correct_ans}**")
    if score == total:
        verdict = "🏆 Excelent! Nota 10!"
    elif score >= total * 0.8:
        verdict = "🌟 Foarte bine!"
    elif score >= total * 0.6:
        verdict = "👍 Bine, mai exersează puțin!"
    elif score >= total * 0.4:
        verdict = "📚 Trebuie să mai studiezi."
    else:
        verdict = "💪 Nu-ți face griji, încearcă din nou!"
    feedback = f"### Rezultat: {score}/{total} — {verdict}\n\n" + "\n\n".join(lines)
    return score, feedback


def run_quiz_ui():
    st.subheader("📝 Mod Examinare")

    if not st.session_state.get("quiz_active"):
        col1, col2 = st.columns(2)
        with col1:
            quiz_materie_label = st.selectbox("Materie:", options=MATERII_QUIZ, key="quiz_materie_select")
        with col2:
            quiz_nivel = st.selectbox("Nivel:", options=NIVELE_QUIZ, key="quiz_nivel_select")

        if st.button("🚀 Generează Quiz", type="primary", use_container_width=True):
            quiz_materie_val = MATERII[quiz_materie_label]
            with st.spinner("📝 Profesorul pregătește întrebările..."):
                prompt = get_quiz_prompt(quiz_materie_label, quiz_nivel, quiz_materie_val)
                full_resp = ""
                # FIX: get_system_prompt called correctly with separate kwargs
                for chunk in run_chat_with_rotation(
                    [], [prompt],
                    system_prompt=get_system_prompt(
                        quiz_materie_val,
                        mod_avansat=st.session_state.get("mod_avansat", False),
                    )
                ):
                    full_resp += chunk
            questions_text, correct = parse_quiz_response(full_resp)
            if len(correct) >= 3:
                st.session_state.quiz_active    = True
                st.session_state.quiz_questions = questions_text
                st.session_state.quiz_correct   = correct
                st.session_state.quiz_answers   = {}
                st.session_state.quiz_submitted = False
                st.session_state.quiz_materie   = quiz_materie_label
                st.session_state.quiz_nivel     = quiz_nivel
                st.rerun()
            else:
                st.error("❌ Nu am putut genera quiz-ul. Încearcă din nou.")
        return

    st.caption(f"📚 {st.session_state.quiz_materie} · {st.session_state.quiz_nivel}")
    st.markdown(st.session_state.quiz_questions)
    st.divider()

    if not st.session_state.quiz_submitted:
        st.markdown("**Alege răspunsurile tale:**")
        answers = {}
        for q_num in sorted(st.session_state.quiz_correct.keys()):
            answers[q_num] = st.radio(
                f"Întrebarea {q_num}:",
                options=["A", "B", "C", "D"],
                horizontal=True,
                key=f"quiz_ans_{q_num}",
                index=None
            )
        all_answered = all(v is not None for v in answers.values())
        col1, col2 = st.columns(2)
        with col1:
            if st.button("✅ Trimite răspunsurile", type="primary",
                         disabled=not all_answered, use_container_width=True):
                st.session_state.quiz_answers   = {k: v for k, v in answers.items() if v}
                st.session_state.quiz_submitted = True
                st.rerun()
        with col2:
            if st.button("🔄 Quiz nou", use_container_width=True):
                for k in ["quiz_active", "quiz_questions", "quiz_correct", "quiz_answers", "quiz_submitted"]:
                    st.session_state.pop(k, None)
                st.rerun()
    else:
        score, feedback = evaluate_quiz(st.session_state.quiz_answers, st.session_state.quiz_correct)
        st.markdown(feedback)
        st.divider()
        col1, col2 = st.columns(2)
        with col1:
            if st.button("🔄 Quiz nou", type="primary", use_container_width=True):
                for k in ["quiz_active", "quiz_questions", "quiz_correct", "quiz_answers", "quiz_submitted"]:
                    st.session_state.pop(k, None)
                st.rerun()
        with col2:
            if st.button("💬 Înapoi la chat", use_container_width=True):
                for k in ["quiz_active", "quiz_questions", "quiz_correct", "quiz_answers", "quiz_submitted", "quiz_mode"]:
                    st.session_state.pop(k, None)
                st.rerun()


def run_chat_with_rotation(history_obj, payload, system_prompt=None):
    MODEL_FALLBACKS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-1.5-flash"]
    active_prompt = system_prompt or st.session_state.get("system_prompt") or SYSTEM_PROMPT
    max_retries = max(len(keys) * 3, 6)
    last_error = None

    for attempt in range(max_retries):
        if st.session_state.key_index >= len(keys):
            st.session_state.key_index = 0
        current_key = keys[st.session_state.key_index]
        model_name = MODEL_FALLBACKS[min(attempt // max(len(keys), 1), len(MODEL_FALLBACKS) - 1)]

        try:
            gemini_client = genai.Client(api_key=current_key)
            gen_config = genai_types.GenerateContentConfig(
                system_instruction=active_prompt,
                safety_settings=[
                    genai_types.SafetySetting(category=s["category"], threshold=s["threshold"])
                    for s in safety_settings
                ],
            )

            history_new = []
            for msg in history_obj:
                history_new.append(
                    genai_types.Content(
                        role=msg["role"],
                        parts=[
                            genai_types.Part(text=p) if isinstance(p, str)
                            else genai_types.Part(file_data=genai_types.FileData(
                                file_uri=p.uri, mime_type=p.mime_type))
                            for p in (msg["parts"] if isinstance(msg["parts"], list) else [msg["parts"]])
                        ]
                    )
                )

            current_parts = []
            for p in (payload if isinstance(payload, list) else [payload]):
                if isinstance(p, str):
                    current_parts.append(genai_types.Part(text=p))
                elif hasattr(p, "uri"):
                    current_parts.append(genai_types.Part(file_data=genai_types.FileData(
                        file_uri=p.uri, mime_type=p.mime_type)))
                else:
                    current_parts.append(genai_types.Part(text=str(p)))

            all_contents = history_new + [genai_types.Content(role="user", parts=current_parts)]
            response_stream = gemini_client.models.generate_content_stream(
                model=model_name,
                contents=all_contents,
                config=gen_config,
            )

            chunks = []
            for chunk in response_stream:
                try:
                    if chunk.text:
                        chunks.append(chunk.text)
                except Exception:
                    continue

            if model_name != MODEL_FALLBACKS[0]:
                st.toast(f"ℹ️ Răspuns generat cu modelul de rezervă ({model_name})", icon="🔄")

            yield from chunks
            return

        except Exception as e:
            last_error = e
            error_msg = str(e)
            if "400" in error_msg:
                raise Exception(f"❌ Cerere invalidă (400): {error_msg}") from e
            if "503" in error_msg or "overloaded" in error_msg.lower() or "resource_exhausted" in error_msg.lower():
                wait = min(0.5 * (2 ** attempt), 5)
                st.toast("🐢 Server ocupat, reîncerc...", icon="⏳")
                time.sleep(wait)
                continue
            elif "429" in error_msg or "quota" in error_msg.lower() or "rate_limit" in error_msg.lower() or "API key not valid" in error_msg:
                st.toast(f"⚠️ Schimb cheia {st.session_state.key_index + 1}...", icon="🔄")
                st.session_state.key_index = (st.session_state.key_index + 1) % len(keys)
                time.sleep(0.5)
                continue
            else:
                raise e

    raise Exception(f"❌ Serviciul este indisponibil după {max_retries} încercări. {last_error or ''}")


# === UI PRINCIPAL ===
st.title("🎓 Profesor Liceu")

with st.sidebar:
    st.header("⚙️ Opțiuni")

    st.subheader("📚 Materie")
    materie_label = st.selectbox(
        "Alege materia:",
        options=list(MATERII.keys()),
        index=0,
        label_visibility="collapsed"
    )
    materie_selectata = MATERII[materie_label]

    # FIX: Separated all get_system_prompt kwargs — no longer nested inside dict.get()
    if st.session_state.get("materie_selectata") != materie_selectata:
        st.session_state.materie_selectata = materie_selectata
        st.session_state["_detected_subject"] = materie_selectata
        st.session_state.system_prompt = get_system_prompt(
            materie_selectata,
            pas_cu_pas=st.session_state.get("pas_cu_pas", False),
            mod_avansat=st.session_state.get("mod_avansat", False),
            mod_strategie=st.session_state.get("mod_strategie", False),
            mod_bac_intensiv=st.session_state.get("mod_bac_intensiv", False),
        )

    if materie_selectata:
        st.info(f"Focusat pe: **{materie_label}**")

    st.divider()

    dark_mode = st.toggle("🌙 Mod Întunecat", value=st.session_state.get("dark_mode", False))
    if dark_mode != st.session_state.get("dark_mode", False):
        st.session_state.dark_mode = dark_mode
        st.rerun()

    # FIX: All toggle handlers now call get_system_prompt with properly separated kwargs
    pas_cu_pas = st.toggle(
        "🔢 Explicație Pas cu Pas",
        value=st.session_state.get("pas_cu_pas", False),
        help="Profesorul va explica fiecare problemă detaliat, pas cu pas."
    )
    if pas_cu_pas != st.session_state.get("pas_cu_pas", False):
        st.session_state.pas_cu_pas = pas_cu_pas
        st.session_state.system_prompt = get_system_prompt(
            st.session_state.get("materie_selectata"),
            pas_cu_pas=pas_cu_pas,
            mod_avansat=st.session_state.get("mod_avansat", False),
            mod_strategie=st.session_state.get("mod_strategie", False),
            mod_bac_intensiv=st.session_state.get("mod_bac_intensiv", False),
        )
        st.toast("🔢 Mod Pas cu Pas activat!" if pas_cu_pas else "Mod normal activat.", icon="✅" if pas_cu_pas else "💬")
        st.rerun()

    if st.session_state.get("pas_cu_pas"):
        st.info("🔢 **Pas cu Pas activ** — fiecare problemă e explicată detaliat.", icon="📋")

    mod_strategie = st.toggle(
        "🧠 Explică-mi Strategia",
        value=st.session_state.get("mod_strategie", False),
        help="Profesorul explică CUM să gândești rezolvarea — logica, nu calculele."
    )
    if mod_strategie != st.session_state.get("mod_strategie", False):
        st.session_state.mod_strategie = mod_strategie
        st.session_state.system_prompt = get_system_prompt(
            st.session_state.get("materie_selectata"),
            pas_cu_pas=st.session_state.get("pas_cu_pas", False),
            mod_avansat=st.session_state.get("mod_avansat", False),
            mod_strategie=mod_strategie,
            mod_bac_intensiv=st.session_state.get("mod_bac_intensiv", False),
        )
        st.toast("🧠 Mod Strategie activat!" if mod_strategie else "Mod normal activat.", icon="✅" if mod_strategie else "💬")
        st.rerun()

    if st.session_state.get("mod_strategie"):
        st.info("🧠 **Strategie activ** — înveți să gândești, nu să copiezi.", icon="🗺️")

    mod_avansat = st.toggle(
        "⚡ Mod Avansat",
        value=st.session_state.get("mod_avansat", False),
        help="Răspunsuri scurte și dense — sari peste explicațiile de bază."
    )
    if mod_avansat != st.session_state.get("mod_avansat", False):
        st.session_state.mod_avansat = mod_avansat
        st.session_state.system_prompt = get_system_prompt(
            st.session_state.get("materie_selectata"),
            pas_cu_pas=st.session_state.get("pas_cu_pas", False),
            mod_avansat=mod_avansat,
            mod_strategie=st.session_state.get("mod_strategie", False),
            mod_bac_intensiv=st.session_state.get("mod_bac_intensiv", False),
        )
        st.toast("⚡ Mod Avansat activat!" if mod_avansat else "Mod normal activat.", icon="✅" if mod_avansat else "💬")
        st.rerun()

    if st.session_state.get("mod_avansat"):
        st.info("⚡ **Mod Avansat activ** — răspunsuri scurte, doar esențialul.", icon="🎯")

    mod_bac_intensiv = st.toggle(
        "🎓 Pregătire BAC Intensivă",
        value=st.session_state.get("mod_bac_intensiv", False),
        help="Focusat pe ce pică la BAC: tipare, punctaj, timp, teorie lipsă detectată automat."
    )
    if mod_bac_intensiv != st.session_state.get("mod_bac_intensiv", False):
        st.session_state.mod_bac_intensiv = mod_bac_intensiv
        st.session_state.system_prompt = get_system_prompt(
            st.session_state.get("materie_selectata"),
            pas_cu_pas=st.session_state.get("pas_cu_pas", False),
            mod_avansat=st.session_state.get("mod_avansat", False),
            mod_strategie=st.session_state.get("mod_strategie", False),
            mod_bac_intensiv=mod_bac_intensiv,
        )
        st.toast("🎓 Mod BAC Intensiv activat!" if mod_bac_intensiv else "Mod normal activat.", icon="✅" if mod_bac_intensiv else "💬")
        st.rerun()

    if st.session_state.get("mod_bac_intensiv"):
        st.info("🎓 **BAC Intensiv activ** — focusat pe ce pică la examen.", icon="📝")


    st.divider()

    if not st.session_state.get("_sb_online", True):
        st.markdown(
            '<div style="background:#e67e22;color:white;padding:8px 12px;'
            'border-radius:8px;font-size:13px;text-align:center;margin-bottom:8px">'
            '📴 Mod offline — datele sunt salvate local</div>',
            unsafe_allow_html=True
        )
    else:
        pending = len(st.session_state.get("_offline_queue", []))
        if pending:
            st.caption(f"☁️ {pending} mesaje în așteptare pentru sincronizare")

    st.divider()

    if st.button("🗑️ Șterge Istoricul", type="primary"):
        clear_history_db(st.session_state.session_id)
        st.session_state.messages = []
        st.rerun()

    enable_audio = st.checkbox("🔊 Voce", value=False)
    if enable_audio:
        voice_option = st.radio(
            "🎙️ Alege vocea:",
            options=["👨 Domnul Profesor (Emil)", "👩 Doamna Profesoară (Alina)"],
            index=0
        )
        selected_voice = VOICE_MALE_RO if "Emil" in voice_option else VOICE_FEMALE_RO
    else:
        selected_voice = VOICE_MALE_RO

    st.divider()
    st.header("📁 Materiale")

    uploaded_file = st.file_uploader(
        "Încarcă imagine, PDF sau document",
        type=["jpg", "jpeg", "png", "webp", "gif", "pdf"],
        help="Imaginile sunt analizate vizual. PDF-urile sunt citite integral."
    )
    media_content = None

    if uploaded_file:
        import os
        file_key  = f"_gfile_{uploaded_file.name}_{uploaded_file.size}"
        cached_gf = st.session_state.get(file_key)

        if cached_gf:
            try:
                gemini_client = genai.Client(api_key=keys[st.session_state.key_index])
                refreshed = gemini_client.files.get(cached_gf.name)
                if str(refreshed.state) in ("FileState.ACTIVE", "ACTIVE", "FileState.PROCESSING", "PROCESSING"):
                    media_content = refreshed
            except Exception:
                st.session_state.pop(file_key, None)
                cached_gf = None

        if not cached_gf:
            file_type = uploaded_file.type
            suffix_map = {
                "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
                "image/webp": ".webp", "image/gif": ".gif", "application/pdf": ".pdf",
            }
            suffix    = suffix_map.get(file_type, ".bin")
            mime_type = file_type
            is_image  = file_type.startswith("image/")
            spinner_text = "🖼️ Profesorul analizează imaginea..." if is_image else "📚 Se trimite documentul la AI..."
            try:
                tmp_path = None
                try:
                    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                        tmp.write(uploaded_file.getvalue())
                        tmp_path = tmp.name
                    gemini_client = genai.Client(api_key=keys[st.session_state.key_index])
                    with st.spinner(spinner_text):
                        gfile = gemini_client.files.upload(file=tmp_path, config=genai_types.UploadFileConfig(mime_type=mime_type))
                        poll = 0
                        while str(gfile.state) in ("FileState.PROCESSING", "PROCESSING") and poll < 60:
                            time.sleep(1)
                            gfile = gemini_client.files.get(gfile.name)
                            poll += 1
                    if gfile.state.name == "ACTIVE":
                        media_content = gfile
                        st.session_state[file_key] = gfile
                    else:
                        st.error(f"❌ Fișierul nu a putut fi procesat (stare: {gfile.state.name})")
                finally:
                    if tmp_path and os.path.exists(tmp_path):
                        os.unlink(tmp_path)
            except Exception as e:
                st.error(f"❌ Eroare la încărcarea fișierului: {e}")

        if media_content:
            if uploaded_file.type.startswith("image/"):
                st.image(uploaded_file, caption=f"🖼️ {uploaded_file.name}", use_container_width=True)
                st.success("✅ Imaginea e pe serverele Google — AI-ul o vede complet.")
            else:
                st.success(f"✅ **{uploaded_file.name}** încărcat ({uploaded_file.size // 1024} KB)")
                st.caption("📄 AI-ul poate citi și analiza tot conținutul documentului.")

            if st.button("🗑️ Elimină fișierul", use_container_width=True, key="remove_media"):
                gf = st.session_state.pop(file_key, None)
                if gf:
                    try:
                        gemini_client = genai.Client(api_key=keys[st.session_state.key_index])
                        gemini_client.files.delete(gf.name)
                    except Exception:
                        pass
                media_content = None
                st.rerun()

    st.divider()
    st.subheader("📝 Examinare & BAC")

    def _clear_all_modes():
        for k in list(st.session_state.keys()):
            if k.startswith("bac_") or k.startswith("hw_"):
                st.session_state.pop(k, None)
        for k in ["quiz_active", "quiz_questions", "quiz_correct", "quiz_answers", "quiz_submitted"]:
            st.session_state.pop(k, None)

    col_q, col_b = st.columns(2)
    with col_q:
        if st.button("🎯 Quiz rapid", use_container_width=True,
                     type="primary" if st.session_state.get("quiz_mode") else "secondary"):
            entering = not st.session_state.get("quiz_mode", False)
            _clear_all_modes()
            st.session_state.quiz_mode = entering
            st.session_state.pop("bac_mode", None)
            st.session_state.pop("homework_mode", None)
            st.rerun()
    with col_b:
        if st.button("🎓 Simulare BAC", use_container_width=True,
                     type="primary" if st.session_state.get("bac_mode") else "secondary"):
            entering = not st.session_state.get("bac_mode", False)
            _clear_all_modes()
            st.session_state.bac_mode = entering
            st.session_state.pop("quiz_mode", None)
            st.session_state.pop("homework_mode", None)
            st.rerun()

    if st.button("📚 Corectează Temă", use_container_width=True,
                 type="primary" if st.session_state.get("homework_mode") else "secondary"):
        entering = not st.session_state.get("homework_mode", False)
        _clear_all_modes()
        st.session_state.homework_mode = entering
        st.session_state.pop("quiz_mode", None)
        st.session_state.pop("bac_mode", None)
        st.rerun()

    st.divider()
    st.subheader("🕐 Conversații anterioare")

    if st.button("🔄 Conversație nouă", use_container_width=True):
        new_sid = generate_unique_session_id()
        register_session(new_sid)
        switch_session(new_sid)
        st.rerun()

    sessions = get_session_list(limit=15)
    current_sid = st.session_state.session_id
    for s in sessions:
        is_current = s["session_id"] == current_sid
        label   = f"{'▶ ' if is_current else ''}{s['preview']}"
        caption = f"{format_time_ago(s['last_active'])} · {s['msg_count']} mesaje"
        with st.container():
            col_btn, col_del = st.columns([5, 1])
            with col_btn:
                if st.button(
                    label,
                    key=f"sess_{s['session_id']}",
                    use_container_width=True,
                    type="primary" if is_current else "secondary",
                    help=caption,
                ):
                    if not is_current:
                        switch_session(s["session_id"])
                        st.rerun()
            with col_del:
                if st.button("🗑", key=f"del_{s['session_id']}", help="Șterge"):
                    clear_history_db(s["session_id"])
                    if is_current:
                        st.session_state.messages = []
                    st.rerun()

    st.divider()
    if st.checkbox("🔧 Debug Info", value=False):
        msg_count = len(st.session_state.get("messages", []))
        st.caption(f"📊 Mesaje în memorie: {msg_count}/{MAX_MESSAGES_IN_MEMORY}")
        st.caption(f"🔑 Cheie API activă: {st.session_state.key_index + 1}/{len(keys)}")
        st.caption(f"🆔 Sesiune: {st.session_state.session_id[:16]}...")


# === MAIN UI ===
if st.session_state.get("homework_mode"):
    run_homework_ui()
    st.stop()

if st.session_state.get("bac_mode"):
    run_bac_sim_ui()
    st.stop()

if st.session_state.get("quiz_mode"):
    run_quiz_ui()
    st.stop()

# === ÎNCĂRCARE MESAJE ===
if "messages" not in st.session_state or not st.session_state.messages:
    st.session_state.messages = load_history_from_db(st.session_state.session_id)

if st.session_state.get("pas_cu_pas"):
    st.markdown(
        '<div style="background:linear-gradient(135deg,#667eea,#764ba2);color:white;'
        'padding:10px 16px;border-radius:10px;margin-bottom:12px;font-size:14px;">'
        '🔢 <strong>Mod Pas cu Pas activ</strong> — '
        'Profesorul îți va explica fiecare problemă detaliat, cu motivația fiecărui pas.'
        '</div>',
        unsafe_allow_html=True
    )

for i, msg in enumerate(st.session_state.messages):
    with st.chat_message(msg["role"]):
        if msg["role"] == "assistant":
            render_message(msg["content"], dark_mode=st.session_state.get("dark_mode", False))
        else:
            st.markdown(msg["content"])

    if msg["role"] == "assistant" and i == len(st.session_state.messages) - 1:
        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button("🔄 Nu am înțeles", key="qa_reexplain", use_container_width=True):
                st.session_state["_quick_action"] = "reexplain"
                st.rerun()
        with col2:
            if st.button("✏️ Exercițiu similar", key="qa_similar", use_container_width=True):
                st.session_state["_quick_action"] = "similar"
                st.rerun()
        with col3:
            if st.button("🧠 Explică strategia", key="qa_strategy", use_container_width=True):
                st.session_state["_quick_action"] = "strategy"
                st.rerun()


TYPING_HTML = """
<div class="typing-indicator">
    <div class="typing-dots"><span></span><span></span><span></span></div>
    <span>Domnul Profesor scrie...</span>
</div>
"""

if st.session_state.get("_quick_action"):
    action = st.session_state.pop("_quick_action")
    action_prompts = {
        "reexplain": "Nu am înțeles explicația anterioară. Te rog să explici altfel — folosește o altă analogie sau un exemplu diferit din viața reală.",
        "similar":   "Generează un exercițiu similar cu cel de mai sus, cu date diferite, de dificultate puțin mai mare. Rezolvă-l complet după ce îl enunți.",
        "strategy":  "Explică-mi STRATEGIA pentru acest tip de problemă — cum recunosc că e acest tip, ce pași urmez în minte, ce capcane să evit. Fără calcule, doar gândirea."
    }
    injected = action_prompts.get(action, "")
    if injected:
        with st.chat_message("user"):
            st.markdown(injected)
        st.session_state.messages.append({"role": "user", "content": injected})
        save_message_with_limits(st.session_state.session_id, "user", injected)

        context_messages = get_context_for_ai(st.session_state.messages)
        history_obj = [
            {"role": "model" if m["role"] == "assistant" else "user", "parts": [m["content"]]}
            for m in context_messages
        ]

        with st.chat_message("assistant"):
            message_placeholder = st.empty()
            full_response = ""
            message_placeholder.markdown(TYPING_HTML, unsafe_allow_html=True)
            try:
                for text_chunk in run_chat_with_rotation(history_obj, [injected]):
                    full_response += text_chunk
                    message_placeholder.markdown(full_response + "▌")
                message_placeholder.empty()
                render_message(full_response, dark_mode=st.session_state.get("dark_mode", False))
                st.session_state.messages.append({"role": "assistant", "content": full_response})
                save_message_with_limits(st.session_state.session_id, "assistant", full_response)
            except Exception as e:
                st.error(f"❌ Eroare: {e}")
    st.stop()


if st.session_state.get("_suggested_question"):
    user_input = st.session_state.pop("_suggested_question")
    with st.chat_message("user"):
        st.markdown(user_input)
    st.session_state.messages.append({"role": "user", "content": user_input})
    save_message_with_limits(st.session_state.session_id, "user", user_input)

    _selector_materie = MATERII.get(st.session_state.get("materie_selectata", "🎓 Toate materiile"))
    if _selector_materie is None:
        _detected = detect_subject_from_text(user_input)
        if _detected and _detected != st.session_state.get("_detected_subject"):
            update_system_prompt_for_subject(_detected)
    else:
        if st.session_state.get("_detected_subject") != _selector_materie:
            update_system_prompt_for_subject(_selector_materie)

    context_messages = get_context_for_ai(st.session_state.messages)
    history_obj = [
        {"role": "model" if m["role"] == "assistant" else "user", "parts": [m["content"]]}
        for m in context_messages
    ]

    with st.chat_message("assistant"):
        message_placeholder = st.empty()
        full_response = ""
        message_placeholder.markdown(TYPING_HTML, unsafe_allow_html=True)
        try:
            for text_chunk in run_chat_with_rotation(history_obj, [user_input]):
                full_response += text_chunk
                message_placeholder.markdown(full_response + "▌")
            message_placeholder.empty()
            render_message(full_response, dark_mode=st.session_state.get("dark_mode", False))
            st.session_state.messages.append({"role": "assistant", "content": full_response})
            save_message_with_limits(st.session_state.session_id, "assistant", full_response)
        except Exception as e:
            st.error(f"❌ Eroare: {e}")
    st.rerun()


# === ÎNTREBĂRI SUGERATE ===
import random as _random

INTREBARI_POOL = {
    None: [
        "Explică-mi cum se rezolvă ecuațiile de gradul 2",
        "Ce este fotosinteza și cum funcționează?",
        "Cum se scrie un eseu la BAC?",
        "Explică legea lui Ohm cu un exemplu",
        "Care sunt curentele literare studiate la BAC?",
        "Cum calculez probabilitatea unui eveniment?",
        "Explică-mi structura atomului",
        "Ce este derivata și la ce folosește?",
        "Cum rezolv o problemă cu mișcare uniformă?",
        "Explică-mi reacțiile chimice de bază",
        "Care sunt figurile de stil principale?",
        "Cum funcționează circuitul electric serie vs paralel?",
    ],
    "matematică": [
        "Cum rezolv o ecuație de gradul 2?",
        "Explică-mi derivatele — ce sunt și cum se calculează",
        "Cum calculez aria și volumul unui corp geometric?",
        "Ce este limita unui șir și cum o calculez?",
        "Cum rezolv un sistem de ecuații?",
        "Explică-mi funcțiile monotone și extreme",
        "Ce este matricea și cum fac operații cu ea?",
        "Cum calculez probabilități cu combinări?",
        "Explică-mi trigonometria — formule esențiale",
        "Cum rezolv inecuații de gradul 2?",
    ],
    "fizică": [
        "Explică legile lui Newton cu exemple",
        "Cum rezolv o problemă cu plan înclinat?",
        "Ce este legea lui Ohm și cum aplic în circuit?",
        "Explică reflexia și refracția luminii",
        "Cum calculez energia cinetică și potențială?",
        "Explică mișcarea uniform accelerată — formule",
        "Ce este câmpul electric și cum funcționează?",
        "Cum rezolv o problemă cu circuite mixte?",
    ],
    "chimie": [
        "Cum echilibrez o ecuație chimică?",
        "Explică-mi legăturile chimice (ionică, covalentă)",
        "Cum calculez concentrația molară?",
        "Ce este regula lui Markovnikov?",
        "Cum fac calcule stoechiometrice?",
        "Ce sunt acizii și bazele — teoria Arrhenius",
    ],
    "limba și literatura română": [
        "Cum structurez un eseu de BAC la Română?",
        "Explică-mi curentele literare principale",
        "Cum analizez o poezie — figuri de stil, prozodie",
        "Care sunt operele obligatorii la BAC Română?",
        "Explică-mi romanul Ion de Rebreanu",
        "Cum caracterizez un personaj literar?",
    ],
    "biologie": [
        "Explică-mi mitoza vs meioza",
        "Cum funcționează fotosinteza și respirația celulară?",
        "Ce este ADN-ul și cum funcționează codul genetic?",
        "Explică-mi legile lui Mendel cu pătrat Punnett",
        "Care sunt organitele celulei și funcțiile lor?",
    ],
    "informatică": [
        "Explică algoritmul de sortare prin selecție în C++",
        "Ce este recursivitatea? Exemplu cu factorial",
        "Cum funcționează căutarea binară?",
        "Explică-mi backtracking-ul cu un exemplu simplu",
        "Ce este complexitatea unui algoritm O(n)?",
    ],
    "geografie": [
        "Care sunt unitățile de relief ale României?",
        "Explică-mi clima României — regiuni și factori",
        "Care sunt râurile principale din România?",
        "Explică-mi Delta Dunării — caracteristici",
    ],
    "istorie": [
        "Explică Marea Unire din 1918 — cauze și consecințe",
        "Care au fost reformele lui Alexandru Ioan Cuza?",
        "Explică-mi perioada comunistă în România",
        "Ce s-a întâmplat la Revoluția din 1989?",
    ],
    "limba franceză": [
        "Explică-mi Passé Composé vs Imparfait",
        "Cum se acordă participiul trecut cu avoir și être?",
        "Explică Subjonctivul — când și cum se folosește",
    ],
    "limba engleză": [
        "Explică Present Perfect vs Past Simple",
        "Cum funcționează propozițiile condiționale (tip 1, 2, 3)?",
        "Explică vocea pasivă în engleză",
    ],
}

if not st.session_state.get("messages"):
    materie_curenta = st.session_state.get("materie_selectata")
    pool = INTREBARI_POOL.get(materie_curenta, INTREBARI_POOL[None])
    _seed_key = f"_sugg_seed_{st.session_state.session_id}"
    if _seed_key not in st.session_state:
        st.session_state[_seed_key] = _random.randint(0, 10000)
    _rng = _random.Random(st.session_state[_seed_key])
    intrebari = _rng.sample(pool, min(4, len(pool)))

    st.markdown("##### 💡 Cu ce începem azi?")
    cols = st.columns(2)
    for i, intrebare in enumerate(intrebari):
        with cols[i % 2]:
            if st.button(intrebare, key=f"sugg_{i}", use_container_width=True):
                st.session_state["_suggested_question"] = intrebare
                st.rerun()


# === CHAT INPUT ===
if user_input := st.chat_input("Întreabă profesorul..."):
    now_ts   = time.time()
    last_msg = st.session_state.get("_last_user_msg", "")
    last_ts  = st.session_state.get("_last_msg_ts", 0)

    if user_input.strip() == last_msg.strip() and (now_ts - last_ts) < 2.5:
        st.toast("⏳ Mesaj duplicat ignorat.", icon="🔁")
        st.stop()

    st.session_state["_last_user_msg"] = user_input
    st.session_state["_last_msg_ts"]   = now_ts

    with st.chat_message("user"):
        st.markdown(user_input)
    st.session_state.messages.append({"role": "user", "content": user_input})
    save_message_with_limits(st.session_state.session_id, "user", user_input)

    # Detecție automată materie
    _selector_materie = MATERII.get(st.session_state.get("materie_selectata", "🎓 Toate materiile"))
    if _selector_materie is None:
        _detected = detect_subject_from_text(user_input)
        if _detected and _detected != st.session_state.get("_detected_subject"):
            update_system_prompt_for_subject(_detected)
            st.toast(f"📚 Materie detectată: {_detected.capitalize()}", icon="🎯")
    else:
        if st.session_state.get("_detected_subject") != _selector_materie:
            update_system_prompt_for_subject(_selector_materie)

    context_messages = get_context_for_ai(st.session_state.messages)
    history_obj = [
        {"role": "model" if m["role"] == "assistant" else "user", "parts": [m["content"]]}
        for m in context_messages
    ]

    final_payload = []
    if media_content:
        fname  = uploaded_file.name if uploaded_file else ""
        ftype  = (uploaded_file.type if uploaded_file else "") or ""
        if ftype.startswith("image/"):
            final_payload.append(
                "Elevul ți-a trimis o imagine. Analizează-o vizual complet: "
                "descrie ce vezi și răspunde la întrebarea elevului."
            )
        else:
            final_payload.append(
                f"Elevul ți-a trimis documentul '{fname}'. "
                "Citește și analizează tot conținutul înainte de a răspunde."
            )
        final_payload.append(media_content)
    final_payload.append(user_input)

    with st.chat_message("assistant"):
        message_placeholder = st.empty()
        full_response = ""
        message_placeholder.markdown(TYPING_HTML, unsafe_allow_html=True)

        try:
            stream_generator = run_chat_with_rotation(history_obj, final_payload)
            first_chunk = True

            for text_chunk in stream_generator:
                full_response += text_chunk
                first_chunk = False
                if "<svg" in full_response or ("<path" in full_response and "stroke=" in full_response):
                    message_placeholder.markdown(
                        full_response.split("<path")[0] + "\n\n*🎨 Domnul Profesor desenează...*\n\n▌"
                    )
                else:
                    message_placeholder.markdown(full_response + "▌")

            message_placeholder.empty()
            render_message(full_response, dark_mode=st.session_state.get("dark_mode", False))

            st.session_state.messages.append({"role": "assistant", "content": full_response})
            save_message_with_limits(st.session_state.session_id, "assistant", full_response)

            if enable_audio:
                with st.spinner("🎙️ Domnul Profesor vorbește..."):
                    audio_file = generate_professor_voice(full_response, selected_voice)
                    if audio_file:
                        st.audio(audio_file, format='audio/mp3')
                    else:
                        st.caption("🔇 Nu am putut genera vocea pentru acest răspuns.")

        except Exception as e:
            st.error(f"❌ Eroare: {e}")
