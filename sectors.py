"""
sectors.py -- Classificazione settoriale unica per tutti gli agenti

La pipeline lavora solo su tre settori: Technology, Financials, Industrials.
Le fonti usano nomi diversi (GICS per le liste S&P, ICB per il FTSE MIB,
nomi Yahoo Finance per i dati fondamentali): normalize_sector() li riporta
tutti a questi tre nomi, oppure restituisce None se il settore e' fuori
perimetro.

Scelta di progetto (ereditata dallo screener originale): i servizi di
comunicazione (GICS "Communication Services", ICB "Telecommunications")
sono trattati come Technology.
"""

TARGET_SECTORS = ("Technology", "Financials", "Industrials")

_SECTOR_MAP = {
    # GICS (liste S&P 500 / S&P 600, EURO STOXX 50 su Wikipedia)
    "information technology":        "Technology",
    "communication services":        "Technology",
    "communication":                 "Technology",
    "financials":                    "Financials",
    "industrials":                   "Industrials",
    # Yahoo Finance
    "technology":                    "Technology",
    "financial services":            "Financials",
    "financial":                     "Financials",
    # ICB (FTSE MIB su Wikipedia)
    "telecommunications":            "Technology",
    "banks":                         "Financials",
    "banking":                       "Financials",
    "insurance":                     "Financials",
    "industrial goods and services": "Industrials",
    "aerospace":                     "Industrials",
    "aerospace and defense":         "Industrials",
    "construction and materials":    "Industrials",
    "shipbuilding":                  "Industrials",
    "semiconductor":                 "Technology",
}


def normalize_sector(raw) -> str | None:
    """Restituisce Technology / Financials / Industrials, oppure None se fuori perimetro."""
    if not raw or not isinstance(raw, str):
        return None
    return _SECTOR_MAP.get(raw.strip().lower())
