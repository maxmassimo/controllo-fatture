import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal
from textual.widgets import Button, ContentSwitcher, DataTable, Input, RichLog, Static

from controlla_fatture import esegui_controllo_cartella

DEFAULT_FOLDER = os.path.join(ROOT_DIR, "Fatture-da-controllare")
LAST_ANALYSIS_FILE = ROOT_DIR / "data" / "ultima_analisi.json"


class FattureTextualApp(App):
    CSS = """
    Screen {
        background: #0f172a;
        color: #e2e8f0;
    }

    #main {
        layout: vertical;
        padding: 1 2;
        width: 100%;
        height: 100%;
    }

    #toolbar, #report_toolbar {
        height: auto;
        margin-bottom: 1;
    }

    Input {
        width: 1fr;
        margin-right: 1;
    }

    Button {
        min-width: 12;
    }

    #pages, .page {
        height: 1fr;
    }

    .page {
        layout: vertical;
    }

    #status {
        background: #1e293b;
        color: #f8fafc;
        padding: 1;
        margin-bottom: 1;
    }

    #summary {
        background: #111827;
        color: #dbeafe;
        padding: 1;
        height: 8;
        margin-bottom: 1;
    }

    #results_table {
        height: 1fr;
    }

    RichLog {
        height: 1fr;
        background: #020817;
        border: solid #334155;
    }
    """

    BINDINGS = [
        ("ctrl+r", "run_analysis", "Analizza"),
        ("q", "quit", "Esci"),
    ]

    def __init__(self):
        super().__init__()
        self.current_results = []
        self.show_anomalies_only = True
        self.last_report_html = str(ROOT_DIR / "report" / "report_ultimo.html")

    def compose(self) -> ComposeResult:
        with Container(id="main"):
            with ContentSwitcher(initial="home_page", id="pages"):
                with Container(id="home_page", classes="page"):
                    with Horizontal(id="toolbar"):
                        yield Input(
                            placeholder="Cartella PDF da controllare",
                            value=DEFAULT_FOLDER,
                            id="folder_input",
                        )
                        yield Button("Analizza", id="run_analysis", variant="success")
                        yield Button("Visualizza report", id="show_report", variant="primary")
                        yield Button("Apri report HTML", id="open_report_html")
                    yield Static("Cartella di lavoro: " + DEFAULT_FOLDER, id="status")
                    yield Static("Nessuna analisi eseguita. Premi Analizza per iniziare.", id="summary")
                    yield RichLog(id="log", highlight=True, markup=True)
                with Container(id="report_page", classes="page"):
                    with Horizontal(id="report_toolbar"):
                        yield Button("← Menu principale", id="show_home", variant="primary")
                        yield Button("Solo anomalie", id="show_anomalies")
                        yield Button("Tutte le righe", id="show_all_rows")
                        yield Static("Esegui un controllo per visualizzare i risultati.", id="report_status")
                    yield DataTable(id="results_table")

    def on_mount(self) -> None:
        table = self.query_one("#results_table", DataTable)
        table.add_columns(
            "Attendibilità", "Livello", "Fattura", "Data", "Cliente",
            "Articolo", "Colli", "Netto", "UM", "Prezzo", "Fatturato",
            "Atteso", "Anomalie"
        )
        table.zebra_stripes = True
        self.query_one(RichLog).write("Sistema pronto. Premi Analizza per iniziare.")
        self._load_last_analysis()

    def run_analysis(self) -> None:
        folder = self.query_one("#folder_input", Input).value.strip() or DEFAULT_FOLDER
        if not os.path.isdir(folder):
            self.query_one("#status", Static).update(f"Cartella non trovata: {folder}")
            self.query_one(RichLog).write(f"[bold red]Cartella non trovata:[/] {folder}")
            return

        self.query_one("#status", Static).update(f"Analisi in corso: {folder}")
        self.query_one(RichLog).write(f"[cyan]Inizio analisi:[/] {folder}")
        self.run_worker(lambda: self._analysis_worker(folder), exclusive=True, thread=True)

    def _analysis_worker(self, folder: str) -> None:
        try:
            summary = esegui_controllo_cartella(folder, mostra_log=False)
            self.call_from_thread(self._show_results, summary)
        except Exception as exc:  # pragma: no cover - GUI error path
            self.call_from_thread(self._show_error, str(exc))

    def _show_results(self, summary) -> None:
        self.current_results = summary["risultati"]
        self.show_anomalies_only = True
        self.last_report_html = summary["report_html"]
        self._set_analysis_status(summary)
        self._set_analysis_summary(summary)
        self._populate_results_table()
        self.query_one(RichLog).write(
            f"[green]Controllo completato.[/] {len(summary['files_pdf'])} PDF, "
            f"{summary['totale_anomalie']} righe anomale."
        )
        try:
            self._save_last_analysis(summary)
        except (OSError, TypeError, ValueError) as exc:
            self.query_one(RichLog).write(f"[bold red]Impossibile salvare l'ultima analisi:[/] {exc}")

    def _set_analysis_status(self, summary) -> None:
        self.query_one("#status", Static).update(
            f"Cartella: {summary['cartella']} | PDF: {len(summary['files_pdf'])} | "
            f"Anomalie: {summary['totale_anomalie']}"
        )

    def _set_analysis_summary(self, summary) -> None:
        self.query_one("#summary", Static).update(
            f"Totale righe: {summary['totale_righe']}\n"
            f"Errori critici: {summary['totale_errori_critici']}\n"
            f"Molto probabili: {summary['totale_molto_probabili']}\n"
            f"Sospetti: {summary['totale_sospetti']}\n"
            f"Avvisi: {summary['totale_avvisi']}\n"
            f"Righe salvate nello storico: {summary['num_salvati_storico']}"
        )

    def _save_last_analysis(self, summary) -> None:
        rows = []
        for item in summary["risultati"]:
            row = item["riga"]
            result = item["risultato"]
            rows.append({
                "riga": {
                    key: row.get(key)
                    for key in ("fattura", "data", "cliente", "descrizione", "colli", "netto", "um", "prezzo", "importo")
                },
                "risultato": {
                    key: result.get(key)
                    for key in ("attendibilita_errore", "livello", "importo_atteso", "anomalie", "ha_anomalia")
                },
            })

        payload = {
            "version": 1,
            "analizzata_il": datetime.now().isoformat(timespec="seconds"),
            "cartella": summary["cartella"],
            "numero_pdf": len(summary["files_pdf"]),
            "totale_righe": summary["totale_righe"],
            "totale_anomalie": summary["totale_anomalie"],
            "totale_errori_critici": summary["totale_errori_critici"],
            "totale_molto_probabili": summary["totale_molto_probabili"],
            "totale_sospetti": summary["totale_sospetti"],
            "totale_avvisi": summary["totale_avvisi"],
            "num_salvati_storico": summary["num_salvati_storico"],
            "risultati": rows,
        }

        LAST_ANALYSIS_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary_file = LAST_ANALYSIS_FILE.with_suffix(".json.tmp")
        try:
            with temporary_file.open("w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False)
            os.replace(temporary_file, LAST_ANALYSIS_FILE)
        finally:
            temporary_file.unlink(missing_ok=True)

    def _load_last_analysis(self) -> None:
        try:
            with LAST_ANALYSIS_FILE.open("r", encoding="utf-8") as file:
                saved = json.load(file)
            if saved.get("version") != 1 or not isinstance(saved.get("risultati"), list):
                raise ValueError("formato del file di ultima analisi non riconosciuto")
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            self.query_one(RichLog).write(f"[bold red]Impossibile caricare l'ultima analisi:[/] {exc}")
            return

        self.current_results = saved["risultati"]
        self.show_anomalies_only = True
        self.query_one("#status", Static).update(
            f"Ultima analisi: {saved['analizzata_il']} | Cartella: {saved['cartella']} | "
            f"PDF: {saved['numero_pdf']} | Anomalie: {saved['totale_anomalie']}"
        )
        self._set_analysis_summary(saved)
        self._populate_results_table()
        self.query_one(RichLog).write(
            f"[green]Ultima analisi ripristinata.[/] {saved['totale_anomalie']} anomalie disponibili."
        )

    def _populate_results_table(self) -> None:
        table = self.query_one("#results_table", DataTable)
        table.clear()
        rows = [
            item for item in self.current_results
            if not self.show_anomalies_only or item["risultato"]["ha_anomalia"]
        ]
        for item in rows:
            row = item["riga"]
            result = item["risultato"]
            table.add_row(
                result["attendibilita_errore"],
                result["livello"],
                str(row.get("fattura") or ""),
                str(row.get("data") or ""),
                str(row.get("cliente") or ""),
                str(row.get("descrizione") or ""),
                self._format_number(row.get("colli")),
                self._format_number(row.get("netto")),
                str(row.get("um") or ""),
                f"{row.get('prezzo', 0):.2f} €",
                f"{row.get('importo', 0):.2f} €",
                f"{result.get('importo_atteso', 0):.2f} €",
                str(result.get("anomalie") or ""),
            )
        view = "anomalie" if self.show_anomalies_only else "righe"
        self.query_one("#report_status", Static).update(
            f"{len(rows)} {view} visualizzate su {len(self.current_results)}"
        )

    @staticmethod
    def _format_number(value) -> str:
        return "" if value is None else str(value)

    def _show_error(self, message: str) -> None:
        self.query_one("#status", Static).update("Errore durante l'analisi")
        self.query_one("#summary", Static).update(message)
        self.query_one(RichLog).write(f"[bold red]Errore:[/] {message}")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run_analysis":
            self.run_analysis()
        elif event.button.id == "open_report_html":
            report_path = getattr(self, "last_report_html", str(ROOT_DIR / "report" / "report_ultimo.html"))
            if os.path.isfile(report_path):
                os.startfile(report_path)
            else:
                self.query_one(RichLog).write(
                    "[yellow]Report HTML non disponibile: esegui prima un controllo.[/]"
                )
        elif event.button.id == "show_report":
            self.query_one("#pages", ContentSwitcher).current = "report_page"
            self._populate_results_table()
        elif event.button.id == "show_home":
            self.query_one("#pages", ContentSwitcher).current = "home_page"
        elif event.button.id == "show_anomalies":
            self.show_anomalies_only = True
            self._populate_results_table()
        elif event.button.id == "show_all_rows":
            self.show_anomalies_only = False
            self._populate_results_table()

    def action_run_analysis(self) -> None:
        self.run_analysis()


if __name__ == "__main__":
    FattureTextualApp().run()
