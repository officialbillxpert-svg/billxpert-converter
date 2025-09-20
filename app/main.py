from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import io
import pandas as pd

app = Flask(__name__)
CORS(app)  # autorise les appels depuis ton site WordPress pour les tests

@app.get("/health")
def health():
    return jsonify({"ok": True})

@app.get("/")
def index():
    # Petit formulaire de test dans le navigateur
    return """
    <h2>BillXpert Converter — Test</h2>
    <form action="/convert" method="post" enctype="multipart/form-data">
      <input type="file" name="file" accept=".pdf" required />
      <button type="submit">Envoyer</button>
    </form>
    """

@app.post("/convert")
def convert():
    # 1) Vérifs basiques de l’upload
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "Aucun fichier reçu (clé 'file' manquante)."}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"ok": False, "error": "Nom de fichier vide."}), 400
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"ok": False, "error": "Le fichier doit être un PDF."}), 400

    # 2) (MVP) — on ne parse pas encore Factur-X.
    # On renvoie un CSV "dummy" pour valider le flux et l’intégration.
    # Étape suivante: on remplacera ça par l’extraction réelle.
    data = [
        {"invoice_number": "DEMO-0001", "seller": "N/A", "buyer": "N/A", "total": 0.0, "currency": "EUR"},
    ]
    df = pd.DataFrame(data)

    # 3) Renvoi d’un CSV en pièce jointe, sans rien écrire sur disque
    csv_bytes = io.BytesIO()
    df.to_csv(csv_bytes, index=False)
    csv_bytes.seek(0)

    return send_file(
        csv_bytes,
        mimetype="text/csv",
        as_attachment=True,
        download_name="billxpert-convert.csv"
    )

if __name__ == "__main__":
    # Dev local uniquement. En prod Render lance 'gunicorn app.main:app'
    app.run(host="0.0.0.0", port=5000)
