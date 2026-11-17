"""
Modulo per il controllo e la verifica delle fatture ortofrutticole in PDF.
Rileva errori matematici, virgole spostate (fattore 10x), scambi colli/pezzi e anomalie di prezzo.
Assegna a ogni anomalia un punteggio di attendibilità dell'errore (0% - 100%).

Supporta:
- Prezzi differenziati per Unità di Misura (KG vs N./Colli vs PZ).
- Finestra Temporale Mobile Continua (Rolling Window di 30 giorni) per gestire la stagionalità
  e le fluttuazioni naturali del mercato senza scalini rigidi a fine mese.
- Report non sovrascritti (timestamped) con ordinamento decrescente per gravità.
- Dashboard HTML interattiva e CSV dedicato alle sole anomalie.
"""

import os
import glob
import json
import csv
import re
import statistics
from datetime import datetime, date, timedelta
from typing import Dict, List, Any, Optional, Tuple
import fitz  # PyMuPDF

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR_FATTURE = os.path.join(BASE_DIR, "Fatture-da-controllare")
DIR_REPORT = os.path.join(BASE_DIR, "report")
DIR_DATA = os.path.join(BASE_DIR, "data")
FILE_RANGE = os.path.join(DIR_DATA, "range_prezzi.json")
FILE_STORICO = os.path.join(DIR_DATA, "storico_prezzi.csv")

FINESTRA_GIORNI_DEFAULT = 30

def to_float(val_str: Any) -> float:
    if val_str is None:
        return 0.0
    if isinstance(val_str, (int, float)):
        return float(val_str)
    s = str(val_str).strip()
    if not s:
        return 0.0
    if ',' in s:
        return float(s.replace('.', '').replace(',', '.'))
    return float(s)

