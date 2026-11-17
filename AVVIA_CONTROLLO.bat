@echo off
chcp 65001 > nul
echo ============================================================
echo         CONTROLLO FATTURE ORTOFRUTTA - AVVIO RAPIDO
echo ============================================================
echo.

python src\controlla_fatture.py %*

echo.
echo ============================================================
echo Controllo completato!
echo I report si trovano nella cartella 'report'.
echo Premi un tasto per uscire...
echo ============================================================
pause > nul
