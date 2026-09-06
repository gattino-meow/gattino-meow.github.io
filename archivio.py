import os
import re
import json
import time
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor

# --- CONFIGURAZIONE ---
CHANNELS_JS_URL = "https://guidatv-omega.vercel.app/channels.js"
BASE_GUIDES_URL = "https://guidatv-omega.vercel.app/output/guides/"
OUTPUT_DIR = "Archivio"
INDEX_FILENAME = "index_structure.json" 
MAX_WORKERS = 30
MAX_RETRIES = 5

# Dimensione del blocco dei giorni (puoi impostarlo a 14, 45, 75, ecc.)
BLOCK_DAYS = 120

def sanitize_filename(name):
    name = re.sub(r'[\\/*?:"<>|]', '', name)
    return name.strip()

# --- GESTIONE DEI BLOCCHI DI GIORNI ---

def get_day_block(date_str):
    """Calcola il blocco di giorni (basato su BLOCK_DAYS) a cui appartiene una data."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    doy = dt.timetuple().tm_yday  # Giorno dell'anno (1-365/366)
    block_idx = (doy - 1) // BLOCK_DAYS
    
    start_dt = dt.replace(month=1, day=1) + timedelta(days=block_idx * BLOCK_DAYS)
    end_dt = start_dt + timedelta(days=BLOCK_DAYS - 1)
    
    # Impedisce lo sforamento nell'anno successivo
    if end_dt.year > start_dt.year:
        end_dt = start_dt.replace(month=12, day=31)
        
    start_str = start_dt.strftime("%Y-%m-%d")
    end_str = end_dt.strftime("%Y-%m-%d")
    
    return f"{start_str}_to_{end_str}.txt"

def parse_block_file(text):
    """Divide il file a blocchi in un dizionario { 'YYYY-MM-DD': 'Testo del giorno' }."""
    days = {}
    pattern = r'={50}\n📅 DATA: (\d{4}-\d{2}-\d{2})\n={50}\n'
    parts = re.split(pattern, text)
    
    for i in range(1, len(parts), 2):
        date_str = parts[i]
        content = parts[i+1].strip()
        days[date_str] = content
    return days

def save_block_file(file_path, days_dict):
    """Salva il dizionario dei giorni nel formato a blocchi con il separatore grafico."""
    final_text = []
    for date_str in sorted(days_dict.keys()):
        header = f"{'='*50}\n📅 DATA: {date_str}\n{'='*50}\n"
        final_text.append(header + days_dict[date_str] + "\n\n")
        
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write("".join(final_text).strip() + "\n")

# --- MIGRAZIONE E RIORGANIZZAZIONE ARCHIVIO ---

def compact_old_files():
    """
    Riorganizza l'archivio eliminando le sottocartelle Anno/Mese e i vecchi file.
    Inoltre, se si cambia BLOCK_DAYS, ripartiziona automaticamente i giorni dei vecchi blocchi
    nei nuovi intervalli corretti. Sposta tutto direttamente sotto Archivio/Canale/.
    """
    print("Verifica e riorganizzazione della struttura dell'archivio...")
    files_migrated = 0
    
    for root, dirs, files in os.walk(OUTPUT_DIR, topdown=False):
        rel_path = os.path.relpath(root, OUTPUT_DIR)
        if rel_path == ".":
            continue
            
        parts = rel_path.split(os.sep)
        channel_name = parts[0]
        target_folder = os.path.join(OUTPUT_DIR, channel_name)
        
        for file in files:
            if not file.endswith('.txt'):
                continue
            
            file_path = os.path.join(root, file)
            
            # Caso 1: Vecchio file singolo (es. 2025-07-08.txt)
            if re.match(r'^\d{4}-\d{2}-\d{2}\.txt$', file):
                date_str = file[:10]
                correct_filename = get_day_block(date_str)
                
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                content = re.sub(r'^Guida TV per .*? - \d{4}-\d{2}-\d{2}\n={50}\n', '', content).strip()
                
                os.makedirs(target_folder, exist_ok=True)
                new_file_path = os.path.join(target_folder, correct_filename)
                
                block_days = {}
                if os.path.exists(new_file_path):
                    with open(new_file_path, 'r', encoding='utf-8') as f:
                        block_days = parse_block_file(f.read())
                        
                block_days[date_str] = content
                save_block_file(new_file_path, block_days)
                os.remove(file_path)
                files_migrated += 1
                
            # Caso 2: Qualsiasi file a blocchi (es. 2025-07-01_to_2025-07-14.txt)
            elif re.match(r'^\d{4}-\d{2}-\d{2}_to_\d{4}-\d{2}-\d{2}\.txt$', file):
                with open(file_path, 'r', encoding='utf-8') as f:
                    file_content = f.read()
                
                incoming_days = parse_block_file(file_content)
                if not incoming_days:
                    os.remove(file_path)
                    continue
                
                # Prende una data interna per controllare se corrisponde al blocco calcolato con il BLOCK_DAYS attuale
                sample_date = list(incoming_days.keys())[0]
                correct_filename = get_day_block(sample_date)
                
                is_nested = len(parts) > 1
                is_wrong_block_size = (file != correct_filename)
                
                if is_nested or is_wrong_block_size:
                    for d_str, d_content in incoming_days.items():
                        target_filename = get_day_block(d_str)
                        os.makedirs(target_folder, exist_ok=True)
                        new_file_path = os.path.join(target_folder, target_filename)
                        
                        block_days = {}
                        if os.path.exists(new_file_path):
                            with open(new_file_path, 'r', encoding='utf-8') as f:
                                block_days = parse_block_file(f.read())
                                
                        block_days[d_str] = d_content
                        save_block_file(new_file_path, block_days)
                    
                    os.remove(file_path)
                    files_migrated += 1
                    
        try:
            if not os.listdir(root):
                os.rmdir(root)
        except:
            pass
            
    if files_migrated > 0:
        print(f"Struttura semplificata. Spostati e ripartizionati {files_migrated} file.")

# --- UTILITY CORE E AGGIORNAMENTO INTELLIGENTE ---

def get_channels_list():
    print(f"Scarico la lista canali da: {CHANNELS_JS_URL}")
    try:
        resp = requests.get(CHANNELS_JS_URL)
        resp.raise_for_status()
        content = resp.text
        
        channels = []
        matches = re.finditer(r'\{(.*?)\}', content, re.DOTALL)
        
        for match in matches:
            block = match.group(1)
            def extract(key):
                m = re.search(rf'["\']?{key}["\']?\s*:\s*["\'](.*?)["\']', block)
                return m.group(1) if m else None

            name = extract("name")
            site_id = extract("site_id")
            alt = extract("alt")
            
            if name and site_id:
                channels.append({"name": name, "site_id": site_id, "json_suffix": ""})
                if alt == "Y":
                    channels.append({"name": f"{name} (Fonte alternativa)", "site_id": site_id, "json_suffix": "ALT"})
        
        print(f"Trovati {len(channels)} canali nel file JS.")
        return channels
    except Exception as e:
        print(f"Errore critico durante l'analisi del file canali: {e}")
        return []

def get_synopsis(p): return p.get('eventSynopsis') or p.get('description') or p.get('synopsis') or ''

def get_best_title(program):
    main_title = program.get('eventTitle') or program.get('title') or ''
    epg_title = program.get('epgEventTitle') or ''
    content_obj = program.get('content', {})
    content_title = content_obj.get('contentTitle') if isinstance(content_obj, dict) else ''
    
    candidates = [t for t in [epg_title, content_title, main_title] if t]
    sky_regex = re.compile(r'\bS\d+\s*E?p?\d+', re.IGNORECASE)
    sky_format_title = next((t for t in candidates if sky_regex.search(t)), None)
    
    if sky_format_title:
        return sky_format_title
    
    best = main_title
    for c in candidates:
        if len(c) > len(best):
            best = c
    return best if best else "Titolo sconosciuto"

def format_time(iso_date_str):
    try:
        if iso_date_str.endswith('Z'):
            iso_date_str = iso_date_str.replace('Z', '+00:00')
        dt = datetime.fromisoformat(iso_date_str)
        if dt.tzinfo is not None:
            dt = dt.astimezone(ZoneInfo("Europe/Rome"))
        return dt.strftime('%H:%M')
    except:
        return "??"

def analyze_schedule_text(text):
    blocks = text.split('-' * 50)
    events = []
    for block in blocks:
        lines = [l.strip() for l in block.strip().split('\n') if l.strip()]
        t_line = next((l for l in lines if re.match(r'^.{2,5}\s*-\s*.{2,5}\s*-', l)), None)
        if t_line:
            events.append({'title_line': t_line, 'has_synopsis': len(lines) > lines.index(t_line) + 1})
    return events

def should_update(old_text, new_text):
    if old_text == new_text: return False
    old_e, new_e = analyze_schedule_text(old_text), analyze_schedule_text(new_text)
    if not old_e or len(new_e) > len(old_e): return True
    if len(new_e) < len(old_e): return False
    if [e['title_line'] for e in old_e] != [e['title_line'] for e in new_e]: return True
    old_syn = sum(1 for e in old_e if e['has_synopsis'])
    new_syn = sum(1 for e in new_e if e['has_synopsis'])
    return new_syn >= old_syn

# ------------------------------------------------

def process_channel(channel):
    site_id, suffix, raw_name = channel['site_id'], channel['json_suffix'], channel['name']
    safe_name = sanitize_filename(raw_name)
    json_url = f"{BASE_GUIDES_URL}{site_id}{suffix}.json"
    
    data = None
    for _ in range(MAX_RETRIES):
        try:
            resp = requests.get(json_url, timeout=10)
            if resp.status_code == 404: return f"SKIP: {safe_name}"
            resp.raise_for_status()
            data = resp.json()
            break 
        except:
            time.sleep(1)
            continue
    
    if not data or 'events_by_date' not in data:
        return f"ERR: {safe_name} (Dati non disponibili)"

    try:
        blocks_data = {} # filename -> { date_str: new_content }
        days_updated = 0
        days_created = 0
        
        folder = os.path.join(OUTPUT_DIR, safe_name)
        
        # Data di riferimento odierna per calcolare i 20 giorni di limite
        today = datetime.now(ZoneInfo("Europe/Rome")).date()
        
        # 1. Genera il testo giornaliero raggruppando per file a blocchi
        for date_str, programs in data['events_by_date'].items():
            try: datetime.strptime(date_str, "%Y-%m-%d")
            except: continue
            
            programs.sort(key=lambda x: x.get('starttime') or x.get('start') or '')
            lines = []
            for prog in programs:
                start, end = prog.get('starttime') or prog.get('start'), prog.get('endtime') or prog.get('end')
                if not start or not end: continue
                lines.append(f"{format_time(start)} - {format_time(end)} - {get_best_title(prog)}")
                syn = get_synopsis(prog)
                if syn: lines.append(syn)
                lines.append("-" * 50)
            
            day_text = "\n".join(lines)
            filename = get_day_block(date_str)
            
            if filename not in blocks_data:
                blocks_data[filename] = {}
            blocks_data[filename][date_str] = day_text

        # 2. Aggiorna in modo intelligente i file a blocchi esistenti (con blocco dei 20 giorni)
        for filename, days_dict in blocks_data.items():
            file_path = os.path.join(folder, filename)
            os.makedirs(folder, exist_ok=True)
            
            existing_days = {}
            if os.path.exists(file_path):
                with open(file_path, 'r', encoding='utf-8') as f:
                    existing_days = parse_block_file(f.read())
            
            file_changed = False
            for date_str, new_day_text in days_dict.items():
                try:
                    date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
                    is_old_day = (today - date_obj).days > 20
                except:
                    is_old_day = False

                if date_str not in existing_days:
                    # Se il giorno non esiste ancora sull'archivio, lo scrive (per evitare buchi)
                    existing_days[date_str] = new_day_text
                    file_changed = True
                    days_created += 1
                else:
                    # Se il giorno esiste ed è più vecchio di 20 giorni, lo ignora (rimane così com'è)
                    if is_old_day:
                        continue
                    
                    # Altrimenti, verifica la presenza di aggiornamenti validi
                    if should_update(existing_days[date_str], new_day_text):
                        existing_days[date_str] = new_day_text
                        file_changed = True
                        days_updated += 1
            
            if file_changed:
                save_block_file(file_path, existing_days)
            
        return f"OK: {safe_name} ({days_created} giorni creati, {days_updated} aggiornati)"
    except Exception as e:
        return f"ERR: {safe_name} ({e})"

def generate_index_json():
    print("Aggiornamento indice...")
    
    index = {
        "_meta": {
            "last_saved": datetime.now(ZoneInfo("Europe/Rome")).isoformat()
        }
    }
    
    for entry in os.scandir(OUTPUT_DIR):
        if entry.is_dir():
            channel_name = entry.name
            txt_files = sorted([
                f.name for f in os.scandir(entry.path)
                if f.is_file() and f.name.endswith('.txt')
            ])
            if txt_files:
                index[channel_name] = txt_files

    with open(INDEX_FILENAME, 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
        
def main():
    if not os.path.exists(OUTPUT_DIR): os.makedirs(OUTPUT_DIR)
    
    compact_old_files()
    
    channels = get_channels_list()
    if not channels: return

    print(f"Elaborazione in corso con {MAX_WORKERS} thread...")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        results = list(executor.map(process_channel, channels))
        for r in results:
            if "OK" in r and "(0 giorni creati, 0 aggiornati)" not in r: print(r)
            elif "ERR" in r: print(r)

    generate_index_json()
    print("Aggiornamento indice completato. Finito.")

if __name__ == "__main__":
    main()