def parse_date(date_str: str) -> Optional[date]:
    """Converte una data stringa GG/MM/AA o GG/MM/AAAA in datetime.date."""
    if not date_str:
        return None
    d_clean = date_str.strip()
    for fmt in ("%d/%m/%y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(d_clean, fmt).date()
        except ValueError:
            pass
    return None

def normalizza_nome(desc: str) -> str:
    """Estrae la radice del prodotto (es. 'LATTUGA ROMANA', 'BANANE CHIQUITA', 'POMODORI GIANCALA')."""
    desc_pulita = re.sub(r'^(?:\d+\s+)+', '', desc).strip()
    desc_pulita = re.sub(r'\s+(?:IT|ES|FR|EC|IL|XX|NL|GR)$', '', desc_pulita).strip()
    parole = desc_pulita.split()
    if len(parole) >= 2:
        return f"{parole[0]} {parole[1]}".lower()
    elif len(parole) == 1:
        return parole[0].lower()
    return desc_pulita.lower()

def carica_range_prezzi() -> Dict[str, Any]:
    if os.path.exists(FILE_RANGE):
        try:
            with open(FILE_RANGE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def salva_range_prezzi(dati: Dict[str, Any]):
    os.makedirs(DIR_DATA, exist_ok=True)
    with open(FILE_RANGE, 'w', encoding='utf-8') as f:
        json.dump(dati, f, indent=2, ensure_ascii=False)

import sys
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

def stampa_barra(attuale: int, totale: int, prefisso: str = "", larghezza: int = 25):
    """Stampa una barra di avanzamento visuale compatibile con qualsiasi console Windows."""
    if totale <= 0:
        return
    pct = int((attuale / totale) * 100)
    pieni = int((larghezza * attuale) / totale)
    vuoti = larghezza - pieni
    barra = "=" * pieni + "-" * vuoti
    sys.stdout.write(f"\r {prefisso} [{barra}] {pct}% ({attuale:,}/{totale:,})")
    sys.stdout.flush()


def estrai_righe_da_pdf(file_pdf: str, mostra_progresso: bool = True) -> List[Dict[str, Any]]:
    import sys
    doc = fitz.open(file_pdf)
    righe = []
    tot_pagine = len(doc)
    nome_file = os.path.basename(file_pdf)
    
    current_fat_num = "N/D"
    current_fat_data = "N/D"
    current_cliente = "N/D"

    for page_idx, page in enumerate(doc):
        if mostra_progresso and (page_idx % 25 == 0 or page_idx == tot_pagine - 1):
            stampa_barra(page_idx + 1, tot_pagine, prefisso=f"Lettura {nome_file[:20]}:")

        text = page.get_text('text')
        lines = text.split('\n')
        
        m_fat = re.search(r'Fattura N\.\s+(\d+)\s+del\s+(\d{2}/\d{2}/\d{2})', text)
        if m_fat:
            current_fat_num = m_fat.group(1)
            current_fat_data = m_fat.group(2)
        
        m_cli = re.search(r'Spett\.le\s*\n\s*([^\n]+)', text)
        if m_cli:
            current_cliente = m_cli.group(1).strip()

        in_table = False
        current_desc_lines = []
        pz_info = None

        for line in lines:
            l_strip = line.strip()
            if 'Art.' in l_strip and 'Descrizione' in l_strip and 'Prezzo' in l_strip:
                in_table = True
                current_desc_lines = []
                continue
            if not in_table:
                continue
            if any(term in l_strip for term in ['Tot.colli', 'Cod. e descrizione IVA', 'Documento in Euro']):
                in_table = False
                continue
            if 'Non staccare il foglio' in l_strip or 'Segue' in l_strip or l_strip.startswith('-----'):
                continue
            if 'Lotto:' in l_strip:
                continue
            if l_strip == 'Imballaggi':
                continue

            m_pz = re.search(r'Pz:\s*(\d+)\s+Prezzo/pz:\s*([\d\,]+)', l_strip)
            if m_pz:
                pz_info = {'pz': int(m_pz.group(1)), 'prezzo_pz': to_float(m_pz.group(2))}

            m_row = re.search(r'([a-zA-Z\.]+)\s+([\d\,]+)\s+([\d\,]+)\s+([0-9A-Za-z]+)$', l_strip)
            if m_row:
                um, pr_str, imp_str, iva = m_row.groups()
                prezzo = to_float(pr_str)
                importo = to_float(imp_str)
                
                left_part = l_strip[:m_row.start()].rstrip()
                tokens = left_part.split()
                
                colli = None
                lordo = None
                tara = None
                netto = None
                
                num_tokens = []
                while tokens and re.match(r'^[\d\,]+$', tokens[-1]):
                    num_tokens.insert(0, tokens.pop())
                
                if len(num_tokens) == 1:
                    colli = to_float(num_tokens[0])
                elif len(num_tokens) == 2:
                    colli = to_float(num_tokens[0])
                    netto = to_float(num_tokens[1])
                elif len(num_tokens) == 3:
                    lordo = to_float(num_tokens[0])
                    tara = to_float(num_tokens[1])
                    netto = to_float(num_tokens[2])
                elif len(num_tokens) >= 4:
                    colli = to_float(num_tokens[-4])
                    lordo = to_float(num_tokens[-3])
                    tara = to_float(num_tokens[-2])
                    netto = to_float(num_tokens[-1])

                desc = " ".join(tokens)
                if current_desc_lines:
                    desc = " ".join(current_desc_lines) + " " + desc
                    current_desc_lines = []

                desc = re.sub(r'^(?:\d+\s+)+', '', desc).strip()
                desc = re.sub(r'\s+(?:IT|ES|FR|EC|IL|XX|NL|GR)$', '', desc).strip()

                riga = {
                    'file': os.path.basename(file_pdf),
                    'pagina': page_idx + 1,
                    'fattura': current_fat_num,
                    'data': current_fat_data,
                    'data_dt': parse_date(current_fat_data),
                    'cliente': current_cliente,
                    'descrizione': desc,
                    'colli': colli,
                    'lordo': lordo,
                    'tara': tara,
                    'netto': netto,
                    'um': um.upper(),
                    'prezzo': prezzo,
                    'importo': importo,
                    'iva': iva,
                    'pz_info': pz_info,
                    'raw': l_strip
                }
                righe.append(riga)
                pz_info = None
            else:
                if not any(k in l_strip for k in ['Pz:', 'Lotto:', 'Imballaggi']):
                    current_desc_lines.append(l_strip)

    if mostra_progresso:
        sys.stdout.write("\n")
        sys.stdout.flush()

    return righe

class IndicePrezziTemporale:
    """
    Gestisce i prezzi storici con date per abilitare la Finestra Temporale Mobile Continua.
    Fornisce la mediana e il range di prezzo dinamico per una finestra di 30 giorni.
    Include cache ad altissime prestazioni per evitare ricalcoli identici.
    """
    def __init__(self, righe_storico: List[Dict[str, Any]], range_db_statico: Dict[str, Any] = None):
        self.range_db_statico = range_db_statico or {}
        self.osservazioni: Dict[Tuple[str, str], List[Tuple[date, float]]] = {}
        self._cache: Dict[Tuple[str, str, Optional[date]], Optional[Dict[str, Any]]] = {}
        
        for r in righe_storico:
            p_key = normalizza_nome(r.get('descrizione', ''))
            um = r.get('um', '').upper()
            prezzo = r.get('prezzo', 0.0)
            dt = r.get('data_dt')
            if not p_key or not dt or prezzo <= 0:
                continue
            pair = (p_key, um)
            if pair not in self.osservazioni:
                self.osservazioni[pair] = []
            self.osservazioni[pair].append((dt, prezzo))

        # Ordina ogni serie per data
        for k in self.osservazioni:
            self.osservazioni[k].sort(key=lambda x: x[0])

    def get_range(self, prodotto_key: str, um: str, data_fattura: Optional[date], finestra_giorni: int = FINESTRA_GIORNI_DEFAULT) -> Optional[Dict[str, Any]]:
        cache_key = (prodotto_key, um, data_fattura)
        if cache_key in self._cache:
            return self._cache[cache_key]

        res = self._calcola_range(prodotto_key, um, data_fattura, finestra_giorni)
        self._cache[cache_key] = res
        return res

    def _calcola_range(self, prodotto_key: str, um: str, data_fattura: Optional[date], finestra_giorni: int) -> Optional[Dict[str, Any]]:
        pair = (prodotto_key, um)
        
        # 1. Ricerca nella finestra temporale [data_fattura - finestra_giorni, data_fattura + 7]
        if pair in self.osservazioni and data_fattura:
            t_min = data_fattura - timedelta(days=finestra_giorni)
            t_max = data_fattura + timedelta(days=7)

            prezzi_finestra = [p for dt, p in self.osservazioni[pair] if t_min <= dt <= t_max]
            
            # Se ci sono almeno 3 osservazioni nel periodo, usiamo la finestra locale
            if len(prezzi_finestra) >= 3:
                med_raw = statistics.median(prezzi_finestra)
                prezzi_filtrati = [p for p in prezzi_finestra if 0.25 * med_raw <= p <= 4.0 * med_raw]
                if not prezzi_filtrati:
                    prezzi_filtrati = prezzi_finestra

                mediana = round(statistics.median(prezzi_filtrati), 2)
                p_min = round(mediana * 0.65, 2)
                p_max = round(mediana * 1.45, 2)

                return {
                    'tipo': 'finestra_mobile_30gg',
                    'mediana': mediana,
                    'prezzo_min': p_min,
                    'prezzo_max': p_max,
                    'osservazioni': len(prezzi_filtrati),
                    'finestra_info': f"±{finestra_giorni}gg attorno a {data_fattura.strftime('%d/%m/%Y')}"
                }
            
            # Se ci sono meno di 3 osservazioni nella finestra, usiamo tutte le osservazioni disponibili per questo prodotto
            elif len(self.osservazioni[pair]) >= 2:
                tutti_prezzi = [p for _, p in self.osservazioni[pair]]
                med_raw = statistics.median(tutti_prezzi)
                prezzi_filtrati = [p for p in tutti_prezzi if 0.25 * med_raw <= p <= 4.0 * med_raw]
                if not prezzi_filtrati:
                    prezzi_filtrati = tutti_prezzi
                mediana = round(statistics.median(prezzi_filtrati), 2)
                return {
                    'tipo': 'storico_globale',
                    'mediana': mediana,
                    'prezzo_min': round(mediana * 0.65, 2),
                    'prezzo_max': round(mediana * 1.45, 2),
                    'osservazioni': len(prezzi_filtrati),
                    'finestra_info': 'intero storico disponibile'
                }

        # 2. Fallback a database statico (se presente)
        prod_data = self.range_db_statico.get(prodotto_key, {})
        um_stats = prod_data.get('per_um', {}).get(um)
        if um_stats:
            return {
                'tipo': 'database_statico',
                'mediana': um_stats.get('prezzo_medio_rilevato', 0.0),
                'prezzo_min': um_stats.get('prezzo_min', 0.0),
                'prezzo_max': um_stats.get('prezzo_max', 0.0),
                'osservazioni': um_stats.get('osservazioni', 1),
                'finestra_info': 'listino base'
            }

        return None

def analizza_riga(riga: Dict[str, Any], indice_temporale: IndicePrezziTemporale) -> Dict[str, Any]:

    """
    Esegue tutti i controlli:
    - Attendibilità 100%: errore matematico certo o colli*pezzi moltiplicati male
    - Attendibilità 90%-95%: virgola saltata (fattore 10x/100x o pezzo a cassa)
    - Attendibilità 70-75%: prezzo fuori dal range stagionale mobile (finestra 30 giorni)
    - Attendibilità 40-50%: scostamento moderato o unità di misura inconsueta
    """
    um = riga['um']
    prezzo = riga['prezzo']
    importo = riga['importo']
    colli = riga['colli']
    netto = riga['netto']
    pz_info = riga['pz_info']
    data_dt = riga.get('data_dt')
    p_key = normalizza_nome(riga['descrizione'])

    anomalie = []
    attendibilita = 0

    # Moltiplicatore atteso per unità di misura
    if um == 'KG':
        moltiplicatore = netto if netto is not None else (colli if colli is not None else 1.0)
    elif um in ['PZ', 'PZ.']:
        moltiplicatore = netto if netto is not None else (colli if colli is not None else 1.0)
    else:  # N., n., ecc. (vendita a collo/cassa)
        moltiplicatore = colli if colli is not None else 1.0

    importo_atteso = round(moltiplicatore * prezzo, 2)
    diff_matematica = round(abs(importo - importo_atteso), 2)

    # 1. CONTROLLO MATEMATICO ASSOLUTO
    if diff_matematica > 0.05:
        if pz_info and pz_info.get('pz'):
            pz = pz_info['pz']
            if colli and abs(importo - round(colli * pz * prezzo, 2)) <= 0.05:
                attendibilita = max(attendibilita, 100)
                anomalie.append(f"Errore Colli*Pezzi: calcolato Colli({colli}) * Pz({pz}) * Prezzo({prezzo:.2f}) = {importo:.2f} € invece di {importo_atteso:.2f} €")
            elif abs(importo - round(pz * prezzo, 2)) <= 0.05:
                attendibilita = max(attendibilita, 85)
                anomalie.append(f"Moltiplicato per Pezzi({pz}) anziché per Colli({colli})")
        
        if not anomalie:
            attendibilita = max(attendibilita, 100)
            anomalie.append(f"Errore Calcolo Matematico: Atteso {importo_atteso:.2f} € ({moltiplicatore} {um} * {prezzo:.2f} €), ma indicato {importo:.2f} € (Differenza: {diff_matematica:.2f} €)")

    # 2. CONTROLLO VIRGOLA SPOSTATA (10x / 100x sull'importo riga)
    if importo > 0 and importo_atteso > 0:
        ratio = importo / importo_atteso
        if 9.5 <= ratio <= 10.5 or 95.0 <= ratio <= 105.0:
            attendibilita = max(attendibilita, 95)
            anomalie.append(f"Virgola Decimale Saltata: Importo fatturato {importo:.2f} € è 10x/100x rispetto al valore corretto ({importo_atteso:.2f} €)")
        elif 0.095 <= ratio <= 0.105:
            attendibilita = max(attendibilita, 95)
            anomalie.append(f"Virgola Decimale: Importo fatturato {importo:.2f} € è 1/10 rispetto al valore corretto ({importo_atteso:.2f} €)")

    # 3. CONTROLLO PREZZO CON FINESTRA TEMPORALE MOBILE (30 GIORNI)
    range_info = indice_temporale.get_range(p_key, um, data_dt, finestra_giorni=FINESTRA_GIORNI_DEFAULT)
    
    if range_info:
        p_min = range_info['prezzo_min']
        p_max = range_info['prezzo_max']
        mediana = range_info['mediana']
        tipo_range = range_info['tipo']

        # Prezzo eccessivo rispetto al periodo
        if p_max > 0 and prezzo > p_max:
            # Controllo ordine di grandezza 10x o 4x rispetto al periodo stagionale
            if mediana > 0 and prezzo >= mediana * 4.0:
                attendibilita = max(attendibilita, 90)
                anomalie.append(f"Prezzo Fuori Scala 10x/5x nel periodo: {prezzo:.2f} €/{um} (Mediana periodo: {mediana:.2f} € | Max: {p_max:.2f} €)")
            else:
                scostamento = (prezzo - p_max) / p_max
                if scostamento > 0.50:  # Oltre +50% dal massimo del periodo
                    attendibilita = max(attendibilita, 75)
                    anomalie.append(f"Prezzo Fuori Range (+{scostamento*100:.0f}%): {prezzo:.2f} €/{um} (Max periodo 30gg: {p_max:.2f} €)")
                elif scostamento > 0.25:
                    attendibilita = max(attendibilita, 40)
                    anomalie.append(f"Prezzo Leggermente Alto: {prezzo:.2f} €/{um} (Max periodo: {p_max:.2f} €)")

        # Prezzo anormalmente basso rispetto al periodo
        elif p_min > 0 and prezzo < p_min:
            if mediana > 0 and prezzo <= mediana / 4.0:
                attendibilita = max(attendibilita, 90)
                anomalie.append(f"Prezzo Anormalmente Basso nel periodo: {prezzo:.2f} €/{um} (Mediana periodo: {mediana:.2f} € | Min: {p_min:.2f} €)")
            else:
                scostamento = (p_min - prezzo) / p_min
                if scostamento > 0.50:
                    attendibilita = max(attendibilita, 70)
                    anomalie.append(f"Prezzo Sotto Soglia (-{scostamento*100:.0f}%): {prezzo:.2f} €/{um} (Min periodo 30gg: {p_min:.2f} €)")

    # Assegnazione Livello
    if attendibilita >= 90:
        livello = "CRITICO (90-100%)"
    elif attendibilita >= 70:
        livello = "SOSPETTO (70-80%)"
    elif attendibilita >= 40:
        livello = "ATTENZIONE (40-50%)"
    else:
        livello = "OK"

    return {
        'attendibilita_errore': f"{attendibilita}%",
        'attendibilita_val': attendibilita,
        'livello': livello,
        'importo_atteso': importo_atteso,
        'anomalie': " | ".join(anomalie) if anomalie else "Nessuna",
        'ha_anomalia': attendibilita > 0
    }

def genera_report_html(righe_analizzate: List[Dict[str, Any]], percorso_html: str, nome_target: str):
    os.makedirs(os.path.dirname(percorso_html), exist_ok=True)
    tot_righe = len(righe_analizzate)
    critici = sum(1 for item in righe_analizzate if item['risultato']['attendibilita_val'] >= 90)
    sospetti = sum(1 for item in righe_analizzate if 70 <= item['risultato']['attendibilita_val'] < 90)
    avvisi = sum(1 for item in righe_analizzate if 40 <= item['risultato']['attendibilita_val'] < 70)
    regolari = tot_righe - critici - sospetti - avvisi

    html_content = f"""<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <title>Report Controllo Fatture - {nome_target}</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; }}
        body {{ background-color: #f4f6f9; color: #2c3e50; padding: 24px; }}
        .header {{ margin-bottom: 24px; }}
        h1 {{ font-size: 26px; color: #1a252f; margin-bottom: 6px; }}
        .sub {{ color: #7f8c8d; font-size: 14px; }}
        .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }}
        .card {{ background: #fff; padding: 18px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.06); border-left: 5px solid #ccc; }}
        .card.critico {{ border-left-color: #e74c3c; }}
        .card.sospetto {{ border-left-color: #e67e22; }}
        .card.avviso {{ border-left-color: #f1c40f; }}
        .card.regolare {{ border-left-color: #2ecc71; }}
        .card-num {{ font-size: 28px; font-weight: bold; margin-top: 4px; }}
        .card-label {{ font-size: 13px; text-transform: uppercase; color: #7f8c8d; font-weight: 600; }}
        .toolbar {{ background: #fff; padding: 14px 18px; border-radius: 8px; margin-bottom: 16px; display: flex; flex-wrap: wrap; gap: 12px; align-items: center; justify-content: space-between; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }}
        .btn-group button {{ background: #ecf0f1; border: none; padding: 8px 14px; border-radius: 6px; font-size: 13px; font-weight: 600; cursor: pointer; transition: all 0.2s; margin-right: 6px; }}
        .btn-group button.active, .btn-group button:hover {{ background: #2c3e50; color: #fff; }}
        .search-box input {{ padding: 8px 14px; border: 1px solid #dcdde1; border-radius: 6px; font-size: 14px; width: 280px; outline: none; }}
        .table-container {{ background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 6px rgba(0,0,0,0.06); }}
        table {{ width: 100%; border-collapse: collapse; text-align: left; font-size: 13px; }}
        th {{ background: #2c3e50; color: #fff; padding: 12px 14px; font-weight: 600; text-transform: uppercase; font-size: 11px; letter-spacing: 0.5px; position: sticky; top: 0; }}
        td {{ padding: 10px 14px; border-bottom: 1px solid #ecf0f1; }}
        tr.row-critico {{ background: #fdf2f2; font-weight: 500; }}
        tr.row-sospetto {{ background: #fef8f3; }}
        tr.row-avviso {{ background: #fffdf5; }}
        tr:hover {{ filter: brightness(0.97); }}
        .badge {{ display: inline-block; padding: 4px 8px; border-radius: 4px; font-size: 11px; font-weight: bold; color: #fff; text-align: center; }}
        .badge-100, .badge-90 {{ background: #e74c3c; }}
        .badge-80, .badge-75, .badge-70 {{ background: #e67e22; }}
        .badge-50, .badge-40 {{ background: #f39c12; }}
        .badge-0 {{ background: #2ecc71; }}
        .anomalia-text {{ color: #c0392b; font-weight: 600; }}
        .prezzo {{ font-family: monospace; font-size: 13px; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>Controllo Fatture Ortofrutta - {nome_target}</h1>
        <div class="sub">Righe analizzate: {tot_righe} | Finestra temporale mobile: ±30 giorni per stagionalità</div>
    </div>

    <div class="cards">
        <div class="card critico">
            <div class="card-label">Errori Gravi (90% - 100%)</div>
            <div class="card-num" style="color: #e74c3c;">{critici}</div>
        </div>
        <div class="card sospetto">
            <div class="card-label">Anomalie Sospette (70% - 80%)</div>
            <div class="card-num" style="color: #e67e22;">{sospetti}</div>
        </div>
        <div class="card avviso">
            <div class="card-label">Avvisi da Verificare (40% - 50%)</div>
            <div class="card-num" style="color: #f39c12;">{avvisi}</div>
        </div>
        <div class="card regolare">
            <div class="card-label">Righe Regolari (0%)</div>
            <div class="card-num" style="color: #2ecc71;">{regolari}</div>
        </div>
    </div>

    <div class="toolbar">
        <div class="btn-group">
            <button class="active" onclick="filterTable('anomalie')">🚨 Solo Anomalie ({critici + sospetti + avvisi})</button>
            <button onclick="filterTable('critici')">🔴 Solo Gravi ({critici})</button>
            <button onclick="filterTable('tutti')">📋 Tutte le Righe ({tot_righe})</button>
        </div>
        <div class="search-box">
            <input type="text" id="searchInput" onkeyup="searchTable()" placeholder="Cerca cliente, articolo o fattura...">
        </div>
    </div>

    <div class="table-container">
        <table id="dataTable">
            <thead>
                <tr>
                    <th>Attendibilità</th>
                    <th>Fattura</th>
                    <th>Data</th>
                    <th>Cliente</th>
                    <th>Articolo</th>
                    <th>Colli</th>
                    <th>Netto</th>
                    <th>UM</th>
                    <th>Prezzo Unit.</th>
                    <th>Fatturato</th>
                    <th>Atteso</th>
                    <th>Diagnosi / Anomalia</th>
                </tr>
            </thead>
            <tbody>
    """

    for item in righe_analizzate:
        r = item['riga']
        res = item['risultato']
        att_val = res['attendibilita_val']
        
        if att_val >= 90:
            row_class = "row-critico"
            badge_class = "badge-90"
            filter_cat = "critico anomalia"
        elif att_val >= 70:
            row_class = "row-sospetto"
            badge_class = "badge-75"
            filter_cat = "sospetto anomalia"
        elif att_val >= 40:
            row_class = "row-avviso"
            badge_class = "badge-50"
            filter_cat = "avviso anomalia"
        else:
            row_class = ""
            badge_class = "badge-0"
            filter_cat = "regolare"

        colli_txt = f"{r['colli']:.0f}" if (r['colli'] is not None and r['colli'].is_integer()) else (f"{r['colli']}" if r['colli'] is not None else "-")
        netto_txt = f"{r['netto']:.1f}" if (r['netto'] is not None) else "-"
        
        anomalia_td = f"<span class='anomalia-text'>{res['anomalie']}</span>" if res['ha_anomalia'] else "<span style='color: #95a5a6;'>Nessuna</span>"

        html_content += f"""
                <tr class="{row_class}" data-cat="{filter_cat}">
                    <td><span class="badge {badge_class}">{res['attendibilita_errore']}</span></td>
                    <td><b>{r['fattura']}</b></td>
                    <td>{r['data']}</td>
                    <td>{r['cliente']}</td>
                    <td>{r['descrizione']}</td>
                    <td>{colli_txt}</td>
                    <td>{netto_txt}</td>
                    <td><b>{r['um']}</b></td>
                    <td class="prezzo">{r['prezzo']:.2f} €</td>
                    <td class="prezzo"><b>{r['importo']:.2f} €</b></td>
                    <td class="prezzo">{res['importo_atteso']:.2f} €</td>
                    <td>{anomalia_td}</td>
                </tr>"""

    html_content += """
            </tbody>
        </table>
    </div>

    <script>
        let currentFilter = 'anomalie';
        filterTable('anomalie');

        function filterTable(cat) {
            currentFilter = cat;
            const buttons = document.querySelectorAll('.btn-group button');
            buttons.forEach(b => b.classList.remove('active'));
            event ? event.target.classList.add('active') : buttons[0].classList.add('active');

            const rows = document.querySelectorAll('#dataTable tbody tr');
            rows.forEach(r => {
                const rowCat = r.getAttribute('data-cat') || '';
                if (cat === 'tutti') {
                    r.style.display = '';
                } else if (cat === 'anomalie') {
                    r.style.display = rowCat.includes('anomalia') ? '' : 'none';
                } else if (cat === 'critici') {
                    r.style.display = rowCat.includes('critico') ? '' : 'none';
                }
            });
            searchTable();
        }

        function searchTable() {
            const input = document.getElementById('searchInput').value.toUpperCase();
            const rows = document.querySelectorAll('#dataTable tbody tr');
            rows.forEach(r => {
                const rowCat = r.getAttribute('data-cat') || '';
                let visibleByCat = true;
                if (currentFilter === 'anomalie') visibleByCat = rowCat.includes('anomalia');
                else if (currentFilter === 'critici') visibleByCat = rowCat.includes('critico');

                if (!visibleByCat) {
                    r.style.display = 'none';
                    return;
                }
                const text = r.innerText.toUpperCase();
                r.style.display = text.indexOf(input) > -1 ? '' : 'none';
            });
        }
    </script>
</body>
</html>
    """
    with open(percorso_html, 'w', encoding='utf-8') as f:
        f.write(html_content)

def genera_report_csv(righe_analizzate: List[Dict[str, Any]], percorso_report: str):
    os.makedirs(os.path.dirname(percorso_report), exist_ok=True)
    headers = [
        'Attendibilita_Errore', 'Livello', 'Fattura', 'Data', 'Cliente',
        'Articolo', 'Colli', 'Netto', 'UM', 'Prezzo', 'Importo_Fatturato',
        'Importo_Atteso', 'Anomalie'
    ]
    with open(percorso_report, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for item in righe_analizzate:
            r = item['riga']
            res = item['risultato']
            writer.writerow({
                'Attendibilita_Errore': res['attendibilita_errore'],
                'Livello': res['livello'],
                'Fattura': r['fattura'],
                'Data': r['data'],
                'Cliente': r['cliente'],
                'Articolo': r['descrizione'],
                'Colli': r['colli'] if r['colli'] is not None else '',
                'Netto': r['netto'] if r['netto'] is not None else '',
                'UM': r['um'],
                'Prezzo': f"{r['prezzo']:.2f}".replace('.', ','),
                'Importo_Fatturato': f"{r['importo']:.2f}".replace('.', ','),
                'Importo_Atteso': f"{res['importo_atteso']:.2f}".replace('.', ','),
                'Anomalie': res['anomalie']
            })

def carica_storico_prezzi() -> List[Dict[str, Any]]:
    """Carica le transazioni dal file storico CSV persistente."""
    if not os.path.exists(FILE_STORICO):
        return []
    righe = []
    try:
        with open(FILE_STORICO, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for r in reader:
                dt = parse_date(r.get('data_fattura', ''))
                pr = to_float(r.get('prezzo_unitario', 0.0))
                if dt and pr > 0:
                    righe.append({
                        'data': r.get('data_fattura', ''),
                        'data_dt': dt,
                        'cliente': r.get('fornitore', ''),
                        'descrizione': r.get('prodotto', ''),
                        'colli': to_float(r.get('colli', 0)),
                        'netto': to_float(r.get('quantita', 0)),
                        'um': r.get('um', '').upper(),
                        'prezzo': pr,
                        'importo': to_float(r.get('totale_riga', 0)),
                        'stato_validazione': r.get('stato_validazione', 'REGOLARE'),
                        'note': r.get('note', '')
                    })
    except Exception as e:
        print(f"[AVVISO] Errore lettura storico_prezzi.csv: {e}")
    return righe

def aggiorna_storico_prezzi(risultati_analizzati: List[Dict[str, Any]], giorni_validita: int = FINESTRA_GIORNI_DEFAULT) -> int:
    """
    Registra nello storico CSV solo le righe certificate prive di errori (attendibilita_val == 0).
    Regola snella a massima efficienza:
    - Massimo 1 rilevazione al giorno per (prodotto, UM): se nello stesso giorno ci sono più fatture,
      calcola la mediana dei prezzi del giorno.
    - Decadimento automatico: mantiene solo le date entro `giorni_validita` (30gg) dalla data più recente.
    Ritorna il numero totale di rilevazioni attive mantenute nello storico.
    """
    storico_esistente = carica_storico_prezzi()

    # Raggruppamento per (prodotto_normalizzato, um, data_dt)
    gruppi: Dict[Tuple[str, str, date], Dict[str, Any]] = {}

    for r in storico_esistente:
        dt = r.get('data_dt')
        p_key = normalizza_nome(r.get('descrizione', ''))
        um = r.get('um', '').upper()
        if not dt or not p_key:
            continue
        key = (p_key, um, dt)
        if key not in gruppi:
            gruppi[key] = {
                'data_str': r.get('data'),
                'fornitori': {r.get('cliente', '')} if r.get('cliente') else set(),
                'prodotto_nome': r.get('descrizione', ''),
                'prezzi': [],
                'colli': 0.0,
                'quantita': 0.0,
                'totale': 0.0
            }
        gruppi[key]['prezzi'].append(r['prezzo'])
        gruppi[key]['colli'] += r.get('colli', 0.0)
        gruppi[key]['quantita'] += r.get('netto', 0.0)
        gruppi[key]['totale'] += r.get('importo', 0.0)

    # Aggiunta delle sole righe regolari (0% errori)
    for item in risultati_analizzati:
        if item['risultato']['attendibilita_val'] != 0:
            continue
        r = item['riga']
        dt = r.get('data_dt')
        p_key = normalizza_nome(r.get('descrizione', ''))
        um = r.get('um', '').upper()
        prezzo = r.get('prezzo', 0.0)
        if not dt or not p_key or prezzo <= 0:
            continue
        
        key = (p_key, um, dt)
        if key not in gruppi:
            gruppi[key] = {
                'data_str': r.get('data'),
                'fornitori': set(),
                'prodotto_nome': p_key,
                'prezzi': [],
                'colli': 0.0,
                'quantita': 0.0,
                'totale': 0.0
            }
        if r.get('cliente'):
            gruppi[key]['fornitori'].add(r['cliente'])
        gruppi[key]['prezzi'].append(prezzo)
        gruppi[key]['colli'] += (r.get('colli') or 0.0)
        gruppi[key]['quantita'] += (r.get('netto') or 0.0)
        gruppi[key]['totale'] += (r.get('importo') or 0.0)

    if not gruppi:
        return 0

    # Finestra mobile temporale di 30 giorni
    tutte_le_date = [k[2] for k in gruppi.keys()]
    data_max = max(tutte_le_date)
    data_limite = data_max - timedelta(days=giorni_validita)

    righe_finali = []
    for (p_key, um, dt), dati in gruppi.items():
        if dt < data_limite:
            continue  # Più vecchio di 30 giorni: decaduto
        
        prezzo_giorno = round(statistics.median(dati['prezzi']), 2)
        fornitori_str = ", ".join(sorted(dati['fornitori'])) if dati['fornitori'] else "N/D"
        
        righe_finali.append({
            'data_dt': dt,
            'data_fattura': dati['data_str'] or dt.strftime('%d/%m/%y'),
            'fornitore': fornitori_str,
            'prodotto': dati['prodotto_nome'],
            'colli': round(dati['colli'], 1) if dati['colli'] > 0 else '',
            'quantita': round(dati['quantita'], 2) if dati['quantita'] > 0 else '',
            'um': um,
            'prezzo_unitario': f"{prezzo_giorno:.2f}",
            'totale_riga': f"{dati['totale']:.2f}" if dati['totale'] > 0 else '',
            'stato_validazione': 'REGOLARE',
            'note': f"Consolidato da {len(dati['prezzi'])} transazioni del giorno" if len(dati['prezzi']) > 1 else 'OK'
        })

    # Ordina per data decrescente e per nome prodotto
    righe_finali.sort(key=lambda x: (x['data_dt'], x['prodotto']), reverse=True)

    # Scrittura CSV
    os.makedirs(DIR_DATA, exist_ok=True)
    fieldnames = [
        'data_fattura', 'fornitore', 'prodotto', 'colli', 'quantita',
        'um', 'prezzo_unitario', 'totale_riga', 'stato_validazione', 'note'
    ]
    with open(FILE_STORICO, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in righe_finali:
            row_dict = {k: r[k] for k in fieldnames}
            writer.writerow(row_dict)

    return len(righe_finali)

def esegui_controllo_cartella(cartella: str = None, files_pdf: Optional[List[str]] = None, nome_target: str = "tutte", mostra_log: bool = True) -> Dict[str, Any]:
    """Analizza i PDF, aggiorna lo storico prezzi e restituisce risultati e riepilogo."""
    if cartella is None:
        cartella = DIR_FATTURE
    cartella = os.path.abspath(cartella)

    if files_pdf is None:
        files_pdf = sorted(glob.glob(os.path.join(cartella, "*.pdf")))
    else:
        files_pdf = [os.path.abspath(p) for p in files_pdf if os.path.exists(p)]

    if not files_pdf:
        raise FileNotFoundError(f"Nessun file PDF trovato nella cartella: {cartella}")

    if nome_target == "tutte" or not nome_target:
        nome_target = os.path.basename(cartella) if os.path.basename(cartella) else "tutte"

    if mostra_log:
        print("=" * 75)
        print("  CONTROLLO FATTURE ORTOFRUTTA - FINESTRA TEMPORALE MOBILE 30gg")
        print("=" * 75)

    print("\n[PASSO 1/3] LETTURA ED ESTRAZIONE FATTURE PDF")
    cache_righe = {}
    for f in files_pdf:
        righe_f = estrai_righe_da_pdf(f, mostra_progresso=True)
        cache_righe[os.path.abspath(f)] = righe_f

    tutte_le_righe_storico = []
    for righe_f in cache_righe.values():
        tutte_le_righe_storico.extend(righe_f)

    righe_csv_storico = carica_storico_prezzi()
    if righe_csv_storico:
        tutte_le_righe_storico.extend(righe_csv_storico)
        if mostra_log:
            print(f" -> Caricate {len(righe_csv_storico):,} rilevazioni consolidate da storico_prezzi.csv.")

    range_db_statico = carica_range_prezzi()
    indice_temporale = IndicePrezziTemporale(tutte_le_righe_storico, range_db_statico)
    if mostra_log:
        print(f" -> Archivio temporale pronto: {len(tutte_le_righe_storico):,} transazioni storiche indicizzate.")

    righe_da_analizzare = []
    for f in files_pdf:
        righe = cache_righe.get(os.path.abspath(f), [])
        if mostra_log:
            print(f" -> File in analisi: {os.path.basename(f)} ({len(righe):,} righe articolo).")
        righe_da_analizzare.extend(righe)

    if mostra_log:
        print("\n[PASSO 2/3] VERIFICA MATEMATICA E RILEVAMENTO ANOMALIE")
    tot_righe = len(righe_da_analizzare)
    risultati = []
    for idx, r in enumerate(righe_da_analizzare):
        if mostra_log and (idx % 100 == 0 or idx == tot_righe - 1):
            stampa_barra(idx + 1, tot_righe, prefisso="Controllo righe:")
        res = analizza_riga(r, indice_temporale)
        risultati.append({'riga': r, 'risultato': res})

    if mostra_log:
        sys.stdout.write("\n")
        sys.stdout.flush()
        print(" -> Verifica completata con successo!")

    risultati.sort(key=lambda item: item['risultato']['attendibilita_val'], reverse=True)
    critici_100 = [x for x in risultati if x['risultato']['attendibilita_val'] == 100]
    molto_probabili_80_90 = [x for x in risultati if 80 <= x['risultato']['attendibilita_val'] < 100]
    sospetti_70_75 = [x for x in risultati if 70 <= x['risultato']['attendibilita_val'] < 80]
    avvisi_40_50 = [x for x in risultati if 40 <= x['risultato']['attendibilita_val'] < 70]
    solo_anomalie = [x for x in risultati if x['risultato']['ha_anomalia']]

    file_report_html = os.path.join(DIR_REPORT, "report_ultimo.html")
    genera_report_html(risultati, file_report_html, nome_target)

    num_salvati = aggiorna_storico_prezzi(risultati, giorni_validita=FINESTRA_GIORNI_DEFAULT)

    if mostra_log:
        print("\n[PASSO 3/3] AGGIORNAMENTO STORICO E RIEPILOGO")
        print("\n" + "=" * 75)
        print(f"ESITO CONTROLLO SU {len(righe_da_analizzare)} RIGHE TOTALI:")
        print(f" - [100%] Errori Matematici Certi: {len(critici_100)}")
        print(f" - [80%-90%] Errori Molto Probabili (10x / Colli*Pz): {len(molto_probabili_80_90)}")
        print(f" - [70%-75%] Anomalie Sospette (Prezzo Fuori Finestra 30gg): {len(sospetti_70_75)}")
        print(f" - [40%-50%] Avvisi da Verificare: {len(avvisi_40_50)}")
        print("=" * 75)

    summary = {
        'cartella': cartella,
        'files_pdf': files_pdf,
        'nome_target': nome_target,
        'risultati': risultati,
        'solo_anomalie': solo_anomalie,
        'totale_righe': len(righe_da_analizzare),
        'totale_anomalie': len(solo_anomalie),
        'totale_errori_critici': len(critici_100),
        'totale_molto_probabili': len(molto_probabili_80_90),
        'totale_sospetti': len(sospetti_70_75),
        'totale_avvisi': len(avvisi_40_50),
        'num_salvati_storico': num_salvati,
        'report_html': file_report_html,
    }

    if mostra_log:
        print("CONTROLLO COMPLETATO (Finestra Mobile 30gg attiva):")
        print(f" Report HTML aggiornato: {file_report_html}")
        print(f" Storico prezzi aggiornato: {FILE_STORICO} ({num_salvati} rilevazioni attive negli ultimi {FINESTRA_GIORNI_DEFAULT} giorni)")
        print("=" * 75)

    return summary


def main():
    import sys
    nome_target = "tutte"
    files_pdf = None
    if len(sys.argv) > 1:
        target = sys.argv[1]
        if not os.path.isabs(target):
            candidate = os.path.join(DIR_FATTURE, target)
            if os.path.exists(candidate):
                files_pdf = [candidate]
            elif os.path.exists(target):
                files_pdf = [os.path.abspath(target)]
            else:
                files_pdf = glob.glob(os.path.join(DIR_FATTURE, f"*{target}*"))
        else:
            files_pdf = [target]
        if files_pdf:
            nome_target = os.path.splitext(os.path.basename(files_pdf[0]))[0]
    else:
        files_pdf = glob.glob(os.path.join(DIR_FATTURE, "*.pdf"))

    if not files_pdf:
        print(f"[AVVISO] Nessun file PDF trovato.")
        return

    try:
        esegui_controllo_cartella(DIR_FATTURE, files_pdf=files_pdf, nome_target=nome_target, mostra_log=True)
    except FileNotFoundError as e:
        print(f"[AVVISO] {e}")

if __name__ == "__main__":
    main()
