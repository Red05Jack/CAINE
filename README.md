# C.A.I.N.E.

Creative Artificial Intelligence Networking Entity

C.A.I.N.E. ist ein Python-Discord-Bot, der mit der OpenAI API sprechen kann und
neue Bot-Funktionen als Plugins vorschlaegt. Die Plugins werden zur Laufzeit
erst nach Admin-Freigabe geladen.

## Was der Bot kann

- `!help`: zeigt normale CAINE-Commands gruppiert und listet geladene Plugins.
- `!help <pluginname>`: zeigt die genauen Commands eines geladenen Plugins.
- `!ask <frage>`: Antwort ueber OpenAI.
- `!evolve <wunsch>`: erzeugt einen Plugin-Vorschlag aus einer Beschreibung.
  Der Wunsch kann auch komplett in einer angehaengten Textdatei stehen.
- `!evolve_plugin <plugin_id> <wunsch>`: liest ein vorhandenes Plugin und
  speichert eine geaenderte Version in `plugins/pending`. Der
  Aenderungswunsch kann auch komplett in einer angehaengten Textdatei stehen.
- `!pending`: zeigt wartende Plugin-Vorschlaege.
- `!review <plugin_id>`: zeigt den Code eines Vorschlags.
- `!approve <plugin_id>`: validiert, verschiebt und laedt ein Plugin.
- `!reject <plugin_id>`: loescht einen wartenden Vorschlag.
- `!plugins`: zeigt geladene Plugins.
- `!reload_plugins`: laedt freigegebene Plugins neu.

Alle Core- und Plugin-Commands werden aus Command-Objekten registriert. Dasselbe
Objekt erzeugt den Prefix-Command mit `!` und den Slash-Command mit `/`.
Slash-Optionen stehen ebenfalls im Command-Objekt. Ohne `options` bekommt ein
Slash-Command keine kuenstliche Textoption; mit `options` werden echte Discord-
Typen wie `string`, `integer`, `boolean`, `user`, `channel`, `role` oder
`attachment` registriert.

Command-Stufen:

- `S1` / `kinger`: nur Nutzer mit einer Rolle aus `command_permissions.json`.
- `S2` / `admin`: Nutzer mit Discord-Administratorrechten oder Bot-Owner.
- `S3` / `user`: normale Nutzer ohne Sonderrechte.

`!help`, `!help <pluginname>` und `!plugins` zeigen einem Nutzer nur Commands,
fuer die seine Stufe ausreicht. Stufenlabels wie `[S1]`, `[S2]` und `[S3]`
werden nur Nutzern mit Kinger-/Master-Rolle angezeigt.

Slash-Commands werden entweder serverbezogen oder global synchronisiert. Wenn
Guild-IDs bekannt sind, nutzt C.A.I.N.E. die schnelle serverbezogene Sync und
raeumt alte globale Slash-Commands auf, damit Discord sie nicht doppelt zeigt.

Die Default-Kinger-Rolle steht in `command_permissions.json`:

```json
{
  "kingerRoleIds": [
    1523734381146148864
  ]
}
```

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

## Bestehende Plugins erweitern

Mit Trusted Plugins kann C.A.I.N.E. bestehende Plugin-Dateien lesen und daraus
eine neue Version erzeugen:

```text
!evolve_plugin levelxp fuege ein leaderboard mit top 10 hinzu
```

Der Bot sucht zuerst in `plugins/pending`, danach in `plugins/approved`. Die
neue Version landet wieder in `plugins/pending` und bekommt eine Meta-Datei,
die sagt, welche approved-Datei sie ersetzt. Nach dem Review:

```text
!approve levelxp
```

Falls die pending-Datei z.B. `levelxp_update` heisst:

```text
!approve levelxp_update
```

Beim Approval wird `plugins/approved/levelxp.py` ueberschrieben und das Plugin
neu geladen. Wenn das Laden fehlschlaegt, versucht C.A.I.N.E. die vorherige
approved-Version wiederherzustellen.

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
