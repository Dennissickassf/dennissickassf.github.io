"""Der Hochlader: lädt einen freigegebenen Clip über die OFFIZIELLE YouTube-Schnittstelle hoch und plant ihn.

Nur nach Dennis' "Ja" für genau diesen Clip (siehe CLAUDE.md). Kein Klick-Bot, kein Browser-Fernsteuern.

    python hochlader.py anmelden                 einmalig: Browser geht auf, Dennis klickt "Zulassen"
    python hochlader.py hochladen <ordner> <datei> [termin]
                                                 Clip aus freigegeben/ hochladen, Titel/Beschreibung aus <clip>.json,
                                                 geplant für termin (z. B. 2026-11-20T18:00) oder den nächsten freien Termin
    python hochlader.py status                   ist die Anmeldung da?

Die Zugangsdaten liegen in geheim/ (google_client.json von Dennis, youtube_token.json nach dem Anmelden).
Sie werden nie angezeigt. Solange Google das Projekt nicht geprüft hat, bleiben hochgeladene Videos PRIVAT.
"""

import base64
import hashlib
import http.server
import json
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

ORDNER = Path(__file__).resolve().parent
GEHEIM = ORDNER / "geheim"
CLIENT = GEHEIM / "google_client.json"
TOKEN = GEHEIM / "youtube_token.json"
SCOPE = "https://www.googleapis.com/auth/youtube.upload"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"


def client_datei():
    """Die NEUESTE Client-Datei in geheim/ (google_client….json oder client_secret_….json, so wie Google sie
    herunterlädt). So ist egal, ob Windows beim Umbenennen ein unsichtbares „.json“ angehängt hat (20.11.: die alte,
    kaputte Datei wurde noch benutzt, obwohl die neue daneben lag)."""
    kandidaten = [p for p in GEHEIM.glob("*.json") if p.name.startswith(("google_client", "client_secret"))]
    return max(kandidaten, key=lambda p: p.stat().st_mtime) if kandidaten else None


def client():
    datei = client_datei()
    if not datei:
        sys.exit("geheim/google_client.json fehlt (siehe YOUTUBE_AUTOMATISCH.md).")
    print(f"(Client-Datei: {datei.name})")  # nur der Dateiname, kein Inhalt
    daten = json.loads(datei.read_text(encoding="utf-8-sig"))
    return daten.get("installed") or daten.get("web")


