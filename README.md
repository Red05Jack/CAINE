# C.A.I.N.E.

Creative Artificial Intelligence Networking Entity

C.A.I.N.E. ist ein Python-Discord-Bot, der mit der OpenAI API sprechen kann und
neue Bot-Funktionen als Plugins vorschlaegt. Die Plugins werden zur Laufzeit
erst nach Admin-Freigabe geladen.

## Was der Bot kann

- `!ask <frage>`: Antwort ueber OpenAI.
- `!evolve <wunsch>`: erzeugt einen Plugin-Vorschlag aus einer Beschreibung.
- `!pending`: zeigt wartende Plugin-Vorschlaege.
- `!review <plugin_id>`: zeigt den Code eines Vorschlags.
- `!approve <plugin_id>`: validiert, verschiebt und laedt ein Plugin.
- `!reject <plugin_id>`: loescht einen wartenden Vorschlag.
- `!plugins`: zeigt geladene Plugins.
- `!reload_plugins`: laedt freigegebene Plugins neu.
- Adminrechte kommen von Discord-Administrator, Bot-Owner, `CAINE_ADMIN_ROLE_NAMES`
  oder `CAINE_ADMIN_ROLE_IDS`.

## Sicherheit

Standardmaessig fuehrt der Bot keinen beliebigen Chat-Code direkt aus. Neue
Funktionen laufen ueber diesen Ablauf:

1. User/Admin beschreibt eine gewuenschte Funktion.
2. OpenAI erzeugt ein kleines Plugin nach einer festen Schnittstelle.
3. Der Bot speichert es in `plugins/pending`.
4. Ein AST-Validator blockiert gefaehrliche Imports, Builtins und Python-Muster.
5. Ein Admin prueft und aktiviert das Plugin mit `!approve`.

Das ist keine vollstaendige Sandbox. Python-Code im selben Prozess kann nie
perfekt isoliert werden. Fuer echte Produktion sollte der Plugin-Check in einen
separaten Container oder Worker ausgelagert werden.

### Trusted Plugins

Wenn `CAINE_TRUSTED_PLUGINS=true` in `.env` gesetzt ist, gilt ein anderer
Modus: freigegebene Plugins duerfen vollen Python-/Discord-Code ausfuehren.
Dann werden strenge Validator-Fehler wie `ctx.bot`, `ctx.channel`,
`try/except`, Top-Level-Hilfsfunktionen und Event-Handler nicht mehr blockiert.

Trusted Plugins bekommen zusaetzlich:

- `api.bot`: direkter Zugriff auf den Discord Bot.
- `api.manager`: Zugriff auf den Plugin-Manager.
- `api.shared`: gemeinsamer Speicher fuer laufende Plugins.
- `@api.event("message")`: Discord-Events abonnieren.
- `@api.on("topic")` und `await api.emit("topic", ...)`: Plugin-zu-Plugin-Kommunikation.
- `await api.storage_get/set/delete(...)`: persistenter Plugin-Speicher.

Dieser Modus bedeutet: approved Plugins koennen alles, was der Bot-Prozess kann.

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Trage danach in `.env` mindestens `DISCORD_TOKEN` und `OPENAI_API_KEY` ein.
Im Discord Developer Portal muss fuer den Bot das Message Content Intent
aktiviert sein.

Wichtig: echte Tokens gehoeren nur in `.env`, nicht in `.env.example`.

## Start

```powershell
python main.py
```

## Tests

```powershell
pytest
```