def post_formular(url, felder):
    anfrage = urllib.request.Request(url, data=urllib.parse.urlencode(felder).encode(), method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(anfrage, timeout=60) as antwort:
            return json.loads(antwort.read().decode())
    except urllib.error.HTTPError as fehler:
        # Fehlertext von Google zeigen, aber nie Schlüssel
        sys.exit(f"Google hat abgelehnt ({fehler.code}): {fehler.read().decode(errors='replace')[:300]}")


# ---------- einmalig anmelden ----------

def anmelden():
    c = client()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    zustand = secrets.token_urlsafe(16)
    ergebnis = {}

    class Rueckruf(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            werte = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if werte.get("state", [""])[0] == zustand:
                ergebnis["code"] = werte.get("code", [""])[0]
                ergebnis["fehler"] = werte.get("error", [""])[0]
            text = ("Fertig! Du kannst dieses Fenster schließen." if ergebnis.get("code")
                    else "Hat nicht geklappt – schau ins schwarze Fenster.")
            daten = f"<html><body style='font-family:sans-serif;font-size:22px'>{text}</body></html>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(daten)))
            self.end_headers()
            self.wfile.write(daten)

    server = http.server.HTTPServer(("127.0.0.1", 0), Rueckruf)  # nur lokal, nur für diesen einen Rückruf
    zurueck = f"http://127.0.0.1:{server.server_address[1]}"
    adresse = c["auth_uri"] + "?" + urllib.parse.urlencode({
        "client_id": c["client_id"], "redirect_uri": zurueck, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "state": zustand,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    print("Der Browser geht gleich auf. Melde dich mit deinem YouTube-Konto an und klick auf 'Zulassen'.")
    print("(Kommt 'Google hat diese App nicht überprüft': auf 'Erweitert' und dann auf"
          " 'Weiter zu ViceCityScreams Uploader' klicken – das ist dein eigenes Programm.)")
    threading.Timer(1.0, lambda: webbrowser.open(adresse)).start()
    server.timeout = 300
    ende = time.time() + 300
    while "code" not in ergebnis and time.time() < ende:
        server.handle_request()
    server.server_close()
    if not ergebnis.get("code"):
        sys.exit(f"Anmeldung abgebrochen ({ergebnis.get('fehler') or 'keine Antwort'}).")
    token = post_formular(c["token_uri"], {
        "code": ergebnis["code"], "client_id": c["client_id"], "client_secret": c.get("client_secret", ""),
        "redirect_uri": zurueck, "grant_type": "authorization_code", "code_verifier": verifier})
    if not token.get("refresh_token"):
        sys.exit("Google hat keinen dauerhaften Zugang geschickt. Bitte nochmal anmelden.")
    GEHEIM.mkdir(exist_ok=True)
    TOKEN.write_text(json.dumps({"refresh_token": token["refresh_token"]}), encoding="utf-8")
    print("Angemeldet! Der Hochlader darf jetzt Videos auf deinen Kanal laden.")


def zugang():
    """Kurzlebigen Zugang mit dem gespeicherten Refresh-Token holen."""
    if not TOKEN.exists():
        sys.exit("Noch nicht angemeldet: erst 'python hochlader.py anmelden'.")
    c = client()
    token = post_formular(c["token_uri"], {
        "refresh_token": json.loads(TOKEN.read_text(encoding="utf-8"))["refresh_token"],
        "client_id": c["client_id"], "client_secret": c.get("client_secret", ""), "grant_type": "refresh_token"})
    return token["access_token"]


# ---------- hochladen ----------

def lade_datei_hoch(pfad, metadaten):
    """Resumable Upload über die offizielle YouTube Data API. Gibt die Video-Antwort (dict) zurück."""
    groesse = Path(pfad).stat().st_size
    kopf = {"Authorization": f"Bearer {zugang()}", "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(groesse)}
    anfrage = urllib.request.Request(UPLOAD_URL, data=json.dumps(metadaten).encode(), headers=kopf, method="POST")
    try:
        with urllib.request.urlopen(anfrage, timeout=60) as antwort:
            ziel = antwort.headers["Location"]
    except urllib.error.HTTPError as fehler:
        sys.exit(f"YouTube hat den Upload abgelehnt ({fehler.code}): {fehler.read().decode(errors='replace')[:400]}")
    with open(pfad, "rb") as f:
        anfrage = urllib.request.Request(ziel, data=f.read(), method="PUT",
                                         headers={"Content-Type": "video/mp4", "Content-Length": str(groesse)})
    try:
        with urllib.request.urlopen(anfrage, timeout=1800) as antwort:
            return json.loads(antwort.read().decode())
    except urllib.error.HTTPError as fehler:
        sys.exit(f"Upload abgebrochen ({fehler.code}): {fehler.read().decode(errors='replace')[:400]}")


def test_upload():
    """5-Sekunden-Testvideo (lila Bild, Stille) PRIVAT hochladen – kein echter Clip, nichts wird verbucht."""
    einst = json.loads((ORDNER / "einstellungen.json").read_text(encoding="utf-8"))
    test = ORDNER / "vorschau" / "test_upload.mp4"
    test.parent.mkdir(exist_ok=True)
    subprocess.run([einst["ffmpeg"], "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=0x6a1b9a:s=1080x1920:d=5:r=30",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "5",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(test)], check=True)
    metadaten = {"snippet": {"title": "Test upload (please ignore)", "description": "Test of my own upload tool.",
                             "categoryId": "20"},
                 "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False}}
    print("Lade Testvideo hoch (privat) ...")
    video = lade_datei_hoch(test, metadaten)
    test.unlink(missing_ok=True)
    print(f"Fertig: https://youtube.com/shorts/{video.get('id', '')}  "
          f"(Status: {video.get('status', {}).get('privacyStatus')}, Upload: {video.get('status', {}).get('uploadStatus')})")

def hochladen(ordner, datei, termin=None):
    import box  # erst hier: Termine/Pfade wie in der Box
    pfad = box.clip_pfad(box.FREI, ordner, datei)
    if not pfad:
        sys.exit("Clip nicht in freigegeben/ gefunden.")
    info = json.loads(pfad.with_suffix(".json").read_text(encoding="utf-8")) if pfad.with_suffix(".json").exists() else {}
    einst = box.einstellungen()
    if termin:
        geplant = datetime.fromisoformat(termin)
    else:
        geplant = box.naechste_termine(1, einst)[0]
    geplant_utc = geplant.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    titel = (info.get("titel") or "GTA 6 moment").strip()[:100]
    beschreibung = info.get("beschreibung", "")
    tags = [t.strip("#") for t in beschreibung.split() if t.startswith("#")][:10]
    metadaten = {
        "snippet": {"title": titel, "description": beschreibung, "tags": tags + ["GTA 6", "funny moments"],
                    "categoryId": "20", "defaultLanguage": "en", "defaultAudioLanguage": "en"},
        "status": {"privacyStatus": "private", "publishAt": geplant_utc,
                   "selfDeclaredMadeForKids": False, "containsSyntheticMedia": False},
    }
    print(f"Lade hoch: {titel} ({pfad.stat().st_size / 1e6:.0f} MB), geplant für {geplant:%d.%m. %H:%M} ...")
    video = lade_datei_hoch(pfad, metadaten)
    video_id = video.get("id", "")
    print(f"Fertig: https://youtube.com/shorts/{video_id}  (Status: {video.get('status', {}).get('privacyStatus')})")
    # wie "Hochgeladen & geplant" in der Box
    code, text = box.als_hochgeladen({"ordner": ordner, "datei": datei, "termin": geplant.isoformat(timespec="minutes")})
    with open(ORDNER / "hochgeladen.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"zeit": datetime.now().isoformat(timespec="seconds"), "ordner": ordner, "datei": datei,
                            "youtube_id": video_id, "link": f"https://youtube.com/shorts/{video_id}"},
                           ensure_ascii=False) + "\n")
    return video_id


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["anmelden"]:
        anmelden()
    elif a[:1] == ["status"]:
        print("Anmeldung vorhanden." if TOKEN.exists() else "Noch nicht angemeldet.")
    elif a[:1] == ["test"]:
        test_upload()
    elif a[:1] == ["hochladen"] and len(a) >= 3:
        hochladen(a[1], a[2], a[3] if len(a) > 3 else None)
    else:
        print(__doc__)
